"""Interactive TUI for share rooms using prompt_toolkit."""

import asyncio
import threading
import time
from pathlib import Path
from typing import List, Optional, Callable, Any, Dict

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, FormattedTextControl, Dimension, ConditionalContainer
from prompt_toolkit.widgets import Frame, Label, Button, RadioList, Box, TextArea
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML, FormattedText
from prompt_toolkit.styles import Style

from .room_manager import RoomManager
from .discovery import RoomDiscovery
from .qr import generate_room_qr, parse_qr_data
from .server import ShareServer
from .client import ShareClient
from .queue import TransferQueue, QueuedFile
from .picker import launch_picker
from .models import FileEntry


class ShareTUI:
    """Non-blocking interactive TUI for Share Phase 2."""

    def __init__(self, room_manager: RoomManager, bus):
        self.room_manager = room_manager
        self.bus = bus
        self.discovery = RoomDiscovery()
        self.server: Optional[ShareServer] = None
        self.client: Optional[ShareClient] = None
        self.queue = TransferQueue()
        
        # UI State
        self.screen = "main" # main, lobby, queue, targeting, qr_join
        self.menu_index = 0
        self.list_index = 0
        self.running = True
        self.last_msg = ""
        self.last_msg_time = 0.0 # TTL for messages
        self._direct_mode = False
        self.last_error = ""
        
        self.body_control = FormattedTextControl(self._get_content, focusable=True)
        self.body_window = Window(content=self.body_control, height=Dimension(min=10))
        
        # Data
        self.discovered_rooms = []
        self.target_device_id = "ALL"  # ALL or specific ID
        self.persistent_transfers = {} # device_id -> { "name": ..., "progress": ... }
        
        # Inputs
        self.input_field = TextArea(multiline=False, password=False)
        self.input_field.accept_handler = self._handle_input_accept
        
        # Styles
        self.style = Style.from_dict({
            'header': '#00ff00 bold',
            'footer': '#aaaaaa italic',
            'selected': '#00ff00 bold reverse',
            'error': '#ff0000 bold',
            'msg': '#00aaff',
            'device-you': '#00ff00 italic',
            'device-active': '#ffffff',
            'device-idle': '#888888',
            'room-id': '#ffff00 bold',
            'input-field': 'bg:#333333 #ffffff',
        })
        
        self.kb = KeyBindings()
        self._setup_keybindings()
        
        # Background refreshing
        self.refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self.shutdown_event = threading.Event()

    def cleanup(self):
        """Robust shutdown of all share resources."""
        self.running = False
        self.shutdown_event.set()
        
        # 1. Notify server we are leaving (if participant)
        if self.client and self.room_manager.current_room and self.room_manager.role == 'RECV':
            try:
                self.client.leave_room()
            except:
                pass
        
        # 2. Stop Heartbeat/Client
        if self.client:
            try: self.client.stop_heartbeat()
            except: pass
            self.client = None
            
        # 3. Stop Server
        if self.server:
            try: self.server.stop()
            except: pass
            self.server = None
            
        # 4. Stop Discovery (FIXES GHOST ROOMS)
        try:
            self.discovery.stop()
        except:
            pass
            
        self.room_manager.leave_room()

        # Phase 12: Notification & Global State
        self.show_global_progress = False
        try:
            from dlm.app.commands import ShareNotify, TakeoverRoom
            bus.register(ShareNotify, self._handle_share_notify)
            bus.register(TakeoverRoom, self._handle_takeover)
        except:
            pass

    def _handle_share_notify(self, command):
        """Update notification msg/error box."""
        if command.is_error:
            self._set_error(command.message)
        else:
            self._set_msg(command.message)
        try:
            get_app().invalidate()
        except: pass

    def _set_msg(self, msg: str):
        self.last_msg = msg
        self.last_msg_time = time.time()
        # Clear error when new msg arrives
        self.last_error = ""
        try:
            get_app().invalidate()
        except:
            pass

    def _set_error(self, err: str):
        self.last_error = err
        self.last_msg = ""
        self.last_msg_time = time.time() # Errors also TTL?
        try:
            get_app().invalidate()
        except:
            pass


    def _do_refresh(self):
        """Force refresh of room state (non-blocking)."""
        self._set_msg("Refreshing...")
        
        def _refresh_bg():
            if self.client:
                try:
                    data = self.client.get_room_info()
                    if data:
                        self._sync_room_state(data)
                except:
                    pass
            # Clear message after refresh
            time.sleep(0.3)
            self._set_msg("")
            try:
                get_app().invalidate()
            except:
                pass
        
        # Run in background to avoid freeze
        threading.Thread(target=_refresh_bg, daemon=True).start()

    def _add_files(self):
        """Invoke ranger file picker."""
        def on_add(path: Path, as_folder: bool = False) -> int:
             # Add to transfer queue (for push)
             count = self.queue.add_path(path, as_folder=as_folder)
             
             if self.server and self.room_manager.current_room:
                 try:
                     fe = FileEntry.from_path(str(path))
                     fe.owner_device_id = self.room_manager.device_id
                     # Deduplicate
                     if not any(f.file_id == fe.file_id for f in self.server.file_entries):
                         self.server.file_entries.append(fe)
                         # Broadcast update
                         if self.server and self.server.ws_manager and self.server.ws_manager.loop:
                             asyncio.run_coroutine_threadsafe(self.server.broadcast_state(), self.server.ws_manager.loop)
                 except Exception:
                     pass
             
             # Phase 15: If participant, announce to host
             elif self.client and self.room_manager.current_room:
                 try:
                     import requests
                     fe = FileEntry.from_path(str(path))
                     payload = {
                         "device_id": self.room_manager.device_id,
                         "files": [{
                             "file_id": fe.file_id,
                             "name": fe.name,
                             "size": fe.size_bytes,
                             "is_dir": fe.is_dir
                         }]
                     }
                     headers = {"Authorization": f"Bearer {self.client.session_id}"}
                     requests.post(f"{self.client.base_url}/room/add-file", json=payload, headers=headers, timeout=5)
                 except Exception:
                     pass
                     
             return count
        
        launch_picker(on_add)
        self._set_msg(f"Items added. (Shared: {self._get_files_count()})")

    def _refresh_loop(self):
        while self.running:
            try:
                if self.screen == "lobby" and self.client:
                    # Poll host for updates
                    data = self.client.get_room_info() 
                    if data:
                        self._sync_room_state(data)
                
                # Check message TTL
                if self.last_msg and (time.time() - self.last_msg_time > 2.0):
                    self.last_msg = ""
                    get_app().invalidate()
                    
                # Fix: Check error TTL if desired, or leave persistent
                if self.last_error and (time.time() - self.last_msg_time > 4.0):
                    self.last_error = "" # longer TTL for errors
                    get_app().invalidate()

            except Exception:
                pass
            
            time.sleep(1.5) # Slower poll to reduce load

    def _get_files_count(self) -> int:
        if self.server:
            return len(self.server.file_entries)
        if self.client:
            return len(self.client.room_files)
        return 0


    def _setup_keybindings(self):
        @self.kb.add('up')
        def _(event):
            if event.app.layout.has_focus(self.input_field): return
            if self.list_index > 0:
                self.list_index -= 1
            else:
                count = self._get_current_list_count()
                self.list_index = count - 1 if count > 0 else 0

        @self.kb.add('down')
        def _(event):
            if event.app.layout.has_focus(self.input_field): return
            count = self._get_current_list_count()
            if self.list_index < count - 1:
                self.list_index += 1
            else:
                self.list_index = 0

        @self.kb.add('enter')
        def _(event):
            if event.app.layout.has_focus(self.input_field):
                self._handle_input_accept(self.input_field.buffer)
            else:
                self._handle_enter()

        @self.kb.add('delete', filter=~Condition(lambda: get_app().layout.has_focus(self.input_field)))
        @self.kb.add('x', filter=~Condition(lambda: get_app().layout.has_focus(self.input_field)))
        def _(event):
            if self.screen == "queue":
                if len(self.queue) > 0 and self.list_index < len(self.queue):
                    self.queue.queue.pop(self.list_index)
                    if self.list_index >= len(self.queue) and len(self.queue) > 0:
                        self.list_index = len(self.queue) - 1
                    self._set_msg("Item removed from queue.")
            elif self.screen == "lobby_files":
                 # Maybe allow hiding? For now just msg
                 pass

        @self.kb.add('p', filter=~Condition(lambda: get_app().layout.has_focus(self.input_field)))
        def _(event):
            """Toggle global progress bar."""
            self.show_global_progress = not self.show_global_progress
            self._set_msg(f"Global Progress: {'Visible' if self.show_global_progress else 'Hidden'}")
            get_app().invalidate()

        @self.kb.add('q', filter=~Condition(lambda: get_app().layout.has_focus(self.input_field)))
        @self.kb.add('escape')
        def _(event):
            if event.app.layout.has_focus(self.input_field):
                # If typing, just unfocus
                event.app.layout.focus(self.body_window)
            else:
                self._handle_back(event)

    def _handle_input_accept(self, buffer):
        text = buffer.text.strip()
        if self.screen == "manual_join":
            # We need to parse "IP:PORT TOKEN" or similar
            # For Phase 2, let's say "192.168.1.1:8080 XXX-XXX"
            parts = text.split()
            if len(parts) >= 2:
                addr, token = parts[0], parts[1]
                if ":" in addr:
                    ip, port = addr.split(":")
                    self._do_join(ip, int(port), token)
                else:
                    self.last_error = "Invalid format. Use IP:PORT TOKEN"
            else:
                self.last_error = "Use: IP:PORT TOKEN"
        elif self.screen == "qr_join":
            self._join_from_qr(text)
        
        buffer.reset()
        get_app().layout.focus(self.body_window) 

    # Actions at bottom of lists
    def _get_lobby_actions(self):
        return [
            ("add", "Add Files / Folders"),
            ("view", "View Shared Files"),
            ("queue", "Open Queue / Send Files"),
            ("qr", "Show Room QR"),
            ("leave", "Leave Room")
        ]

    def _get_queue_actions(self):
        return [
            ("send", "Send All Pending"),
            ("clear", "Clear Completed"),
            ("back", "Back to Lobby")
        ]

    def _get_current_list_count(self) -> int:
        if self.screen == "main":
            return len(self._get_main_menu_items())
        elif self.screen == "lobby":
            # ONLY Action Menu is selectable
            return len(self._get_lobby_actions())
        elif self.screen == "queue":
            return len(self.queue) + len(self._get_queue_actions())
        elif self.screen == "scan_results":
            return len(self.discovered_rooms)
        elif self.screen == "lobby_files":
            files = self.client.room_files if self.client else []
            return len(files) + len(self._get_lobby_file_actions())
        return 0

    def _get_main_menu_items(self):
        return [
            ("create", "Create Room (Host)"),
            ("scan", "Scan for Rooms (Auto)"),
            ("exit", "Exit")
        ]


    def _get_lobby_file_actions(self):
        return [
            ("download_all", "Download All Files"),
            ("back", "Back to Lobby Info")
        ]

    def _handle_enter(self):
        if self.screen == "main":
            items = self._get_main_menu_items()
            action = items[self.list_index][0]
            if action == "create": self._create_room()
            elif action == "scan": self._scan_rooms()
            elif action == "exit": self.running = False; get_app().exit()
        
        elif self.screen == "scan_results":
            if self.discovered_rooms and self.list_index < len(self.discovered_rooms):
                room = self.discovered_rooms[self.list_index]
                self._do_join(room['ip'], room['port'], room['token'])

        elif self.screen == "lobby":
            room = self.room_manager.current_room
            if not room: return
            
            action = self._get_lobby_actions()[self.list_index][0]
            if action == "add": self._add_files()
            elif action == "copy": self._copy_invite_link()
            elif action == "view": 
                if self.client: self.client.get_room_info()
                self.screen = "lobby_files"; self.list_index = 0
            elif action == "queue": self.screen = "queue"; self.list_index = 0
            elif action == "qr": self.screen = "qr"; self.list_index = 0
            elif action == "refresh": self._do_refresh()
            elif action == "leave": self._leave_room()

        elif self.screen == "queue":
            room = self.room_manager.current_room
            devices = room.devices if room else []
            other_devices = [d for d in devices if "(you)" not in d.name]
            device_ids = ["ALL"] + [d.device_id for d in other_devices]

            if self.list_index < len(self.queue):
                item = self.queue.queue[self.list_index]
                # Toggle through devices (excluding self)
                try:
                    curr_idx = device_ids.index(item.target_device_id)
                    next_idx = (curr_idx + 1) % len(device_ids)
                    next_target = device_ids[next_idx]
                    
                    # Ensure we don't target self
                    if next_target == self.room_manager.device_id and len(device_ids) > 1:
                        next_idx = (next_idx + 1) % len(device_ids)
                        next_target = device_ids[next_idx]
                    
                    item.target_device_id = next_target
                except ValueError:
                    item.target_device_id = "ALL"
            else:
                action_idx = self.list_index - len(self.queue)
                action = self._get_queue_actions()[action_idx][0]
                if action == "send": self._execute_queue_transfer()
                elif action == "clear": self.queue.clear_completed()
                elif action == "back": self.screen = "lobby"; self.list_index = 0
            
        elif self.screen == "lobby_files":
            files = self.client.room_files if self.client else []
            if self.list_index < len(files):
                 # Toggle selection? For now just download one
                 f = files[self.list_index]
                 self._download_files([f])
            else:
                 action_idx = self.list_index - len(files)
                 action = self._get_lobby_file_actions()[action_idx][0]
                 if action == "download_all": self._download_files(files)
                 elif action == "back": self.screen = "lobby"; self.list_index = 0

    def _do_join(self, ip: str, port: int, token: str):
        """Join a room in the background."""
        self.screen = "joining"
        self._set_msg("Joining room...")
        
        def _target():
            if not self.client:
                self.client = ShareClient(self.bus)
            
            try:
                success = self.client.join_room(ip, port, token, self.room_manager.device_name, self.room_manager.device_id)
                if success:
                    # Sync Local RoomManager to avoid "Room lost" error
                    self.room_manager.join_room(
                        room_id=self.client.room_id,
                        ip=ip,
                        port=port,
                        token=token
                    )
                    self.screen = "lobby"
                    self.list_index = 0
                    self.last_msg = f"Joined room {self.client.room_id} at {ip}:{port}"
                else:
                    self._set_error("Failed to join. Check IP/Token.")
                    self.screen = "scan_results" # Fallback
            except Exception as e:
                self._set_error(f"Join error: {e}")
                self.screen = "main"
            
            try:
                get_app().invalidate()
            except: pass

        threading.Thread(target=_target, daemon=True).start()

    def _join_from_qr(self, data: str):
        """Parse QR/Invite data and join."""
        try:
            from .qr import parse_qr_data
            room_info = parse_qr_data(data)
            self._do_join(room_info['ip'], room_info['port'], room_info['token'])
        except Exception as e:
            self._set_error(f"Invalid Invite Link: {e}")

    def _scan_rooms(self):
        """Scan for available rooms and show results."""
        self.screen = "scanning"
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        self.discovered_rooms = self.discovery.scan_rooms(timeout=3.0)
        self.screen = "scan_results"
        self.list_index = 0

    def _execute_queue_transfer(self):
        """Start transferring all pending items in queue."""
        if not self.client or not self.queue:
            return

        room = self.room_manager.current_room
        other_devices = [d for d in room.devices if "(you)" not in d.name and d.is_active()] if room else []
        if not other_devices:
             self._set_error("No active peers to send to!")
             return
            
        # 1. Prepare server with all queued files
        if self.server:
            for item in self.queue.queue:
                if item.status == "pending":
                    fe = FileEntry.from_path(str(item.file_path))
                    if not any(f.file_id == fe.file_id for f in self.server.file_entries):
                        self.server.file_entries.append(fe)
        
        # 2. Coordinate with host
        files_data = []
        for item in self.queue.queue:
            if item.status == "pending":
                # For folder units, we don't need a single FileEntry with full size, 
                # but we need the correct folder name and is_dir flag.
                files_data.append({
                    "file_id": str(uuid.uuid4()) if item.is_dir else FileEntry.from_path(str(item.file_path)).file_id,
                    "name": item.file_path.name,
                    "size": item.file_size,
                    "is_dir": item.is_dir,
                    "targets": [item.target_device_id] if item.target_device_id != "ALL" else ["ALL"]
                })
                item.status = "transferring"
        
        if files_data:
            self.client.queue_transfer(["SPECIAL_MULTIPER"], files_data) # Use a flag if server supports per-file targets
            self.last_msg = f"Transfer started for {len(files_data)} items."
            self.screen = "lobby"; self.list_index = 0
        else:
            self.last_error = "No pending files found."

    def _handle_back(self, event):
        if self.screen == "main":
             self.running = False
             get_app().exit()
        elif self.screen in ("lobby", "scan_results"):
             # Phase 16: Handover before leaving if we are the host
             if self.server and self.room_manager.current_room:
                 success = self._initiate_handover()
                 if success:
                     self._set_msg("Handover initiated. Closing...")
                     time.sleep(1.0) # Grace period for broadcast
             
             self._leave_room()
        elif self.screen == "queue":
             self.screen = "lobby"; self.list_index = 0
        elif self.screen == "qr_join":
             self.screen = "main"; self.list_index = 0
        elif self.screen in ("qr", "lobby_files"):
             self.screen = "lobby"; self.list_index = 0

    def _copy_invite_link(self):
        """Copy invitation URL to clipboard."""
        if not self.room_manager.current_room:
            return
            
        room = self.room_manager.current_room
        url = f"http://{room.host_ip}:{room.port}/invite?t={room.token}"
        
        try:
            import pyperclip
            pyperclip.copy(url)
            self._set_msg("Invite link COPIED to clipboard.")
        except KeyboardInterrupt:
            return
        except ImportError:
            # Fallback: Just show it very clearly
            self._set_msg(f"LINK: {url}")
        except Exception as e:
            self._set_error(f"Copy failed: {e}")

    def _leave_room(self):
        """Cleanup and return to main screen."""
        if self.client:
            try:
                self.client.update_device_state("idle")
                self.client.stop_heartbeat()
            except: pass
            self.client = None
        if self.server:
            # Shutdown server
            try: self.server.stop()
            except: pass
            self.server = None
            
        self.room_manager.leave_room()
        self.screen = "main"
        self.list_index = 0
        self.last_msg = "Left share room."
        
        # If we were started in direct room mode (dlm share room create), we should exit
        if getattr(self, '_direct_mode', False):
            raise KeyboardInterrupt 

    def _download_files(self, files: List[dict]):
        """Queue files for download."""
        if not self.client or not files: return
        for f in files:
            # Add to local engine via bus
            from dlm.app.commands import AddDownload
            template = self.client._get_output_template(f['name'])
            self.bus.handle(AddDownload(
                url=f"{self.client.base_url}/download/{f['file_id']}?token={self.client.session_id}",
                output_template=template,
                title=f['name'],
                source='share',
                ephemeral=True
            ))
        self.last_msg = f"Started download of {len(files)} items."
        self.screen = "lobby"; self.list_index = 0


    def _create_room(self):
        """Create a new room and start server."""
        self.screen = "creating"
        self._set_msg("Creating room...")
        threading.Thread(target=self._do_create_room, daemon=True).start()
    
    def _do_create_room(self):
        """Background thread for room creation."""
        try:
            room = self.room_manager.create_room()
            # Pass empty list initially, will add as files are queued for send
            self.server = ShareServer(room=room, port=room.port, bus=self.bus, file_entries=[])
            threading.Thread(target=self.server.run_server, daemon=True).start()
            
            # Wait for port (non-blocking for TUI)
            for _ in range(20):  # 2 seconds max
                if self.server.port: 
                    break
                time.sleep(0.1)
            
            if not self.server.port:
                self._set_error("Failed to start server")
                self.screen = "main"
                return
                
            self.client = ShareClient(self.bus)
            success = self.client.join_room(
                room.host_ip, 
                self.server.port, 
                room.token, 
                self.room_manager.device_name, 
                self.room_manager.device_id
            )
            
            if not success:
                self._set_error("Failed to join own room")
                self.screen = "main"
                return
            
            # Advertise room
            adv_success = self.discovery.advertise_room(
                room.room_id, 
                room.token, 
                self.server.port, 
                self.room_manager.device_id
            )
            
            if not adv_success:
                self._set_msg("Room created (LAN discovery unavailable)")
            else:
                self._set_msg(f"Room {room.room_id} created successfully")
            
            self.screen = "lobby"
            self.list_index = 0
            
            try:
                get_app().invalidate()
            except:
                pass
                
        except Exception as e:
            self._set_error(f"Room creation failed: {e}")
            self.screen = "main"
            try:
                get_app().invalidate()
            except:
                pass

    def _select_successor(self) -> Optional[Any]:
        """Find the best device to take over the room."""
        if not self.room_manager.current_room: return None
        room = self.room_manager.current_room
        candidates = [d for d in room.devices if d.device_id != self.room_manager.device_id and d.is_active()]
        if not candidates: return None
        
        # Count files per owner
        file_counts = {}
        if self.server:
             for f in self.server.file_entries:
                 file_counts[f.owner_device_id] = file_counts.get(f.owner_device_id, 0) + 1
        
        # Sort by file count then name (randomness)
        candidates.sort(key=lambda d: (file_counts.get(d.device_id, 0), d.name), reverse=True)
        return candidates[0]

    def _initiate_handover(self) -> bool:
        """Trigger handover process."""
        successor = self._select_successor()
        if not successor: return False
        
        # Give successor's IP a default port guess or just use 0
        # For now, successor will notify us of their actual port via POST /room/handover-ready
        
        handover_info = {
            "action": "become_host",
            "room_id": self.room_manager.current_room.room_id,
            "token": self.room_manager.current_room.token,
            "files": [
                {"id": f.file_id, "name": f.name, "size": f.size_bytes, "owner_id": f.owner_device_id, "is_dir": f.is_dir}
                for f in self.server.file_entries
            ],
            "devices": [
                {"device_id": d.device_id, "name": d.name, "ip": d.ip, "state": d.state}
                for d in self.room_manager.current_room.devices
            ]
        }
        
        successor_obj = self.room_manager.current_room.get_device(successor.device_id)
        if successor_obj:
            successor_obj.pending_transfers.append(handover_info)
            return True
        return False

    def _handle_takeover(self, cmd):
        """Handle promotion to room host."""
        self._set_msg("👑 PROMOTED TO HOST. Re-starting server...")
        
        def _bg():
            from .room import Room, Device
            from .server import ShareServer
            
            # Transition models
            new_room = Room(
                room_id=cmd.room_id,
                token=cmd.token,
                host_ip=self.room_manager._get_local_ip(),
                port=0,
                host_device_name=self.room_manager.device_name,
                host_device_id=self.room_manager.device_id
            )
            for d in cmd.devices:
                new_room.devices.append(Device(device_id=d["device_id"], name=d["name"], ip=d["ip"], state=d["state"]))
            
            self.room_manager.current_room = new_room
            
            # Start server
            from .models import FileEntry
            files = []
            for f in cmd.files:
                files.append(FileEntry(file_id=f["id"], name=f["name"], size_bytes=f["size"], owner_device_id=f["owner_id"], is_dir=f.get("is_dir", False)))
            
            self.server = ShareServer(room=new_room, port=0, bus=self.bus, file_entries=files)
            threading.Thread(target=self.server.run_server, daemon=True).start()
            
            # Wait for port
            for _ in range(20):
                if self.server.port: break
                time.sleep(0.1)
            
            if self.server.port:
                # Notify OLD host that we are ready
                try:
                    import requests
                    payload = {"ip": new_room.host_ip, "port": self.server.port, "device_id": self.room_manager.device_id}
                    headers = {"Authorization": f"Bearer {self.client.session_id}"}
                    # We use the OLD client's base_url which is the old host
                    requests.post(f"{self.client.base_url}/room/handover-ready", json=payload, headers=headers, timeout=5)
                except:
                    pass
                
                # Now behave like host
                self.client.base_url = f"http://{new_room.host_ip}:{self.server.port}"
                self._set_msg("👑 Server started. You are now the room host.")
                get_app().invalidate()
        
        threading.Thread(target=_bg, daemon=True).start()

    def _sync_room_state(self, data: dict):
        """Update local models from server data."""
        if not self.room_manager.current_room:
             return
             
        room = self.room_manager.current_room
        
        # Sync devices
        if "devices" in data:
            from .room import Device
            new_devices = []
            for d in data["devices"]:
                name = d["name"]
                # Keep the "(you)" mark for self
                if d["device_id"] == self.room_manager.device_id:
                     if "(you)" not in name: name += " (you)"
                
                dev = Device(
                    device_id=d["device_id"],
                    name=name,
                    ip=d["ip"],
                    state=d["state"],
                    current_transfer=d.get("current_transfer")
                )
                new_devices.append(dev)
            room.devices = new_devices
            
        # Sync files
        if "files" in data:
            self.client.room_files = data["files"]
            
        # Phase 16: Handling Migration
        if "migration" in data and data["migration"]:
            m = data["migration"]
            # If we are NOT the new host, but the current host is telling us to move
            if self.client and m["id"] != self.room_manager.device_id:
                new_url = f"http://{m['ip']}:{m['port']}"
                if self.client.base_url != new_url:
                    self._set_msg(f"Room migrating to {m['ip']}:{m['port']}...")
                    self.client.base_url = new_url
                    # Update room manager host/port for future reconnects
                    if self.room_manager.current_room:
                        self.room_manager.current_room.host_ip = m["ip"]
                        self.room_manager.current_room.port = m["port"]

        try:
            get_app().invalidate()
        except: pass


    def _show_qr(self):
        """Show the QR code screen."""
        self.screen = "qr"
        self.list_index = 0

    # --- Rendering ---

    def _get_content(self):
        if self.screen == "main": return self._render_main()
        elif self.screen == "lobby": return self._render_lobby()
        elif self.screen == "queue": return self._render_queue()
        elif self.screen == "qr": return self._render_qr()
        elif self.screen == "creating": return HTML(" <header>Creating Room...</header>\n\n Please wait while the server starts...")
        elif self.screen == "scanning": return HTML(" <header>Scanning for rooms...</header>\n\n Please wait (3s)...")
        elif self.screen == "joining": return HTML(" <header>Joining room...</header>\n\n Authenticating with host...")
        elif self.screen == "scan_results": return self._render_scan_results()
        elif self.screen == "manual_join": return self._render_manual_join()
        elif self.screen == "lobby_files": return self._render_lobby_files()
        elif self.screen == "qr_join": return HTML(" <header>Join via URL</header>\n\n [Please paste the HTTP invite link here]")
        return HTML("Loading...")

    def _render_qr(self):
        room = self.room_manager.current_room
        if not room: return HTML("ERROR: NO ROOM")
        qr_art = generate_room_qr(room.room_id, room.host_ip, room.port, room.token)
        return HTML(f" <header>ROOM QR CODE (Lobby Invite)</header>\n\n{qr_art}\n\n <i>Press 'q' or 'esc' to go back.</i>")

    def _render_lobby_files(self):
        all_files = self.client.room_files if self.client else []
        # Filter out self-owned files
        files = [f for f in all_files if f.get('owner_id') != self.room_manager.device_id]
        
        lines = ["<header>DOWNLOAD FROM OTHERS</header>", ""]
        if not files:
            lines.append(" <i>No files from other devices available.</i>")
            if len(all_files) > len(files):
                lines.append(f" (Hiding {len(all_files) - len(files)} self-owned items)")
        else:
            for i, f in enumerate(files):
                style = "selected" if i == self.list_index else "default"
                size = f.get('size_bytes', 0) / 1024 / 1024
                dir_mark = "[DIR] " if f.get('is_dir') else ""
                lines.append(f" <{style}>{'>' if i == self.list_index else ' '} {dir_mark}{f['name'][:35]:<35} {size:>6.1f}MB</{style}>")
        
        lines.append("")
        actions = self._get_lobby_file_actions()
        for i, (act, label) in enumerate(actions):
            idx = i + len(files)
            style = "selected" if idx == self.list_index else "header"
            lines.append(f" <{style}>{'>' if idx == self.list_index else ' '} {label}</{style}>")
            
        return HTML("\n".join(lines))

    def _render_scan_results(self):
        lines = ["<header>Discovered Rooms</header>", ""]
        if not self.discovered_rooms:
            lines.append(" No rooms found. Press 'q' to go back.")
        else:
            for i, r in enumerate(self.discovered_rooms):
                style = "selected" if i == self.list_index else "default"
                lines.append(f" <{style}>{'>' if i == self.list_index else ' '} {r['room_id']} - {r['hostname']} ({r['ip']})</{style}>")
        return HTML("\n".join(lines))

    def _render_manual_join(self):
        return HTML(" <header>Manual Join</header>\n\n Enter <b>IP:PORT TOKEN</b> (e.g. 192.168.1.5:8080 ABC-XYZ)\n Press <b>ENTER</b> to join, <b>ESC</b> to cancel.")

    def _render_qr_join(self):
        return HTML(" <header>Invite Join</header>\n\n Paste <b>http://.../invite</b> link here\n Press <b>ENTER</b> to confirm, <b>ESC</b> to cancel.")

    def _render_main(self):
        lines = ["<header>      DLM SHARE (Phase 2)</header>", ""]
        items = self._get_main_menu_items()
        for i, (act, label) in enumerate(items):
            style = "selected" if i == self.list_index else "default"
            lines.append(f" <{style}>{'>' if i == self.list_index else ' '} {label}</{style}>")
        if self.last_error: lines.append(f"\n <error>! {self.last_error}</error>")
        return HTML("\n".join(lines))

    def _render_lobby(self):
        room = self.room_manager.current_room
        if not room: return HTML("Error: Room lost")
        
        # --- HEADER / STATUS ---
        pending_count = len([f for f in self.queue.queue if f.status == "pending"])
        
        lines = [
            f" <header>DLM SHARE ROOM</header>  ID: <room-id>{room.room_id}</room-id>  |  TOKEN: <msg>{room.token}</msg>",
            f" <header>Invite:</header> <msg>http://{room.host_ip}:{room.port}/invite?t={room.token}</msg>",
            f" Status: <msg>Lobby Active</msg>  |  Queue: <msg>{pending_count}</msg>",
            " " + "─"*60
        ]
        
        # --- TOTAL PROGRESS (Unified) ---
        active_transfers = [
            d.current_transfer for d in room.devices 
            if d.current_transfer and (d.current_transfer.get('speed', 0) > 0 or d.current_transfer.get('progress', 0) > 0)
        ]
        if active_transfers:
            total_prog = sum(t.get('progress', 0) for t in active_transfers) / len(active_transfers)
            total_speed = sum(t.get('speed', 0.0) for t in active_transfers)
            
            width = 40
            filled = int(total_prog / 100 * width)
            bar = "█" * filled + "░" * (width - filled)
            lines.append(f" <header>TOTAL PROGRESS:</header> <msg>[{bar}] {total_prog:.1f}% ({total_speed:.2f} MB/s)</msg>")
            lines.append(" " + "─"*60)

        # Display Devices
        devices = room.devices
        lines.append(" <header>Connected Devices:</header>")
        if not devices:
            lines.append("  <i>No devices connected</i>")
        else:
            for d in devices:
                is_you = "(you)" in d.name
                style = "device-you" if is_you else ("device-active" if d.is_active() else "device-idle")
                active_mark = "●" if d.is_active() else "○"
                status_txt = f"[{d.state}]"
                lines.append(f"  <{style}>{active_mark} {d.name[:18]:<18} {status_txt:<12} {d.ip}</{style}>")
                
                if d.current_transfer and d.is_active():
                    t = d.current_transfer
                    prog = t.get('progress', 0)
                    speed = t.get('speed', 0.0)
                    
                    if speed > 0 or prog > 0:
                        name = t.get('name', 'file')
                        # Mini progress bar
                        width = 20
                        filled = int(prog / 100 * width)
                        bar = "█" * filled + "░" * (width - filled)
                        lines.append(f"      <msg>└ {name[:25]:<25} [{bar}] {prog:.1f}% ({speed:.1f} MB/s)</msg>")

        lines.append(" " + "─"*50)
        
        # --- ACTION MENU (Selectable) ---
        lines.append(" <header>Actions:</header>")
        
        actions = self._get_lobby_actions()
        for i, (act, label) in enumerate(actions):
            is_selected = (i == self.list_index)
            prefix = " > " if is_selected else "   "
            style = "selected" if is_selected else "default"
            lines.append(f" <{style}>{prefix}{label}</{style}>")
            
        if self.last_msg:
             lines.append(f"\n <msg>{self.last_msg}</msg>")
        if self.last_error:
             lines.append(f"\n <error>{self.last_error}</error>")
             
        return HTML("\n".join(lines))

    def _render_queue(self):
        lines = ["<header>TRANSFER QUEUE</header>", ""]
        if not self.queue:
            lines.append(" <i>Queue is empty. Use lobby to add files.</i>")
        else:
            for i, item in enumerate(self.queue.queue):
                style = "selected" if i == self.list_index else "default"
                status_color = "00ff00" if item.status == "completed" else ("ffaa00" if item.status == "transferring" else "ffffff")
                target_name = item.target_device_id
                if target_name != "ALL":
                    room = self.room_manager.current_room
                    if room:
                        dev = next((d for d in room.devices if d.device_id == target_name), None)
                        if dev: target_name = dev.name[:8]
                
                dir_mark = "[DIR] " if item.is_dir else ""
                lines.append(f" <{style}>{i+1}. {dir_mark}{item.file_name[:25]:<25} {item.file_size/1024/1024:>6.1f}MB  -> <msg>{target_name:<8}</msg> <text fg='#{status_color}'>{item.status}</text></{style}>")
        
        lines.append("")
        for i, (act, label) in enumerate(self._get_queue_actions()):
            idx = i + len(self.queue)
            style = "selected" if idx == self.list_index else "header"
            lines.append(f" <{style}>{'>' if idx == self.list_index else ' '} {label}</{style}>")
            
        return HTML("\n".join(lines))

    def run(self):
        self.refresh_thread.start()
        
        # Fixed layout: Use ConditionalContainer so the TextArea widget itself is in the layout tree
        input_area = ConditionalContainer(
            content=self.input_field,
            filter=Condition(lambda: self.screen in ("manual_join", "qr_join"))
        )
        
        layout = Layout(
            HSplit([
                self.body_window
            ])
        )
        
        app = Application(
            layout=layout,
            key_bindings=self.kb,
            style=self.style,
            full_screen=True,
            refresh_interval=0.5,
            mouse_support=False,  # Disable mouse to prevent cursor issues
            erase_when_done=True  # Clean terminal on exit
        )
        
        app.run()


