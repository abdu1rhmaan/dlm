from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Label, Static

class BaseSubScreen(Screen):
    """Base class for sub-screens with transparent background."""
    CSS = """
    BaseSubScreen {
        background: transparent;
    }
    
    #header {
        dock: top;
        height: 1;
        width: 100%;
        color: green;
        text-style: bold;
    }

    #content {
        padding: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label(self.TITLE, id="header")
        yield Static(self.CONTENT, id="content")
        yield Label("\n[Press Escape to Back]", id="all")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()

class QueueScreen(BaseSubScreen):
    TITLE = "QUEUE MANAGER"
    CONTENT = "No files in queue."

class TransferScreen(BaseSubScreen):
    TITLE = "TRANSFER STATUS"
    CONTENT = "No active transfers."

class QRScreen(BaseSubScreen):
    TITLE = "ROOM QR CODE"
    CONTENT = """
    [QR CODE PLACEHOLDER]
    
    IP: 192.168.1.100
    Port: 9090
    """
