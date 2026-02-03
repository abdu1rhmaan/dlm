from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Button, Label
from textual.containers import Vertical
from .sub_screens import QueueScreen, TransferScreen, QRScreen

class RoomScreen(Screen):
    """
    Room Screen Features:
    - Real-time Device List Listing
    - Real IP/Port Display
    """

    CSS = """
    RoomScreen {
        background: transparent;
        padding: 0;
    }

    #room-header {
        height: auto;
        padding-bottom: 1;
        color: green;
        text-style: bold;
    }

    #middle-section {
        height: auto;
        padding: 1;
        color: white;
        padding-bottom: 2; /* Space before menu */
    }

    #menu {
        width: auto;
        height: auto;
        align: left top;
        border-top: solid #333333; /* Separator */
    }

    Button {
        width: auto;
        min-width: 0;
        height: 1;
        border: none;
        margin-bottom: 0; 
        padding: 0 1;
        background: transparent;
        color: white;
        text-align: left;
    }

    Button:focus {
        background: white !important;
        color: black !important;
        text-style: bold;
    }
    """

    BUTTON_IDS = ["item-add", "item-queue", "item-trans", "item-qr", "item-leave"]

    def compose(self) -> ComposeResult:
        yield Static("ROOM: Loading...", id="room-header")
        yield Static("Waiting for devices...", id="middle-section")
        
        with Vertical(id="menu"):
            yield Button("Add Files",       id="item-add")
            yield Button("Queue Manager",   id="item-queue")
            yield Button("Transfer Status", id="item-trans")
            yield Button("Show QR Code",    id="item-qr")
            yield Button("Leave Room",      id="item-leave")

    def on_mount(self) -> None:
        self.query_one("#item-add").focus()
        
        # Subscribe to network updates
        self.app.net.on_device_list_update = self.update_device_list
        
        # Initial Info Update
        ip = self.app.net.host_ip
        port = self.app.net.tcp_port
        room = self.app.net.room_name or "Connected"
        self.query_one("#room-header").update(f"ROOM: {room}   [LAN: {ip}:{port}]")
        
        # Initial List Trigger
        if self.app.net.connected_devices:
            self.update_device_list(self.app.net.connected_devices)

    def update_device_list(self, devices):
        """Update the middle text area with list of devices."""
        lines = ["DEVICES:"]
        for d in devices:
            status = d.get('status', 'idle')
            lines.append(f"• {d['name']} ({status})") # e.g. "• User1 (idle)"
        
        lines.append("\n(Select actions below)")
        self.query_one("#middle-section").update("\n".join(lines))

    def on_key(self, event) -> None:
        if event.key == "down":
            self.cycle_focus(1)
            event.stop()
        elif event.key == "up":
            self.cycle_focus(-1)
            event.stop()

    def cycle_focus(self, direction: int) -> None:
        focused = self.query("Button:focus").first()
        if not focused: return
        current_id = focused.id
        if current_id not in self.BUTTON_IDS: return
        idx = self.BUTTON_IDS.index(current_id)
        new_idx = (idx + direction) % len(self.BUTTON_IDS)
        self.query_one(f"#{self.BUTTON_IDS[new_idx]}").focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        item_id = event.button.id
        
        if item_id == "item-add":
            pass
        elif item_id == "item-queue":
            self.app.push_screen(QueueScreen())
        elif item_id == "item-trans":
            self.app.push_screen(TransferScreen())
        elif item_id == "item-qr":
            self.app.push_screen(QRScreen())
        elif item_id == "item-leave":
            # Disconnect logic here
            await self.app.leave_room()
            # Reset UI
            self.query_one("#room-header").update("ROOM: Disconnected")
            self.query_one("#middle-section").update("Disconnected.")
