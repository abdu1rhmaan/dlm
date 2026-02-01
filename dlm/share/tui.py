"""Interactive TUI for share rooms using prompt_toolkit."""

from prompt_toolkit import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, FormattedTextControl, Dimension
from prompt_toolkit.widgets import Frame, Label, Button, RadioList, CheckboxList, TextArea, Box
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML, FormattedText
from prompt_toolkit.styles import Style
from typing import List, Optional, Callable, Any
import asyncio
import threading
from pathlib import Path

from .room_manager import RoomManager
from .discovery import RoomDiscovery
from .qr import generate_room_qr
from .server import ShareServer
from .client import ShareClient
from .queue import TransferQueue
from .picker import pick_file


class ShareTUI:
    """Interactive TUI for share rooms."""
    
    def __init__(self, room_manager: RoomManager, bus):
        self.room_manager = room_manager
        self.bus = bus
        self.discovery = RoomDiscovery()
        self.server: Optional[ShareServer] = None
        self.client: Optional[ShareClient] = None
        self.transfer_queue: Optional[TransferQueue] = None
        
        # TUI state
        self.current_screen = "main_menu"
        self.selected_files: List[Path] = []
        self.selected_devices: List[str] = []
        self.discovered_rooms: List[dict] = []
        
        # Style
        self.style = Style.from_dict({
            'title': '#00ff00 bold',
            'subtitle': '#00aaff',
            'info': '#aaaaaa',
            'warning': '#ffaa00',
            'error': '#ff0000',
            'success': '#00ff00',
            'border': '#444444',
        })
    
    def run(self):
        """Main TUI entry point."""
        if self.room_manager.is_in_room():
            self._show_room_lobby()
        else:
            self._show_main_menu()
    
    def _show_main_menu(self):
        """Show create/join menu."""
        options = [
            ('create', 'Create Room'),
            ('scan', 'Scan for Rooms'),
            ('join', 'Join Room Manually'),
            ('exit', 'Exit')
        ]
        
        result = self._radio_list_dialog(
            title="DLM Share - Main Menu",
            text="Select an option:",
            values=options
        )
        
        if result == 'create':
            self._create_room()
        elif result == 'scan':
            self._scan_and_join()
        elif result == 'join':
            self._join_room_manual()
        # exit or None - return
    
    def _create_room(self):
        """Create a new room."""
        # Create room
        room = self.room_manager.create_room()
        
        # Start server in background
        self.server = ShareServer(room=room, port=room.port, bus=self.bus)
        # Use run_server for non-blocking threaded execution
        server_thread = threading.Thread(target=self.server.run_server, daemon=True)
        server_thread.start()
        
        # Wait a bit for server to start before joining
        import time
        time.sleep(0.5)
        
        # Creator also creates a client to talk to its own server (for coordination)
        self.client = ShareClient(self.bus)
        self.client.join_room(
            ip=room.host_ip,
            port=room.port,
            token=room.token,
            device_name=self.room_manager.device_name
        )
        
        # Advertise room
        self.discovery.advertise_room(room.room_id, room.token, room.port)
        
        # Show success message
        self._message_dialog(
            title="Room Created",
            text=f"""
Room ID: {room.room_id}
Token: {room.token}
IP: {room.host_ip}:{room.port}

Room is now discoverable on LAN.
Press Enter to continue to Room Lobby.
""",
            style_class='success'
        )
        
        # Show room lobby
        self._show_room_lobby()
    
    def _scan_and_join(self):
        """Scan for rooms and show selection."""
        # Show scanning message
        self._message_dialog(
            title="Scanning",
            text="Scanning for rooms on LAN...\nThis may take a few seconds.",
            duration=0.5
        )
        
        # Scan for rooms
        rooms = self.discovery.scan_rooms(timeout=3.0)
        
        if not rooms:
            result = self._yes_no_dialog(
                title="No Rooms Found",
                text="No rooms were discovered on the network.\n\nWould you like to join manually?"
            )
            if result:
                self._join_room_manual()
            return
        
        # Show discovered rooms
        options = [
            (room, f"{room['room_id']} - {room['hostname']} ({room['ip']}:{room['port']})")
            for room in rooms
        ]
        
        selected = self._radio_list_dialog(
            title="Available Rooms",
            text=f"Found {len(rooms)} room(s). Select one to join:",
            values=options
        )
        
        if selected:
            self._join_room_auto(selected)
    
    def _join_room_auto(self, room_data: dict):
        """Join a room using discovered data."""
        # Join room in manager
        room = self.room_manager.join_room(
            room_id=room_data['room_id'],
            ip=room_data['ip'],
            port=room_data['port'],
            token=room_data['token']
        )
        
        # Create client and connect
        self.client = ShareClient(self.bus)
        success = self.client.join_room(
            ip=room_data['ip'],
            port=room_data['port'],
            token=room_data['token'],
            device_name=self.room_manager.device_name
        )
        
        if success:
            self._message_dialog(
                title="Joined Room",
                text=f"Successfully joined room {room_data['room_id']}!",
                style_class='success'
            )
            self._show_room_lobby()
        else:
            self._message_dialog(
                title="Join Failed",
                text="Failed to join room. Please try again.",
                style_class='error'
            )
    
    def _join_room_manual(self):
        """Manual room join with IP/port/token input."""
        # Get IP
        ip = self._input_dialog(
            title="Join Room",
            text="Enter room IP address:",
            default="192.168.1."
        )
        if not ip:
            return
        
        # Get port
        port_str = self._input_dialog(
            title="Join Room",
            text="Enter port:",
            default="8080"
        )
        if not port_str:
            return
        
        try:
            port = int(port_str)
        except ValueError:
            self._message_dialog(
                title="Error",
                text="Invalid port number.",
                style_class='error'
            )
            return
        
        # Get token
        token = self._input_dialog(
            title="Join Room",
            text="Enter token (XXX-XXX):",
            default=""
        )
        if not token:
            return
        
        # Join room
        room_data = {
            'room_id': 'MANUAL',
            'ip': ip,
            'port': port,
            'token': token
        }
        self._join_room_auto(room_data)
    
    def _show_room_lobby(self):
        """Show room lobby with devices and actions."""
        while self.room_manager.is_in_room():
            room = self.room_manager.current_room
            devices = room.get_active_devices()
            
            # Build device list text
            device_lines = []
            for d in devices:
                status_icon = "●" if d.is_active() else "○"
                is_you = " (you)" if d.device_id == self.room_manager.device_id else ""
                device_lines.append(f"  {status_icon} {d.name:<25} {d.state:<12} {d.ip}{is_you}")
            
            device_text = "\n".join(device_lines) if device_lines else "  No devices"
            
            # Build info text
            info_text = f"""
Room ID: {room.room_id}
Token: {room.token}
Address: {room.host_ip}:{room.port}

Connected Devices ({len(devices)}):
{device_text}
"""
            
            # Show menu
            options = [
                ('send', 'Send Files'),
                ('refresh', 'Refresh Device List'),
                ('qr', 'Show QR Code'),
                ('leave', 'Leave Room')
            ]
            
            result = self._radio_list_dialog(
                title="Room Lobby",
                text=info_text,
                values=options
            )
            
            if result == 'send':
                self._send_files_flow()
            elif result == 'refresh':
                continue  # Loop will refresh
            elif result == 'qr':
                self._show_qr_code()
            elif result == 'leave' or result is None:
                self.room_manager.leave_room()
                self.discovery.stop()
                if self.server:
                    # Server cleanup would happen here
                    pass
                break
    
    def _send_files_flow(self):
        """Complete send files flow: select files -> select devices -> transfer."""
        # Step 1: Select files
        files = self._select_files()
        if not files:
            return
        
        # Step 2: Select target devices
        targets = self._select_devices()
        if not targets:
            return
        
        # Step 3: Confirm and transfer
        file_list = "\n".join([f"  • {f.name}" for f in files])
        device_names = []
        room = self.room_manager.current_room
        for target_id in targets:
            device = room.get_device(target_id)
            if device:
                device_names.append(device.name)
        
        device_list = "\n".join([f"  • {name}" for name in device_names])
        
        confirm = self._yes_no_dialog(
            title="Confirm Transfer",
            text=f"""
Send {len(files)} file(s) to {len(targets)} device(s)?

Files:
{file_list}

Devices:
{device_list}
"""
        )
        
        if confirm:
            self._execute_transfer(files, targets)
    
    def _select_files(self) -> List[Path]:
        """Multi-select file picker."""
        # Use existing file picker or create simple list
        try:
            # Try to use existing picker
            file_path = pick_file()
            if file_path:
                return [Path(file_path)]
        except:
            pass
        
        # Fallback: simple current directory listing
        cwd = Path.cwd()
        files = [f for f in cwd.iterdir() if f.is_file()]
        
        if not files:
            self._message_dialog(
                title="No Files",
                text="No files found in current directory.",
                style_class='warning'
            )
            return []
        
        options = [(f, f.name) for f in files[:50]]  # Limit to 50 files
        
        selected = self._checkbox_list_dialog(
            title="Select Files",
            text="Use SPACE to select, ENTER to confirm:",
            values=options
        )
        
        return selected if selected else []
    
    def _select_devices(self) -> List[str]:
        """Multi-select device picker."""
        room = self.room_manager.current_room
        devices = room.get_active_devices()
        
        # Filter out self
        other_devices = [d for d in devices if d.device_id != self.room_manager.device_id]
        
        if not other_devices:
            self._message_dialog(
                title="No Devices",
                text="No other devices in room.",
                style_class='warning'
            )
            return []
        
        # Build options
        options = [
            (d.device_id, f"{d.name} ({d.ip}) - {d.state}")
            for d in other_devices
        ]
        
        # Add "All Devices" option
        options.insert(0, ('ALL', 'All Devices'))
        
        selected = self._checkbox_list_dialog(
            title="Select Devices",
            text="Use SPACE to select, ENTER to confirm:",
            values=options
        )
        
        if not selected:
            return []
        
        # If "ALL" selected, return all device IDs
        if 'ALL' in selected:
            return [d.device_id for d in other_devices]
        
        return selected
    
    def _execute_transfer(self, files: List[Path], targets: List[str]):
        """Execute file transfer through coordination."""
        if not self.client:
            self._message_dialog(title="Error", text="No active connection.")
            return

        # 1. Prepare files
        from .models import FileEntry
        file_entries = [FileEntry.from_path(str(p)) for p in files]
        
        # 2. Ensure server is running (to serve the files)
        if not self.server:
            self.server = ShareServer(file_entries=file_entries, bus=self.bus, room=self.room_manager.current_room)
            self.server.prepare() # Get port
            
            server_thread = threading.Thread(target=self.server.run_server, daemon=True)
            server_thread.start()
        else:
            # Already have server (host), ensure it knows these files
            for fe in file_entries:
                if not any(f.file_id == fe.file_id for f in self.server.file_entries):
                    self.server.file_entries.append(fe)

        # 3. Tell room host to coordinate
        # Files data for the queue endpoint
        files_data = [{"file_id": f.file_id, "name": f.name, "size": f.size_bytes} for f in file_entries]
        
        success = self.client.queue_transfer(targets, files_data)
        
        if success:
             self._show_transfer_monitoring(targets)
        else:
             self._message_dialog(
                 title="Error",
                 text="Failed to coordinate with room host. Check connection.",
                 style_class='error'
             )
    
    def _show_transfer_monitoring(self, targets: List[str]):
        """Real-time transfer monitor for specific targets."""
        while True:
            # Use client to get fresh room info from host
            room_info = self.client.get_room_info()
            if not room_info:
                self._message_dialog(title="Error", text="Lost connection to room host.")
                break
                
            devices = room_info.get('devices', [])
            active_lines = []
            
            for d in devices:
                # Show if it's one of our targets OR if it's currently receiving something
                if d['device_id'] in targets or (d['current_transfer'] and d['state'] != "idle"):
                    name = d['name'][:15]
                    status = d['state']
                    prog_str = "0%"
                    speed_str = "0 B/s"
                    file_name = "-"
                    
                    if d['current_transfer']:
                        t = d['current_transfer']
                        file_name = t['name'][:20]
                        prog_str = f"{t['progress']:>.1f}%"
                        speed_str = self._format_speed(t['speed'])
                    
                    active_lines.append(f"  {name:<15} {file_name:<20} {prog_str:<8} {speed_str:<10} {status}")

            if not active_lines:
                # If we expect targets but none are active, they might be joining still
                active_lines = ["  No active transfers. (Waiting for devices to start downloading...)"]
                
            monitor_text = f"  ID: {room_info['room_id']} | HOST: {room_info['host_ip']}\n\n"
            monitor_text += "  DEVICE          FILE                 PROGRESS SPEED      STATUS\n"
            monitor_text += "  " + "-" * 70 + "\n"
            monitor_text += "\n".join(active_lines)
            
            options = [
                ('refresh', 'Refresh (Update Stats)'),
                ('cancel_all', 'Cancel All (This Room)'),
                ('back', 'Return to Lobby')
            ]
            
            result = self._radio_list_dialog(
                title="Transfer Monitor",
                text=monitor_text,
                values=options
            )
            
            if result == 'refresh':
                continue
            elif result == 'cancel_all':
                confirm = self._yes_no_dialog(title="Confirm", text="Stop all active and queued transfers for these targets?")
                if confirm:
                    for tid in targets:
                        self.client.control_transfer("cancel", tid)
            elif result == 'back' or result is None:
                break

    def _format_speed(self, speed_bytes_sec: float) -> str:
        """Format speed bytes/sec to human readable."""
        if speed_bytes_sec < 1024:
            return f"{speed_bytes_sec:.0f} B/s"
        elif speed_bytes_sec < 1024 * 1024:
            return f"{speed_bytes_sec / 1024:.1f} KB/s"
        else:
            return f"{speed_bytes_sec / (1024 * 1024):.1f} MB/s"
    
    def _show_transfer_progress(self):
        """Show transfer progress screen."""
        # This would integrate with existing ProgressManager
        # For now, show a simple message
        self._message_dialog(
            title="Transfer Started",
            text=f"""
Transfer initiated for {len(self.transfer_queue)} file(s).

Progress will be shown in the main DLM interface.
Press Enter to return to room lobby.
""",
            style_class='info'
        )
    
    def _show_qr_code(self):
        """Show QR code for room."""
        room = self.room_manager.current_room
        qr_text = generate_room_qr(
            room.room_id,
            room.host_ip,
            room.port,
            room.token
        )
        
        self._message_dialog(
            title="Room QR Code",
            text=qr_text,
            style_class='info'
        )
    
    # Dialog helper methods
    def _radio_list_dialog(self, title: str, text: str, values: List[tuple]) -> Any:
        """Show radio list dialog and return selected value."""
        from prompt_toolkit.shortcuts import radiolist_dialog
        
        return radiolist_dialog(
            title=title,
            text=text,
            values=values,
            style=self.style
        ).run()
    
    def _checkbox_list_dialog(self, title: str, text: str, values: List[tuple]) -> List[Any]:
        """Show checkbox list dialog and return selected values."""
        from prompt_toolkit.shortcuts import checkboxlist_dialog
        
        result = checkboxlist_dialog(
            title=title,
            text=text,
            values=values,
            style=self.style
        ).run()
        
        return result if result else []
    
    def _input_dialog(self, title: str, text: str, default: str = "") -> Optional[str]:
        """Show input dialog and return entered text."""
        from prompt_toolkit.shortcuts import input_dialog
        
        return input_dialog(
            title=title,
            text=text,
            default=default,
            style=self.style
        ).run()
    
    def _message_dialog(self, title: str, text: str, style_class: str = 'info', duration: Optional[float] = None):
        """Show message dialog."""
        from prompt_toolkit.shortcuts import message_dialog
        
        if duration:
            # Show for specific duration
            import time
            threading.Thread(
                target=lambda: (time.sleep(duration)),
                daemon=True
            ).start()
        
        message_dialog(
            title=title,
            text=text,
            style=self.style
        ).run()
    
    def _yes_no_dialog(self, title: str, text: str) -> bool:
        """Show yes/no dialog and return result."""
        from prompt_toolkit.shortcuts import yes_no_dialog
        
        return yes_no_dialog(
            title=title,
            text=text,
            style=self.style
        ).run()


# Convenience function
def run_share_tui(bus):
    """Run share TUI."""
    from .room_manager import RoomManager
    
    # Get or create room manager
    if not hasattr(run_share_tui, '_room_manager'):
        run_share_tui._room_manager = RoomManager()
    
    tui = ShareTUI(run_share_tui._room_manager, bus)
    tui.run()