def run_share_tui(bus):
    """Run share TUI with error reporting."""
    from dlm.share.room_manager import RoomManager
    
    try:
        # Get or create room manager
        if not hasattr(run_share_tui, '_room_manager'):
            run_share_tui._room_manager = RoomManager()
        
        tui = ShareTUI(run_share_tui._room_manager, bus)
        tui.run()
    except KeyboardInterrupt:
        # Phase 21 & 23: Global cleanup on exit
        try:
            if 'tui' in locals():
                tui.cleanup()
            else:
                rm = getattr(run_share_tui, '_room_manager', None)
                if rm and rm.current_room and rm.role == 'RECV':
                     from .client import ShareClient
                     client = ShareClient(bus)
                     client.leave_room(
                         rm.current_room.host_ip, 
                         rm.current_room.port, 
                         rm.current_room.token
                     )
        except: pass
        return
    except Exception as e:
        import traceback
        # Attempt cleanup even on crash
        try:
            if 'tui' in locals(): tui.cleanup()
        except: pass
        
        print(f"\n❌ \033[1;31mSHARE TUI CRASHED\033[0m")
        print(f"Error: {e}")
        # Write to a log file for diagnosis
        with open("dlm_share_error.log", "w") as f:
            f.write(traceback.format_exc())
        print("Detailed error saved to dlm_share_error.log")
        time.sleep(3)
