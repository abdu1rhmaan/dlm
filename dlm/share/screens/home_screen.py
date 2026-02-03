from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Label, Button
from textual.containers import Vertical
import asyncio

class HomeScreen(Screen):
    """
    Home Screen Features:
    - Text-only Highlight
    - Looping Navigation
    - Real Networking Triggers
    """
    
    CSS = """
    HomeScreen {
        align: left top;
        padding: 1;
        background: transparent;
    }
    
    #title {
        margin-bottom: 1;
        color: green;
        text-style: bold;
    }

    #menu {
        width: auto;
        height: auto;
        align: left top;
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
    
    BUTTON_IDS = ["btn-create", "btn-join", "btn-exit"]

    def compose(self) -> ComposeResult:
        yield Label("DLM SHARE", id="title")
        with Vertical(id="menu"):
            yield Button("Create Room", id="btn-create")
            yield Button("Join Room",   id="btn-join")
            yield Button("Exit",        id="btn-exit")

    def on_mount(self) -> None:
        self.query_one("#btn-create").focus()

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
        if event.button.id == "btn-create":
            # Real Networking: Host Room
            await self.app.host_room()
        elif event.button.id == "btn-join":
            # Real Networking: Switch to Join Scanner
            self.app.switch_mode("join")
        elif event.button.id == "btn-exit":
            self.app.exit()
