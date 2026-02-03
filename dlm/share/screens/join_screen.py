from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Label, Button, ListView, ListItem
from textual.containers import Vertical
from textual.reactive import reactive

class JoinScreen(Screen):
    """
    Screen to scan and list available rooms.
    Uses the same minimalist styling.
    """
    
    CSS = """
    JoinScreen {
        background: transparent;
        padding: 1;
    }

    #scan-status {
        color: yellow;
        margin-bottom: 1;
        text-style: italic;
    }

    /* List of Rooms (Buttons) */
    Button {
        width: auto;
        min-width: 0;
        height: 1;
        border: none;
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

    found_rooms = reactive([]) # List of dicts: {name, ip, port, host}

    def compose(self) -> ComposeResult:
        yield Label("SCANNING FOR ROOMS...", id="scan-status")
        with Vertical(id="room-list"):
            # Placeholder content initially
            yield Label("No rooms found yet...", id="placeholder", classes="dim")
        
        yield Label("\n[Press Escape to Cancel]", classes="dim")

    def on_mount(self) -> None:
        # Start scanning via App's NetworkManager
        self.app.start_scanning(self.on_room_found)

    def on_room_found(self, room_data):
        """Callback when a room beacon is received."""
        # Avoid duplicates
        for r in self.found_rooms:
            if r['ip'] == room_data['ip'] and r['port'] == room_data['port']:
                return
        
        self.found_rooms.append(room_data)
        self.rebuild_list()

    def rebuild_list(self):
        """Update the UI with found rooms."""
        container = self.query_one("#room-list")
        container.remove_children()
        
        if not self.found_rooms:
            container.mount(Label("No rooms found yet...", id="placeholder"))
            return

        for idx, room in enumerate(self.found_rooms):
            # Format: "Room Name (Host) - IP"
            label = f"{room['room']} ({room['host']}) - {room['ip']}"
            btn = Button(label, id=f"room-{idx}")
            btn.room_data = room # Attach data to button
            container.mount(btn)
        
        # Focus first if just added
        if len(self.found_rooms) == 1:
            self.query("Button").first().focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if getattr(event.button, "room_data", None):
            room = event.button.room_data
            connected = await self.app.join_room(room['ip'], room['port'])
            if connected:
                await self.app.switch_mode("room")
                # Now self.app.screen is the RoomScreen
                if hasattr(self.app.screen, "init_room_state"):
                     self.app.screen.init_room_state()
            else:
                self.query_one("#scan-status").update("CONNECTION FAILED!")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.switch_mode("home")
