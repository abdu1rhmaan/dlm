"""Room and Device models for share Phase 2."""

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime, timedelta
import secrets
import string


@dataclass
class Device:
    """Represents a device in a share room."""
    device_id: str
    name: str
    ip: str
    state: str = "idle"  # idle, sending, receiving
    last_seen: Optional[datetime] = None
    pending_transfers: List[dict] = field(default_factory=list) # Phase 2 Coordination
    current_transfer: Optional[dict] = None # {file_id, name, progress, speed, size}
    
    def is_active(self, timeout_seconds: int = 30) -> bool:
        """Check if device is still active (seen recently)."""
        # Treat (you) or the host as always active if it's the current session
        if "(you)" in self.name or self.device_id == "HOST":
            return True
        if not self.last_seen:
            return False
        return datetime.now() - self.last_seen < timedelta(seconds=timeout_seconds)
    
    def update_heartbeat(self):
        """Update last_seen timestamp."""
        self.last_seen = datetime.now()


@dataclass
class Room:
    """Represents a share room."""
    room_id: str
    token: str
    host_ip: str
    port: int
    host_device_name: str = "Host"
    host_device_id: str = "HOST"
    owner_device_id: str = "HOST" # Phase 16: Tracks authority for handover
    devices: List[Device] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    ttl: int = 86400  # 24 hours default
    
    @staticmethod
    def generate_room_id() -> str:
        """Generate a 4-character room ID (e.g., X7K2)."""
        chars = string.ascii_uppercase + string.digits
        return ''.join(secrets.choice(chars) for _ in range(4))
    
    @staticmethod
    def generate_token() -> str:
        """Generate a 6-character token in XXX-XXX format."""
        chars = string.ascii_uppercase + string.digits
        part1 = ''.join(secrets.choice(chars) for _ in range(3))
        part2 = ''.join(secrets.choice(chars) for _ in range(3))
        return f"{part1}-{part2}"
    
    def is_expired(self) -> bool:
        """Check if room has exceeded its TTL."""
        return datetime.now() - self.created_at > timedelta(seconds=self.ttl)
    
    def add_device(self, device: Device):
        """Add or update a device in the room."""
        # Remove existing device with same ID
        self.devices = [d for d in self.devices if d.device_id != device.device_id]
        device.update_heartbeat()
        self.devices.append(device)
    
    def remove_device(self, device_id: str):
        """Remove a device from the room."""
        self.devices = [d for d in self.devices if d.device_id != device_id]
    
    def get_device(self, device_id: str) -> Optional[Device]:
        """Get device by ID."""
        for device in self.devices:
            if device.device_id == device_id:
                return device
        return None
    
    def get_active_devices(self) -> List[Device]:
        """Get list of currently active devices."""
        return [d for d in self.devices if d.is_active()]
    
    def update_device_state(self, device_id: str, state: str):
        """Update device state."""
        device = self.get_device(device_id)
        if device:
            device.state = state
            device.update_heartbeat()

    def prune_stale_devices(self, timeout_seconds: int = 60) -> bool:
        """Remove devices not seen for a while. Returns True if any removed."""
        old_count = len(self.devices)
        self.devices = [d for d in self.devices if d.is_active(timeout_seconds)]
        return len(self.devices) != old_count
