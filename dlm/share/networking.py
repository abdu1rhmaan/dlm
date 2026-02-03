import asyncio
import socket
import json
import logging
import random
from typing import Callable, Dict, List, Optional

# Basic logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dlm.share.net")

class NetworkManager:
    DISCOVERY_PORT = 9999
    
    def __init__(self, username: str = "User"):
        self.username = username
        self.is_host = False
        self.host_ip = self._get_local_ip()
        self.server = None
        self.reader = None
        self.writer = None
        
        # State
        self.room_name = None
        self.tcp_port = None
        self.connected_devices: List[Dict] = []
        
        # Callbacks (UI updates)
        self.on_device_list_update: Optional[Callable[[List[Dict]], None]] = None
        self.on_room_found: Optional[Callable[[Dict], None]] = None
        
        # Tasks
        self._tasks = []

    def _get_local_ip(self):
        try:
            # Trick to get the actual local IP connected to the network
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _find_free_port(self, start=9000, end=9100):
        for port in range(start, end):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('', port))
                    return port
            except OSError:
                continue
        return 0

    async def start_host(self, room_name="default"):
        """Start hosting a room."""
        # Reset state from any previous session
        await self.shutdown()
        
        self.is_host = True
        self.room_name = room_name
        
        # 1. Start TCP Server on random port (0)
        self.server = await asyncio.start_server(
            self._handle_client, '0.0.0.0', 0
        )
        # Retrieve the actual assigned port
        self.tcp_port = self.server.sockets[0].getsockname()[1]
        
        # Add self to device list
        self.connected_devices = [{"name": f"{self.username} (Host)", "ip": self.host_ip, "status": "idle"}]
        if self.on_device_list_update:
            self.on_device_list_update(self.connected_devices)

        # 2. Start UDP Beacon
        self._tasks.append(asyncio.create_task(self._broadcast_beacon()))
        
        logger.info(f"Hosting room '{room_name}' on {self.host_ip}:{self.tcp_port}")

    async def _broadcast_beacon(self):
        """UDP Broadcast to announce room."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        message = json.dumps({
            "op": "beacon",
            "room": self.room_name,
            "port": self.tcp_port,
            "host": self.username
        }).encode()
        
        while self.is_host:
            try:
                sock.sendto(message, ('<broadcast>', self.DISCOVERY_PORT))
            except Exception as e:
                logger.error(f"Beacon error: {e}")
            await asyncio.sleep(2)

    async def start_client_scan(self):
        """Listen for UDP beacons."""
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: DiscoveryProtocol(self.on_room_found),
            local_addr=('0.0.0.0', self.DISCOVERY_PORT)
        )
        # Store transport to close later if needed
        return transport

    async def connect_to_room(self, ip: str, port: int):
        """Connect to a host."""
        try:
            self.reader, self.writer = await asyncio.open_connection(ip, port)
            
            # Send HELLO
            hello = {"op": "hello", "name": self.username}
            self.writer.write(json.dumps(hello).encode() + b'\n')
            await self.writer.drain()
            
            # Start listener loop
            self._tasks.append(asyncio.create_task(self._client_listener()))
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False

    async def _handle_client(self, reader, writer):
        """Host: Handle new client connection."""
        addr = writer.get_extra_info('peername')
        client_name = "Unknown"
        
        try:
            while True:
                data = await reader.readline()
                if not data: break
                
                msg = json.loads(data.decode())
                
                if msg['op'] == 'hello':
                    client_name = msg['name']
                    # Add to list
                    self.connected_devices.append({"name": client_name, "ip": addr[0], "status": "idle", "_w": writer})
                    self._broadcast_peers() 
                
        except Exception:
            pass
        finally:
            # Remove client
            self.connected_devices = [d for d in self.connected_devices if d.get("_w") != writer]
            self._broadcast_peers()
            writer.close()

    def _broadcast_peers(self):
        """Host: Send updated list to all clients."""
        # Clean list for sending (remove writer objects)
        clean_list = [{k:v for k,v in d.items() if k != '_w'} for d in self.connected_devices]
        
        msg = json.dumps({"op": "peers", "data": clean_list}).encode() + b'\n'
        
        # Update Host UI
        if self.on_device_list_update:
            try:
                self.on_device_list_update(clean_list)
            except Exception:
                # UI might be dead
                pass
            
        # Send to clients
        for d in self.connected_devices:
            w = d.get("_w")
            if w:
                try:
                    w.write(msg)
                    # Don't await drain here to prevent blocking if one client is slow
                except:
                    pass

    async def _client_listener(self):
        """Client: Listen for updates from Server."""
        try:
            while True:
                data = await self.reader.readline()
                if not data: break
                
                msg = json.loads(data.decode())
                if msg['op'] == 'peers':
                    self.connected_devices = msg['data']
                    if self.on_device_list_update:
                        try:
                            self.on_device_list_update(self.connected_devices)
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            logger.info("Disconnected from server")

    async def shutdown(self):
        self.is_host = False
        self.on_device_list_update = None # Stop UI updates immediately
        self.on_room_found = None
        
        self.room_name = None
        self.tcp_port = None
        self.connected_devices = []
        
        for t in self._tasks: t.cancel()
        self._tasks = []
        
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None
            
        if self.reader:
            self.reader = None

class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, callback):
        self.callback = callback

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode())
            if msg.get('op') == 'beacon' and self.callback:
                # Add IP from addr
                msg['ip'] = addr[0]
                self.callback(msg)
        except:
            pass
