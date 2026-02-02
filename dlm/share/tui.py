"""Interactive TUI for share rooms using prompt_toolkit."""

import asyncio
import threading
import time
from pathlib import Path
from typing import List, Optional, Callable, Any, Dict

from prompt_toolkit import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, FormattedTextControl, Dimension
from prompt_toolkit.widgets import Frame, Label, Button, RadioList, Box, TextArea
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
        self.last_error = None
        self.last_msg = None
        
        # Data
        self.discovered_rooms = []
        self.targeting_item_index = -1
        
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

        @self.kb.add('q')
        @self.kb.add('esc')
        def _(event):
            if event.app.layout.has_focus(self.input_field):
                event.app.layout.focus(Window(content=FormattedTextControl(self._get_content))) # Focus back to main area
                return
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
        Application.get_current().layout.focus(Window(content=FormattedTextControl(self._get_content))) 

    # Actions at bottom of lists
    def _get_queue_actions(self):
        return [
            ("send", "[S] Send All Pending"),
            ("clear", "[C] Clear Completed"),
            ("back", "[B] Back to Lobby")
        ]

    def _get_current_list_count(self) -> int:
        if self.screen == "main":
            return len(self._get_main_menu_items())
        elif self.screen == "lobby":
            return len(self.room_manager.current_room.devices) + len(self._get_lobby_actions())
        elif self.screen == "queue":
            return len(self.queue) + len(self._get_queue_actions())
        elif self.screen == "scan_results":
            return len(self.discovered_rooms)
        return 0

    def _get_main_menu_items(self):
        return [
            ("create", "Create Room (Host)"),
            ("scan", "Scan for Rooms"),
            ("join", "Join Manually (IP/Token)"),
            ("qr", "Join via QR Paste"),
            ("exit", "Exit")
        ]

    def _get_lobby_actions(self):
        return [
            ("add", "[+] Add Files/Folders"),
            ("queue", "[Q] Manage Queue / Send"),
            ("qr", "[S] Show Room QR"),
            ("refresh", "[R] Refresh List"),
            ("leave", "[L] Leave Room")
        ]

    def _handle_enter(self):
        if self.screen == "main":
            items = self._get_main_menu_items()
            action = items[self.list_index][0]
            if action == "create": self._create_room()
            elif action == "scan": self._scan_rooms()
            elif action == "join": 
                self.screen = "manual_join"
                Application.get_current().layout.focus(self.input_field)
            elif action == "qr": 
                self.screen = "qr_join"
                Application.get_current().layout.focus(self.input_field)
            elif action == "exit": self.running = False; Application.get_current().exit()
        
        elif self.screen == "scan_results":
            if self.discovered_rooms and self.list_index < len(self.discovered_rooms):
                room = self.discovered_rooms[self.list_index]
                self._do_join(room['ip'], room['port'], room['token'])

        elif self.screen == "lobby":
            devices = self.room_manager.current_room.devices
            if self.list_index < len(devices):
                pass
            else:
                action_idx = self.list_index - len(devices)
                action = self._get_lobby_actions()[action_idx][0]
                if action == "add": self._add_files()
                elif action == "queue": self.screen = "queue"; self.list_index = 0
                elif action == "qr": self._show_qr()
                elif action == "refresh": pass
                elif action == "leave": self._leave_room()

        elif self.screen == "queue":
            if self.list_index < len(self.queue):
                # Toggle skip/remove? 
                item = self.queue.queue[self.list_index]
                if item.status == "pending":
                    self.queue.remove_item(self.list_index)
            else:
                action_idx = self.list_index - len(self.queue)
                action = self._get_queue_actions()[action_idx][0]
                if action == "send": self._execute_queue_transfer()
                elif action == "clear": self.queue.clear_completed()
                elif action == "back": self.screen = "lobby"; self.list_index = 0

    def _execute_queue_transfer(self):
        """Start transferring all pending items in queue."""
        if not self.client or not self.queue:
            return
            
        # 1. Prepare server with all queued files
        if self.server:
            for item in self.queue.queue:
                if item.status == "pending":
                    fe = FileEntry.from_path(str(item.file_path))
                    if not any(f.file_id == fe.file_id for f in self.server.file_entries):
                        self.server.file_entries.append(fe)
        
        # 2. Coordinate with host
        # For simplicity, we broadcast to all in room for now
        targets = [d.device_id for d in self.room_manager.current_room.devices if "(you)" not in d.name]
        files_data = []
        for item in self.queue.queue:
            if item.status == "pending":
                fe = FileEntry.from_path(str(item.file_path))
                files_data.append({"file_id": fe.file_id, "name": fe.name, "size": fe.size_bytes})
                item.status = "transferring"
        
        if targets and files_data:
            self.client.queue_transfer(targets, files_data)
            self.last_msg = "Transfer started for all queued files!"
            self.screen = "lobby"; self.list_index = 0
        else:
            self.last_error = "No targets or files to send."

    def _handle_back(self, event):
        if self.screen == "main":
             self.running = False
             event.app.exit()
        elif self.screen == "lobby":
             self._leave_room()
        elif self.screen == "queue":
             self.screen = "lobby"; self.list_index = 0
        elif self.screen == "qr_join":
             self.screen = "main"; self.list_index = 0

    def _refresh_loop(self):
        while self.running:
            if self.screen == "lobby" and self.client:
                # Poll host for updates
                self.client.get_room_info() 
            elif self.screen == "scanning":
                # Discovery scan
                pass
            time.sleep(2)

    def _create_room(self):
        """Create a new room and start server."""
        room = self.room_manager.create_room()
        # Pass empty list initially, will add as files are queued for send
        self.server = ShareServer(room=room, port=room.port, bus=self.bus, file_entries=[])
        threading.Thread(target=self.server.run_server, daemon=True).start()
        
        # Wait for port
        for _ in range(10):
            if self.server.port: break
            time.sleep(0.1)
            
        self.client = ShareClient(self.bus)
        self.client.join_room(room.host_ip, self.server.port, room.token, self.room_manager.device_name)
        self.discovery.advertise_room(room.room_id, room.token, self.server.port, self.room_manager.device_id)
        
        self.screen = "lobby"
        self.list_index = len(room.devices)

    def _add_files(self):
        """Invoke ranger file picker."""
        def on_add(path: Path) -> int:
            count = self.queue.add_path(path)
            self.last_msg = f"✔ Added to queue: {path.name} ({count} items)"
            return count
        launch_picker(on_add)

    def _show_qr(self):
        """Placeholder for showing QR within TUI."""
        # For now, we set a temporary message or a dedicated screen
        room = self.room_manager.current_room
        self.last_msg = f"QR Info: {room.room_id} | {room.token} | {room.host_ip}"
        # generate_room_qr could be displayed in a full-screen mode later

    # --- Rendering ---

    def _get_content(self):
        if self.screen == "main": return self._render_main()
        elif self.screen == "lobby": return self._render_lobby()
        elif self.screen == "queue": return self._render_queue()
        elif self.screen == "scanning": return HTML(" <header>Scanning for rooms...</header>\n\n Please wait (3s)...")
        elif self.screen == "scan_results": return self._render_scan_results()
        elif self.screen == "manual_join": return self._render_manual_join()
        elif self.screen == "qr_join": return HTML(" <header>Join via QR</header>\n\n [Coming soon: Paste QR URI here]")
        return HTML("Loading...")

    def _render_scan_results(self):
        lines = [HTML("<header>Discovered Rooms</header>"), ""]
        if not self.discovered_rooms:
            lines.append(HTML(" No rooms found. Press 'q' to go back."))
        else:
            for i, r in enumerate(self.discovered_rooms):
                style = "selected" if i == self.list_index else "default"
                lines.append(HTML(f" <{style}>{'>' if i == self.list_index else ' '} {r['room_id']} - {r['hostname']} ({r['ip']})</{style}>"))
        return HTML("\n".join(lines))

    def _render_manual_join(self):
        return HTML(" <header>Manual Join</header>\n\n Enter <b>IP:PORT TOKEN</b> (e.g. 192.168.1.5:8080 ABC-XYZ)\n Press <b>ENTER</b> to join, <b>ESC</b> to cancel.")

    def _render_qr_join(self):
        return HTML(" <header>Join via QR</header>\n\n Paste <b>dlm://share...</b> URI here\n Press <b>ENTER</b> to confirm, <b>ESC</b> to cancel.")

    def _render_main(self):
        lines = [HTML("<header>      DLM SHARE (Phase 2)</header>"), ""]
        items = self._get_main_menu_items()
        for i, (act, label) in enumerate(items):
            style = "selected" if i == self.list_index else "default"
            lines.append(HTML(f" <{style}>{'>' if i == self.list_index else ' '} {label}</{style}>"))
        if self.last_error: lines.append(HTML(f"\n <error>! {self.last_error}</error>"))
        return HTML("\n".join(lines))

    def _render_lobby(self):
        room = self.room_manager.current_room
        if not room: return HTML("Error: Room lost")
        
        lines = [
            HTML(f" <header>ROOM:</header> <room-id>{room.room_id}</room-id>  |  TOKEN: <msg>{room.token}</msg>"),
            HTML(f" HOST: {room.host_ip}:{room.port}"),
            ""
        ]
        
        # Display Devices
        devices = room.devices
        for i, d in enumerate(devices):
            style = "selected" if i == self.list_index else ("device-you" if "(you)" in d.name else "device-idle")
            active_mark = "●" if d.is_active() else "○"
            lines.append(HTML(f" <{style}>{active_mark} {d.name[:20]:<20} {d.state:<10} {d.ip}</{style}>"))
            if d.current_transfer:
                t = d.current_transfer
                lines.append(HTML(f"    <msg>└ Sending: {t['name'][:30]} ({t['progress']:.1f}%)</msg>"))

        lines.append("")
        
        # Actions
        actions = self._get_lobby_actions()
        for i, (act, label) in enumerate(actions):
            idx = i + len(devices)
            style = "selected" if idx == self.list_index else "header"
            lines.append(HTML(f" <{style}>{'>' if idx == self.list_index else ' '} {label}</{style}>"))
            
        if self.last_msg:
             lines.append(HTML(f"\n <msg>{self.last_msg}</msg>"))
             
        return HTML("\n".join(lines))

    def _render_queue(self):
        lines = [HTML("<header>TRANSFER QUEUE</header>"), ""]
        if not self.queue:
            lines.append(HTML(" <i>Queue is empty. Use lobby to add files.</i>"))
        else:
            for i, item in enumerate(self.queue.queue):
                style = "selected" if i == self.list_index else "default"
                status_color = "00ff00" if item.status == "completed" else ("ffaa00" if item.status == "transferring" else "ffffff")
                lines.append(HTML(f" <{style}>{i+1}. {item.file_name[:40]:<40} {item.file_size/1024/1024:>6.1f}MB  <text fg='#{status_color}'>{item.status}</text></{style}>"))
        
        lines.append("")
        for i, (act, label) in enumerate(self._get_queue_actions()):
            idx = i + len(self.queue)
            style = "selected" if idx == self.list_index else "header"
            lines.append(HTML(f" <{style}>{'>' if idx == self.list_index else ' '} {label}</{style}>"))
            
        return HTML("\n".join(lines))

    def run(self):
        self.refresh_thread.start()
        
        body = Window(content=FormattedTextControl(self._get_content), height=Dimension(min=10))
        input_window = Window(content=self.input_field.control, height=1, style='class:input-field',
                             get_height=lambda: 1 if self.screen in ("manual_join", "qr_join") else 0)
        
        layout = Layout(
            HSplit([
                body,
                input_window,
                Window(content=FormattedTextControl(HTML("\n <footer>Arrows: Navigate | Enter: Select | q: Back/Exit</footer>")), height=2)
            ])
        )
        
        app = Application(
            layout=layout,
            key_bindings=self.kb,
            style=self.style,
            full_screen=True,
            refresh_interval=0.5
        )
        
        app.run()


def run_share_tui(bus):
    """Run share TUI."""
    from dlm.share.room_manager import RoomManager
    
    # Get or create room manager
    if not hasattr(run_share_tui, '_room_manager'):
        run_share_tui._room_manager = RoomManager()
    
    tui = ShareTUI(run_share_tui._room_manager, bus)
    tui.run()
