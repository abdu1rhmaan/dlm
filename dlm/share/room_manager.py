"""Room manager for share Phase 2."""

from typing import Optional
from .room import Room, Device
import socket
import platform
import uuid


class RoomManager:
    """Manages share room state and device identity."""
    
    def __init__(self):
        self.current_room: Optional[Room] = None
        self.device_id = self._generate_device_id()
        self.device_name = self._get_device_name()
    
    def _generate_device_id(self) -> str:
        """Generate unique device ID."""
        return str(uuid.uuid4())[:8]
    
    def _get_device_name(self) -> str:
        """Get friendly device name from hostname."""
        try:
            hostname = platform.node() or socket.gethostname()
            # Truncate to 20 chars for display
            return hostname[:20] if hostname else "Unknown"
        except:
            return "Unknown"
    
    def _get_local_ip(self) -> str:
        """Get the actual LAN IP address, filtering virtual adapters."""
        try:
            import psutil
            BLACKLIST = ['vbox', 'docker', 'virtual', 'wsl', 'tailscale', 'zerotier', 'vpn', 'vmnet']
            candidates = []
            
            for interface, addrs in psutil.net_if_addrs().items():
                if any(b in interface.lower() for b in BLACKLIST):
                    continue
                for addr in addrs:
                    if addr.family == 2:  # AF_INET
                        ip = addr.address
                        if ip.startswith('127.'): continue
                        
                        score = 70
                        if ip.startswith('192.168.1.') or ip.startswith('192.168.0.'): score = 100
                        elif ip.startswith('192.168.56.'): score = 10  # Deprioritize VBox
                        elif ip.startswith('192.168.'): score = 95
                        elif ip.startswith('10.'): score = 90
                        elif ip.startswith('172.'):
                            try:
                                second = int(ip.split('.')[1])
                                if 16 <= second <= 31: score = 80
                            except: pass
                        candidates.append((score, ip))
            
            if candidates:
                candidates.sort(reverse=True)
                return candidates[0][1]
        except:
            pass

        # Fallback to socket trick
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            pass
        
        return "127.0.0.1"
    
    def create_room(self, host_ip: str = None, port: int = 0) -> Room:
        """Create a new room and set as current."""
        if host_ip is None:
            host_ip = self._get_local_ip()
        
        room = Room(
            room_id=Room.generate_room_id(),
            token=Room.generate_token(),
            host_ip=host_ip,
            port=port,
            host_device_name=self.device_name,
            host_device_id=self.device_id
        )
        
        # Add self as first device
        self_device = Device(
            device_id=self.device_id,
            name=f"{self.device_name} (you)",
            ip=host_ip,
            state="idle"
        )
        room.add_device(self_device)
        
        self.current_room = room
        return room
    
    def join_room(self, room_id: str, ip: str, port: int, token: str) -> Room:
        """Join an existing room and set as current."""
        room = Room(
            room_id=room_id,
            token=token,
            host_ip=ip,
            port=port
        )
        
        # Add self as a device
        self_device = Device(
            device_id=self.device_id,
            name=f"{self.device_name} (you)",
            ip=self._get_local_ip(),
            state="idle"
        )
        room.add_device(self_device)
        
        self.current_room = room
        return room
    
    def leave_room(self):
        """Leave current room."""
        self.current_room = None
    
    def is_in_room(self) -> bool:
        """Check if currently in a room."""
        return self.current_room is not None and not self.current_room.is_expired()
    
    def get_self_device(self) -> Optional[Device]:
        """Get the device object representing this instance."""
        if not self.current_room:
            return None
        return self.current_room.get_device(self.device_id)
