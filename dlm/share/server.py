from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import socket
import threading
from typing import Optional, List, Dict
import os
from datetime import datetime

from .models import FileEntry
from .auth import AuthManager
from .room import Room, Device

class ShareServer:
    def __init__(self, file_entries: Optional[List[FileEntry]] = None, port: int = 0, bus=None, upload_task_id: str = None, room=None):
        self.app = FastAPI(title="dlm-share")
        self.auth_manager = AuthManager()
        
        # Handle both single FileEntry (Phase 1 legacy) and List[FileEntry] (Phase 2)
        if file_entries:
            if isinstance(file_entries, list):
                self.file_entries = file_entries
            else:
                self.file_entries = [file_entries]
        else:
            self.file_entries = []
            
        self.room = room  # Phase 2: Room instance
        self.port = port
        self.host = "0.0.0.0"
        self._server_thread = None
        self._server = None
        self.bus = bus
        self.upload_task_id = upload_task_id
        
        self.transfer_queues = {} # Track active transfer queues
        self._bytes_sent = 0
        self._lock = threading.Lock()
        self._last_update = 0
        
        # Track connected clients for UI
        self.connected_clients = set()
        
        # Add Middleware for Progress
        # Add Middleware for Progress (Always on for share)
        self.app.middleware("http")(self.progress_middleware)
            
        self._setup_routes()

    async def progress_middleware(self, request: Request, call_next):
        response = await call_next(request)
        
        # Check if it's the download route
        if "download" in request.url.path:
             # Wrap the streaming response
             async def wrapped_iterator(original_iterator):
                 import time
                 try:
                     async for chunk in original_iterator:
                         yield chunk
                         with self._lock:
                             self._bytes_sent += len(chunk)
                             current_bytes = self._bytes_sent
                         
                         # Throttled update (every 0.5s or so?)
                         # Or just fire-and-forget? Bus overhead?
                         # Let's fire every 100KB or something.
                         # Better: Check time.
                        #  now = time.time()
                        #  if now - self._last_update > 0.5:
                        #      self._last_update = now
                        #      self._update_dlm(current_bytes)
                         # Actually, let's just update. The TUI polls. Repo updates are fast enough?
                         # 10MB/s = 1000 updates/s if 10KB chunks. Too fast.
                         # Update logic:
                         self._update_dlm_throttled(current_bytes)
                         
                 except Exception:
                     # Connection dropped?
                     pass
             
             # Modify response body
             # Starlette StreamingResponse / FileResponse uses .body_iterator for async
             if hasattr(response, 'body_iterator'):
                 response.body_iterator = wrapped_iterator(response.body_iterator)
        
        return response

    def _update_dlm_throttled(self, bytes_sent):
        import time
        now = time.time()
        if now - self._last_update < 0.2: # Max 5 updates/sec
            return
        self._last_update = now
        
        # Calculate speed
        if not hasattr(self, '_last_bytes_measure'):
             self._last_bytes_measure = 0
             self._last_time_measure = now
        
        speed = 0.0
        time_diff = now - self._last_time_measure
        if time_diff > 0:
            bytes_diff = bytes_sent - self._last_bytes_measure
            speed = bytes_diff / time_diff
        
        self._last_bytes_measure = bytes_sent
        self._last_time_measure = now
        
        # Expose speed for local monitoring
        self.current_speed = speed

        if self.bus and self.upload_task_id:
            from dlm.app.commands import UpdateExternalTask
            self.bus.handle(UpdateExternalTask(
                id=self.upload_task_id,
                downloaded_bytes=bytes_sent,
                speed=speed
            ))

    def _setup_routes(self):
        # 1. Auth Endpoint
        @self.app.post("/auth")
        async def auth(request: Request):
            data = await request.json()
            token = data.get("token")
            if not token:
                raise HTTPException(status_code=400, detail="Token required")
            
            session = self.auth_manager.create_session(token, self.room)
            if not session:
                raise HTTPException(status_code=401, detail="Invalid token or expired room")
                
            print(f"[INFO] Authenticated: {session.session_id} (New Session)")
            return {"session_id": session.session_id}

        # 2. Dependency for protected routes
        async def verify_session(request: Request):
            ip = request.client.host
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                 session_id = auth_header.split(" ")[1]
                 if self.auth_manager.validate_session(session_id):
                     return session_id
            
            # Log connection attempt only if failing? Or success?
            # Too noisy if every chunk logs success. 
            # We log initial Auth endpoint usage above.
            return None # Return None if header auth fails, custom logic in endpoint can check query param

        # 3. List Files
        @self.app.get("/list")
        async def list_files(request: Request, session_id: str = Depends(verify_session)):
            if not session_id:
                 print(f"[INFO] Unauthorized connection attempt from {request.client.host}")
                 raise HTTPException(status_code=401, detail="Unauthorized")
            
            # Log only once per session/list? LIST is good indicator of initial connection.
            print(f"[INFO] Receiver connected: {request.client.host} (Listing files)")
            return [{
                "file_id": self.file_entry.file_id,
                "name": self.file_entry.name,
                "size_bytes": self.file_entry.size_bytes
            }]

        # 4. Download File
        @self.app.get("/download/{file_id}")
        async def download_file(file_id: str, request: Request, session_id: Optional[str] = Depends(verify_session), token: Optional[str] = None):
            # Track Client
            client_ip = request.client.host
            with self._lock:
                self.connected_clients.add(client_ip)


            # Explicitly set Content-Length to ensure receiver can see it
            headers = {
                "Content-Length": str(self.file_entry.size_bytes),
                "Accept-Ranges": "bytes"
            }
            
            # Allow auth via Query param 'token' if header is missing (for dlm engine integration)
            if not session_id:
                if token and self.auth_manager.validate_session(token):
                     pass
                else:
                     raise HTTPException(status_code=401, detail="Unauthorized")

            if file_id != self.file_entry.file_id:
                raise HTTPException(status_code=404, detail="File not found")
                
            path = self.file_entry.absolute_path
            if not os.path.exists(path):
                raise HTTPException(status_code=404, detail="File content missing")

            # Support Range requests logic handled by FileResponse/Starlette
            print(f"[INFO] Transfer started: {self.file_entry.name} -> {request.client.host}")
            
            # Notify DLM that transfer started (Switch color to Pink/Active)
            if self.bus and self.upload_task_id:
                 from dlm.app.commands import UpdateExternalTask
                 self.bus.handle(UpdateExternalTask(
                     id=self.upload_task_id,
                     downloaded_bytes=0, # Start
                     state="DOWNLOADING"
                 ))
            
            # Hook into response to log completion?
            # FileResponse streams. We can subclass or use background task.
            # Simple way: Background task runs after response sends.
            from starlette.background import BackgroundTask
            
            def on_complete():
                print(f"[INFO] Transfer completed: {self.file_entry.name}")
                # Mark Complete in DLM Task
                if self.bus and self.upload_task_id:
                    from dlm.app.commands import UpdateExternalTask
                    self.bus.handle(UpdateExternalTask(
                        id=self.upload_task_id,
                        downloaded_bytes=self.file_entry.size_bytes,
                        state="COMPLETED"
                    ))

            # Allow explicit headers to be managed by FileResponse (except explicit ones we need)
            # FileResponse handles Content-Length and Content-Range for us.
            
            return FileResponse(
                path, 
                filename=self.file_entry.name,
                media_type='application/octet-stream',
                background=BackgroundTask(on_complete)
            )
        
        # Phase 2: Room Endpoints
        @self.app.get("/room/info")
        async def get_room_info(session_id: str = Depends(verify_session)):
            """Get room information and device list."""
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            if not self.room:
                raise HTTPException(status_code=404, detail="No room available")
            
            return {
                "room_id": self.room.room_id,
                "host_ip": self.room.host_ip,
                "port": self.room.port,
                "devices": [
                    {
                        "device_id": d.device_id,
                        "name": d.name,
                        "ip": d.ip,
                        "state": d.state,
                        "active": d.is_active()
                    }
                    for d in self.room.devices
                ]
            }
        
        @self.app.post("/room/join")
        async def join_room(request: Request):
            """Join the room as a new device."""
            if not self.room:
                raise HTTPException(status_code=404, detail="No room available")
            
            data = await request.json()
            device_name = data.get("device_name")
            device_ip = data.get("device_ip")
            device_id = data.get("device_id")
            
            if not device_name or not device_ip:
                raise HTTPException(status_code=400, detail="device_name and device_ip required")
            
            # Import Device model
            from .room import Device
            import uuid
            
            # Generate device ID if not provided
            if not device_id:
                device_id = str(uuid.uuid4())[:8]
            
            device = Device(
                device_id=device_id,
                name=device_name,
                ip=device_ip,
                state="idle"
            )
            
            self.room.add_device(device)
            print(f"[INFO] Device joined room: {device_name} ({device_ip})")
            
            return {
                "room_id": self.room.room_id,
                "device_id": device.device_id,
                "status": "joined"
            }
        
        @self.app.post("/room/heartbeat")
        async def heartbeat(request: Request):
            """Update device heartbeat timestamp and return pending transfers."""
            if not self.room:
                raise HTTPException(status_code=404, detail="No room available")
            
            data = await request.json()
            device_id = data.get("device_id")
            
            if not device_id:
                raise HTTPException(status_code=400, detail="device_id required")
            
            device = self.room.get_device(device_id)
            if not device:
                raise HTTPException(status_code=404, detail="Device not found")
            
            device.update_heartbeat()
            
            # Return and clear pending transfers
            pending = list(device.pending_transfers)
            device.pending_transfers.clear()
            
            return {
                "status": "ok",
                "pending_transfers": pending
            }
        
        @self.app.post("/room/state")
        async def update_device_state(request: Request):
            """Update device state (idle/sending/receiving)."""
            if not self.room:
                raise HTTPException(status_code=404, detail="No room available")
            
            data = await request.json()
            device_id = data.get("device_id")
            state = data.get("state")
            
            if not device_id or not state:
                raise HTTPException(status_code=400, detail="device_id and state required")
            
            self.room.update_device_state(device_id, state)
            return {"status": "ok"}

        @self.app.post("/transfer/queue")
        async def queue_transfer(request: Request, session_id: str = Depends(verify_session)):
            """Coordinate multi-file transfer queue."""
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            data = await request.json()
            # data: { "target_devices": [...], "files": [{"file_id": "...", "name": "...", "...", "size": 123}, ...] }
            
            target_device_ids = data.get("target_devices", [])
            files = data.get("files", [])
            sender_info = data.get("sender", {
                "ip": self.room.host_ip,
                "port": self.room.port
            })
            
            if not target_device_ids or not files:
                raise HTTPException(status_code=400, detail="target_devices and files required")
            
            for device_id in target_device_ids:
                device = self.room.get_device(device_id)
                if device:
                    # Add all files to this device's pending list
                    for f in files:
                        device.pending_transfers.append({
                            "action": "download",
                            "file_id": f["file_id"],
                            "name": f["name"],
                            "size": f["size"],
                            "sender_ip": sender_info.get("ip", self.room.host_ip),
                            "sender_port": sender_info.get("port", self.room.port)
                        })
            
            import uuid
            queue_id = str(uuid.uuid4())[:8]
            return {"queue_id": queue_id, "status": "queued"}

    def prepare(self):
        """Prepare server (bind port, gen token) without running."""
        # Get local IP
        local_ip = self._get_local_ip()
        
        # Determine port if 0
        if self.port == 0:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('0.0.0.0', 0))
            self.port = sock.getsockname()[1]
            sock.close()
            
        # If no room provided, create a default one
        if not self.room:
            token = self.auth_manager.generate_token()
            self.room = Room(
                room_id=self.auth_manager._generate_room_id() if hasattr(self.auth_manager, '_generate_room_id') else "ROOM", 
                token=token,
                host_ip=local_ip,
                port=self.port,
                devices=[],
                created_at=datetime.now()
            )
        else:
            # Sync room host/port if needed
            self.room.host_ip = local_ip
            self.room.port = self.port
            
        return {
            "ip": local_ip,
            "port": self.port,
            "token": self.room.token
        }

    def run_server(self):
        """Run the server (blocking). Must call prepare() first."""
        if not self.room:
             self.prepare()
        
        # Run Uvicorn
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="error")
        server = uvicorn.Server(config)
        server.run()

    def start(self):
        """Legacy start (auto prepare and run)."""
        info = self.prepare()
        print("\n" + "="*40)
        print(" 🚀 SHARE STARTED")
        print("="*40)
        print(f" 📂 File:  {self.file_entry.name}")
        print(f" 📏 Size:  {self._format_size(self.file_entry.size_bytes)}")
        print("-" * 40)
        print(f" 📡 IP:    {info['ip']}")
        print(f" 🔌 Port:  {info['port']}")
        print(f" 🔑 Token: {info['token']}")
        print("="*40)
        print("\nWaiting for receiver... (Ctrl+C to stop)")
        self.run_server()

    def _get_local_ip(self):
        """Get the actual LAN IP address, with Termux compatibility."""
        
        # Method 1: Try psutil (most reliable for Termux and cross-platform)
        try:
            import psutil
            for interface, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family == 2:  # AF_INET (IPv4)
                        ip = addr.address
                        # Skip loopback, prioritize 192.168.x.x, then 10.x.x.x, then 172.16-31.x.x
                        if ip.startswith('192.168.'):
                            return ip
                        elif ip.startswith('10.'):
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
        
        # Method 2: Try netifaces (additional fallback)
        try:
            import netifaces
            for interface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(interface)
                if netifaces.AF_INET in addrs:
                    for addr_info in addrs[netifaces.AF_INET]:
                        ip = addr_info.get('addr')
                        if ip and not ip.startswith('127.'):
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
        
        # Method 3: Socket trick (last resort before hostname)
        # This can return incorrect IPs on some configurations
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            
            # STRICT validation: Only accept if it's a valid LAN IP
            if ip.startswith('192.168.') or ip.startswith('10.'):
                return ip
            elif ip.startswith('172.'):
                try:
                    second_octet = int(ip.split('.')[1])
                    if 16 <= second_octet <= 31:
                        return ip
                except (ValueError, IndexError):
                    pass
        except Exception:
            pass
        
        # Method 4: Hostname resolution (last resort)
        try:
            hostname = socket.gethostname()
            ip = socket.gethostbyname(hostname)
            if not ip.startswith('127.'):
                return ip
        except Exception:
            pass
        
        # Ultimate fallback
        return "127.0.0.1"

    def _format_size(self, size):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} TB"
