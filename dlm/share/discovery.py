"""Device discovery for share rooms using mDNS/Zeroconf."""

from zeroconf import Zeroconf, ServiceBrowser, ServiceInfo, ServiceListener
import socket
import threading
import time
from typing import List, Dict, Optional, Callable
import logging

logger = logging.getLogger(__name__)


class RoomDiscovery:
    """Handles mDNS-based room discovery and advertisement."""
    
    SERVICE_TYPE = "_dlm-share._tcp.local."
    
    def __init__(self):
        self.zeroconf: Optional[Zeroconf] = None
        self.discovered_rooms: Dict[str, Dict] = {}
        self.browser: Optional[ServiceBrowser] = None
        self.service_info: Optional[ServiceInfo] = None
        self._lock = threading.Lock()
    
    def _get_local_ip(self) -> str:
        """Get local IP address."""
        try:
            import psutil
            for interface, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family == 2:  # AF_INET (IPv4)
                        ip = addr.address
                        if ip.startswith('192.168.') or ip.startswith('10.'):
                            return ip
                        elif ip.startswith('172.'):
                            try:
                                second_octet = int(ip.split('.')[1])
                                if 16 <= second_octet <= 31:
                                    return ip
                            except (ValueError, IndexError):
                                pass
        except ImportError:
            pass
        except Exception:
            pass
        
        # Fallback to socket trick
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith('192.168.') or ip.startswith('10.'):
                return ip
        except Exception:
            pass
        
        return "127.0.0.1"
    
    def advertise_room(self, room_id: str, token: str, port: int, device_id: str = "HOST") -> bool:
        """
        Advertise room on LAN via mDNS.
        
        Args:
            room_id: 4-character room ID
            token: XXX-XXX format token
            port: Server port
            device_id: Unique device ID
        
        Returns:
            True if advertisement successful, False otherwise
        """
        try:
            if not self.zeroconf:
                self.zeroconf = Zeroconf()
            
            hostname = socket.gethostname()
            local_ip = self._get_local_ip()
            
            # Create service info
            self.service_info = ServiceInfo(
                self.SERVICE_TYPE,
                f"{room_id}.{self.SERVICE_TYPE}",
                addresses=[socket.inet_aton(local_ip)],
                port=port,
                properties={
                    b'room_id': room_id.encode('utf-8'),
                    b'token': token.encode('utf-8'),
                    b'version': b'2.0',
                    b'hostname': hostname.encode('utf-8'),
                    b'device_id': device_id.encode('utf-8')
                },
                server=f"{hostname}.local."
            )
            
            self.zeroconf.register_service(self.service_info)
            logger.info(f"Room {room_id} advertised on {local_ip}:{port} (Device: {device_id})")
            return True
        
        except Exception as e:
            logger.error(f"Failed to advertise room: {e}")
            return False
    
    def scan_rooms(self, timeout: float = 3.0) -> List[Dict]:
        """
        Scan for available rooms on LAN.
        
        Args:
            timeout: Scan duration in seconds
        
        Returns:
            List of discovered rooms with room_id, token, ip, port
        """
        try:
            if not self.zeroconf:
                self.zeroconf = Zeroconf()
            
            with self._lock:
                self.discovered_rooms.clear()
            
            class RoomListener(ServiceListener):
                def __init__(self, discovery):
                    self.discovery = discovery
                
                def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                    """Called when a service is discovered."""
                    try:
                        info = zc.get_service_info(type_, name)
                        if info and info.addresses:
                            room_id = info.properties.get(b'room_id', b'').decode('utf-8')
                            token = info.properties.get(b'token', b'').decode('utf-8')
                            hostname = info.properties.get(b'hostname', b'Unknown').decode('utf-8')
                            device_id = info.properties.get(b'device_id', b'HOST').decode('utf-8')
                            
                            room_data = {
                                'room_id': room_id,
                                'token': token,
                                'ip': socket.inet_ntoa(info.addresses[0]),
                                'port': info.port,
                                'hostname': hostname,
                                'device_id': device_id,
                                'service_name': name
                            }
                            
                            with self.discovery._lock:
                                self.discovery.discovered_rooms[name] = room_data
                            
                            logger.info(f"Discovered room: {room_id} at {room_data['ip']}:{room_data['port']}")
                    except Exception as e:
                        logger.error(f"Error processing discovered service: {e}")
                
                def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                    """Called when a service is removed."""
                    with self.discovery._lock:
                        self.discovery.discovered_rooms.pop(name, None)
                    logger.info(f"Room removed: {name}")
                
                def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                    """Called when a service is updated."""
                    pass
            
            listener = RoomListener(self)
            self.browser = ServiceBrowser(self.zeroconf, self.SERVICE_TYPE, listener)
            
            # Wait for discovery
            time.sleep(timeout)
            
            with self._lock:
                rooms = list(self.discovered_rooms.values())
            
            logger.info(f"Scan complete. Found {len(rooms)} room(s)")
            return rooms
        
        except Exception as e:
            logger.error(f"Failed to scan for rooms: {e}")
            return []
    
    def stop_advertising(self):
        """Stop advertising the current room."""
        try:
            if self.service_info and self.zeroconf:
                self.zeroconf.unregister_service(self.service_info)
                self.service_info = None
                logger.info("Stopped advertising room")
        except Exception as e:
            logger.error(f"Error stopping advertisement: {e}")
    
    def stop(self):
        """Stop discovery and cleanup resources."""
        try:
            if self.browser:
                self.browser.cancel()
                self.browser = None
            
            self.stop_advertising()
            
            if self.zeroconf:
                self.zeroconf.close()
                self.zeroconf = None
            
            logger.info("Discovery stopped")
        except Exception as e:
            logger.error(f"Error stopping discovery: {e}")
    
    def __del__(self):
        """Cleanup on deletion."""
        self.stop()


# Convenience functions
def discover_rooms(timeout: float = 3.0) -> List[Dict]:
    """
    Quick function to discover rooms.
    
    Args:
        timeout: Scan duration in seconds
    
    Returns:
        List of discovered rooms
    """
    discovery = RoomDiscovery()
    try:
        return discovery.scan_rooms(timeout)
    finally:
        discovery.stop()
