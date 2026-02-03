from textual.app import App
from .screens.home_screen import HomeScreen
from .screens.room_screen import RoomScreen
from .screens.join_screen import JoinScreen
from .networking import NetworkManager
import asyncio

class DlmShareApp(App):
    """The main application class for dlm share."""
    
    CSS = """
    Screen {
        layers: base;
    }
    .dim {
        color: #666666;
    }
    """

    MODES = {
        "home": HomeScreen,
        "room": RoomScreen,
        "join": JoinScreen
    }

    def __init__(self):
        super().__init__()
        self.net = NetworkManager(username=self._get_username())

    def _get_username(self):
        import os
        return os.environ.get("USERNAME", "User")

    def on_mount(self) -> None:
        self.switch_mode("home")

    async def on_shutdown(self) -> None:
        await self.net.shutdown()

    # --- Networking Actions ---

    async def host_room(self):
        """Start hosting and switch to room."""
        await self.net.shutdown() # Ensure clean slate
        await self.net.start_host(room_name="DLM Room")
        self.switch_mode("room")
    async def leave_room(self):
        """Disconnect and return home."""
        await self.net.shutdown()
        self.switch_mode("home")
    
    def start_scanning(self, callback):
        """Start UDP listener."""
        self.net.on_room_found = callback
        asyncio.create_task(self.net.start_client_scan())

    async def join_room(self, ip, port):
        """Connect to a room."""
        return await self.net.connect_to_room(ip, port)
