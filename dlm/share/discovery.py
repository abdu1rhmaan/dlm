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
        """Get best local LAN IP."""
        excluded_subnets = ["192.168.56."] # VirtualBox
        excluded_ifaces = ["docker", "vbox", "vmware", "wsl", "v-ethernet", "virbr"]
        
        candidates = []
        try:
            import psutil
            for iface, addrs in psutil.net_if_addrs().items():
                # Filter by interface name
                if any(x in iface.lower() for x in excluded_ifaces):
                    continue
                    
                for addr in addrs:
                    if addr.family == 2:  # AF_INET
                        ip = addr.address
                        if ip == "127.0.0.1": continue
                        if ip.startswith("169.254"): continue
                        if any(ip.startswith(ex) for ex in excluded_subnets): continue
                        
                        candidates.append(ip)
        except:
            pass

        # Fallback using socket if psutil failed or yielded nothing
        if not candidates:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # Doesn't actually connect, just picks interface
                s.connect(("8.8.8.8", 80)) 
                ip = s.getsockname()[0]
                s.close()
                if ip != "127.0.0.1" and not ip.startswith("169.254"):
                    candidates.append(ip)
            except:
                pass

        if not candidates:
            return "127.0.0.1"

        # Prioritize 192.168 -> 10 -> 172
        def score_ip(ip):
            if ip.startswith("192.168."): return 3
            if ip.startswith("10."): return 2
            if ip.startswith("172."): return 1
            return 0

        candidates.sort(key=score_ip, reverse=True)
        return candidates[0]
    
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
            # Cleanup previous if any
            if self.service_info:
                 self.stop_advertising()
                 
            if not self.zeroconf:
                self.zeroconf = Zeroconf()
            
            hostname = socket.gethostname().split('.')[0] # Clean hostname
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
                try:
                    self.zeroconf.unregister_service(self.service_info)
                except RuntimeError:
                    # Event loop already closed, ignore
                    pass
                self.service_info = None
                logger.info("Stopped advertising room")
        except Exception as e:
            # Only log if it's not an event loop error
            if "Event loop is closed" not in str(e):
                logger.error(f"Error stopping advertisement: {e}")
    
    def stop(self):
        """Stop discovery and cleanup resources."""
        try:
            if self.browser:
                try:
                    self.browser.cancel()
                except RuntimeError:
                    pass
                self.browser = None
            
            self.stop_advertising()
            
            if self.zeroconf:
                try:
                    self.zeroconf.close()
                except RuntimeError:
                    # Event loop already closed, ignore
                    pass
                self.zeroconf = None
            
            logger.info("Discovery stopped")
        except Exception as e:
            if "Event loop is closed" not in str(e):
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
