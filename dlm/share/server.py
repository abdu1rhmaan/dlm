from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import socket
import threading
import asyncio
from typing import Optional, List, Dict
import os
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import json
import secrets

from .models import FileEntry
from .auth import AuthManager
from .room import Room, Device

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.loop = None

    async def connect(self, websocket: WebSocket):
        if not self.loop:
            self.loop = asyncio.get_running_loop()
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

class ShareServer:
    def __init__(self, file_entries: Optional[List[FileEntry]] = None, port: int = 0, bus=None, upload_task_id: str = None, room=None):
        self.app = FastAPI(title="dlm-share")
        self.auth_manager = AuthManager()
        
        # Phase 2: Handle multiple file entries
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
        self._bytes_sent = {} # Track bytes sent per file/device
        self.total_bytes_sent = 0 
        self._lock = threading.Lock()
        self._last_update = 0
        
        # WebSockets
        self.ws_manager = ConnectionManager()
        
        # Add Middleware for Progress
        self.app.middleware("http")(self.progress_middleware)
            
        self._setup_routes()

        @self.app.get("/ping")
        async def ping():
            return {"status": "ok", "host": socket.gethostname()}
            
    async def broadcast_state(self):
        """Broadcast full room state to all WS clients."""
        if not self.room: return
        
        state = self.get_full_state()
        state["type"] = "state"
        await self.ws_manager.broadcast(state)

    def get_full_state(self) -> dict:
        """Helper to get full serialized room state."""
        if not self.room: return {}
        
        return {
            "room_id": self.room.room_id,
            "host": f"{self.room.host_ip}:{self.port}",
            "token": self.room.token,
            "files": [
                {
                    "id": f.file_id, 
                    "name": f.name, 
                    "size": f.size_bytes, 
                    "is_dir": f.is_dir, 
                    "owner_id": f.owner_device_id
                }
                for f in self.file_entries
            ],
            "devices": [
                {
                    "device_id": d.device_id,
                    "name": d.name, 
                    "state": d.state, 
                    "ip": d.ip, 
                    "is_active": d.is_active(),
                    "current_transfer": d.current_transfer
                }
                for d in self.room.devices
            ],
            "transfer": {
                "active": self.total_bytes_sent > 0,
                "progress": (self.total_bytes_sent / sum(f.size_bytes for f in self.file_entries) * 100) if self.file_entries and sum(f.size_bytes for f in self.file_entries) > 0 else 0,
                "speed": getattr(self, 'current_speed', 0)
            }
        }
        
    async def broadcast_progress(self, file_name, percent, speed_mbps, current_bytes, total_bytes):
        msg = {
            "type": "progress",
            "file": file_name,
            "percent": percent,
            "speed": f"{speed_mbps:.1f} MB/s",
            "progress_text": f"{self._format_size(current_bytes)} / {self._format_size(total_bytes)}"
        }
        await self.ws_manager.broadcast(msg)

    def _get_file_by_id(self, file_id: str) -> Optional[FileEntry]:
        for fe in self.file_entries:
            if fe.file_id == file_id:
                return fe
        return None

    async def progress_middleware(self, request: Request, call_next):
        # Extract file_id from path if download
        path_parts = request.url.path.strip("/").split("/")
        is_download = len(path_parts) >= 2 and path_parts[0] == "download"
        file_id = path_parts[1] if is_download else None
        
        response = await call_next(request)
        
        if is_download and response.status_code < 400:
             client_ip = request.client.host
             fe = self._get_file_by_id(file_id)
             
             if fe:
                 # Update device state in room if we are host
                 if self.room:
                     # Find device by IP (rough matching for state tracking)
                     for d in self.room.devices:
                         if d.ip == client_ip:
                             d.state = "receiving"
                             # Initialize transfer info if not there
                             if not d.current_transfer:
                                 d.current_transfer = {
                                     "file_id": file_id,
                                     "name": fe.name,
                                     "progress": 0.0,
                                     "speed": 0.0,
                                     "size": fe.size_bytes
                                 }
                             break

                 async def wrapped_iterator(original_iterator):
                     import time
                     bytes_sent_for_this = 0
                     last_measure_time = time.time()
                     last_measure_bytes = 0
                     
                     try:
                         async for chunk in original_iterator:
                             yield chunk
                             chunk_len = len(chunk)
                             bytes_sent_for_this += chunk_len
                             
                             now = time.time()
                             if now - last_measure_time > 0.5: # Update room state every 0.5s
                                 diff_time = now - last_measure_time
                                 diff_bytes = bytes_sent_for_this - last_measure_bytes
                                 speed = diff_bytes / diff_time if diff_time > 0 else 0
                                 
                                 progress = (bytes_sent_for_this / fe.size_bytes * 100) if fe.size_bytes > 0 else 0
                                 
                                 if self.room:
                                     for d in self.room.devices:
                                         if d.ip == client_ip and d.current_transfer and d.current_transfer['file_id'] == file_id:
                                             d.current_transfer['progress'] = progress
                                             d.current_transfer['speed'] = speed
                                             d.update_heartbeat()
                                 
                                 last_measure_time = now
                                 last_measure_bytes = bytes_sent_for_this
                                 
                                 self.total_bytes_sent = bytes_sent_for_this
                                 # Switch to general throttled update for DLM Bus
                                 self._update_dlm_throttled(bytes_sent_for_this, speed)
                                 
                                 # Broadcast WS Event (Fire and Forget)
                                 try:
                                     # Convert to MB/s
                                     mbps = speed / 1024 / 1024
                                     asyncio.create_task(self.broadcast_progress(
                                         fe.name, progress, mbps, bytes_sent_for_this, fe.size_bytes
                                     ))
                                 except:
                                     pass
                                 
                     except Exception:
                         pass
                     finally:
                         # Cleanup state
                         if self.room:
                             for d in self.room.devices:
                                 if d.ip == client_ip:
                                     d.state = "idle"
                                     d.current_transfer = None
                                     d.update_heartbeat()
                 
                 if hasattr(response, 'body_iterator'):
                     response.body_iterator = wrapped_iterator(response.body_iterator)
        
        return response

    def _update_dlm_throttled(self, bytes_sent, speed):
        import time
        now = time.time()
        if now - self._last_update < 0.2:
            return
        self._last_update = now
        
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
                
            return {"session_id": session.session_id}

        # --- SETUP & JOIN FLOW ---
        
        @self.app.get("/invite")
        async def invite_page(request: Request):
            """Serve a modern invitation page with auto-auth."""
            t = request.query_params.get("t")
            from fastapi.responses import HTMLResponse
            html = self._get_invite_html(token_hint=t)
            return HTMLResponse(content=html)

        # 2. Dependency for protected routes
        async def verify_session(request: Request):
            ip = request.client.host
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                 session_id = auth_header.split(" ")[1]
                 if self.auth_manager.validate_session(session_id):
                     return session_id
            
            token = request.query_params.get("token")
            if token:
                # 1. Check if it's a valid session ID
                if self.auth_manager.validate_session(token):
                    return token
                # 2. Check if it's the raw room token (XX-XXX format)
                if self.room and secrets.compare_digest(token, self.room.token):
                    return token
                
            return None

        # --- WEB SOCKETS & WEB CLIENT ---
        
        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await self.ws_manager.connect(websocket)
            # Send initial state
            await self.broadcast_state()
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                self.ws_manager.disconnect(websocket)

        @self.app.get("/api/room/state")
        async def get_room_state(request: Request, session_id: str = Depends(verify_session), register: bool = False):
            """Get full real-time room state, optionally registering a device."""
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            if not self.room:
                raise HTTPException(status_code=404, detail="No room available")

            # Auto-register web client if requested
            if register:
                device_name = request.query_params.get("device_name", "Web Client")
                device_id = request.query_params.get("device_id")
                ip = request.client.host
                
                # Check if already exists
                existing = self.room.get_device(device_id) if device_id else None
                if not existing:
                    if not device_id:
                        import uuid
                        device_id = "WEB-" + str(uuid.uuid4())[:6]
                    
                    new_dev = Device(
                        device_id=device_id,
                        name=device_name,
                        ip=ip,
                        state="idle"
                    )
                    self.room.add_device(new_dev)
                    print(f"[WEB] New client joined: {device_name} ({ip})")
                    asyncio.create_task(self.broadcast_state())

            return self.get_full_state()

        @self.app.post("/room/request-download")
        async def request_download(request: Request):
            """Web Client registering intent to download."""
            data = await request.json()
            item_id = data.get("item_id")
            # We can log this or notify TUI
            print(f"[WEB] Client requested download: {item_id}")
            return {"status": "ok"}
            
        # 3. List Files

        # 3. List Files
        @self.app.get("/list")
        async def list_files(request: Request, session_id: str = Depends(verify_session)):
            if not session_id:
                 raise HTTPException(status_code=401, detail="Unauthorized")
            
            return [{
                "file_id": fe.file_id,
                "name": fe.name,
                "size_bytes": fe.size_bytes,
                "is_dir": getattr(fe, 'is_dir', False)
            } for fe in self.file_entries]

        # 4. Download File
        @self.app.get("/download/{file_id}")
        async def download_file(file_id: str, request: Request, session_id: Optional[str] = Depends(verify_session)):
            if not session_id:
                 raise HTTPException(status_code=401, detail="Unauthorized")

            fe = self._get_file_by_id(file_id)
            if not fe:
                raise HTTPException(status_code=404, detail="File not found")
                
            path = fe.absolute_path
            if not os.path.exists(path):
                raise HTTPException(status_code=404, detail="File content missing")

            headers = {
                "Content-Length": str(fe.size_bytes),
                "Accept-Ranges": "bytes"
            }
            
            print(f"[INFO] Transfer started: {fe.name} -> {request.client.host}")
            
            if self.bus and self.upload_task_id:
                 from dlm.app.commands import UpdateExternalTask
                 self.bus.handle(UpdateExternalTask(
                     id=self.upload_task_id,
                     state="DOWNLOADING"
                 ))
            
            return FileResponse(
                path, 
                filename=fe.name,
                headers=headers
            )

        # 5. List Folder Contents (Recursive)
        @self.app.get("/folder/{folder_id}")
        async def list_folder(folder_id: str, session_id: str = Depends(verify_session)):
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            fe = self._get_file_by_id(folder_id)
            if not fe or not getattr(fe, 'is_dir', False):
                raise HTTPException(status_code=404, detail="Folder unit not found")
            
            base_path = Path(fe.absolute_path)
            items = []
            for p in base_path.rglob("*"):
                if p.is_file():
                    items.append({
                        "rel_path": str(p.relative_to(base_path)),
                        "size": p.stat().st_size
                    })
            return items

        # 6. Download Sub-file from Folder Unit
        @self.app.get("/download/{folder_id}/sub")
        async def download_sub_file(folder_id: str, rel_path: str, session_id: str = Depends(verify_session)):
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            fe = self._get_file_by_id(folder_id)
            if not fe or not getattr(fe, 'is_dir', False):
                raise HTTPException(status_code=404, detail="Folder unit not found")
            
            # Prevent path traversal
            safe_rel_path = rel_path.replace("..", "").replace("//", "/")
            full_path = Path(fe.absolute_path) / safe_rel_path
            
            if not full_path.exists() or not full_path.is_file():
                raise HTTPException(status_code=404, detail="Sub-file not found")
            
            return FileResponse(str(full_path), filename=full_path.name, media_type='application/octet-stream')
        
        # Phase 2: Room Endpoints
        @self.app.get("/room/info")
        async def get_room_info(session_id: str = Depends(verify_session)):
            """Get room information and device list."""
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            if not self.room:
                raise HTTPException(status_code=404, detail="No room available")
            
            # Prune stale devices to keep display clean
            self.room.prune_stale_devices()
            
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
                ],
                "files": [
                    {
                        "file_id": fe.file_id, 
                        "name": fe.name, 
                        "size_bytes": fe.size_bytes, 
                        "owner_id": fe.owner_device_id
                    }
                    for fe in self.file_entries
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
            
            # 1. Check if this is the host itself re-joining (e.g. from TUI)
            if device_ip == self.room.host_ip and (device_id == "HOST" or device_name == self.room.host_device_name):
                 # Update host state instead of adding new device
                 for d in self.room.devices:
                      if "(you)" in d.name or d.device_id == self.room.host_device_id:
                           d.update_heartbeat()
                           return {
                               "room_id": self.room.room_id,
                               "device_id": d.device_id,
                               "status": "active"
                           }

            # 2. Use provided ID or generate new one
            if not device_id:
                import uuid
                device_id = str(uuid.uuid4())[:8]
            
            device = Device(
                device_id=device_id,
                name=device_name,
                ip=device_ip,
                state="idle"
            )
            
            self.room.add_device(device)
            # Broadcast join event
            asyncio.create_task(self.broadcast_state())
            
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
            asyncio.create_task(self.broadcast_state())
            return {"status": "ok"}

        @self.app.post("/transfer/queue")
        async def queue_transfer(request: Request, session_id: str = Depends(verify_session)):
            """Coordinate multi-file transfer queue."""
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            data = await request.json()
            target_device_ids = data.get("target_devices", [])
            files = data.get("files", [])
            
            # Phase 2: Per-file targeting
            multi_target = target_device_ids == ["SPECIAL_MULTIPER"]

            for f in files:
                file_info = {
                    "action": "download",
                    "file_id": f["file_id"],
                    "name": f["name"],
                    "size": f["size"],
                    "sender_ip": self.room.host_ip,
                    "sender_port": self.port,
                    "is_dir": f.get("is_dir", False)
                }
                
                targets = f.get("targets", target_device_ids) if multi_target else target_device_ids
                
                for device_id in targets:
                    device = self.room.get_device(device_id)
                    if device:
                        device.pending_transfers.append(file_info)
            
            return {"status": "queued"}

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
            # Sync back to room if it exists
            if self.room:
                self.room.port = self.port
            
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
        if not self.room or not self.port or self.port == 0:
             self.prepare()
        
        # Run Uvicorn
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="error", ws_ping_interval=None)
        self._server = uvicorn.Server(config)
        self._server.run()

    def stop(self):
        """Stop the server."""
        if self._server:
            self._server.should_exit = True

    def start(self):
        """Legacy start (auto prepare and run)."""
        info = self.prepare()
        # Suppress prints to avoid breaking TUI
        # print("\n" + "="*40)
        # print(" 🚀 SHARE STARTED")
        # print("="*40)
        # ...
        self.run_server()

    def _get_local_ip(self):
        """Get the actual LAN IP address, with Termux compatibility."""
        
        # Method 1: Try psutil (most reliable for Termux and cross-platform)
        try:
            import psutil
            # Common virtual/loopback/VPN interface prefixes to ignore
            BLACKLIST = ['vbox', 'docker', 'virtual', 'wsl', 'tailscale', 'zerotier', 'vpn', 'vmnet']
            
            candidates = []
            for interface, addrs in psutil.net_if_addrs().items():
                if any(b in interface.lower() for b in BLACKLIST):
                    continue
                    
                for addr in addrs:
                    if addr.family == 2:  # AF_INET (IPv4)
                        ip = addr.address
                        if ip.startswith('127.'):
                            continue
                            
                        # Assign scores: 192.168 (100), 10. (90), 172. (80), Other (70)
                        score = 70
                        # Assign scores: 
                        # 192.168.1.x or 192.168.0.x (100) - Most common home routers
                        # 192.168.x.x (95) - Other 192.168
                        # 10.x.x.x (90) - Common corporate LAN
                        # 172.16-31.x.x (80)
                        # 192.168.56.x (10) - VirtualBox (Heuristic penalty)
                        # Other (70)
                        
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
                # Return the one with highest score
                candidates.sort(reverse=True)
                return candidates[0][1]
            
            # Absolute fallback: Find ANY IPv4
            for interface, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family == 2 and not addr.address.startswith('127.'):
                        return addr.address
                        
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

    def _get_join_bash(self, base_url: str) -> str:
        """Dynamic Smart Join Script for Termux/Shell."""
        if not self.room: return "echo 'No room active'"
        
        room_id = self.room.room_id
        host = self.room.host_ip
        port = self.port
        token = self.room.token

        script = f"""#!/bin/bash
# DLM SMART JOIN SCRIPT
# This script ensures dlm is installed and connects to the room.

echo -e "\\033[1;32m[ DLM AUTO-JOIN ]\\033[0m"
echo "Room: {room_id} | Host: {host}:{port}"

# 1. Dependency Validation
for cmd in git python3 pip; do
    if ! command -v $cmd &> /dev/null; then
        echo -e "\\033[1;31mDependency missing: $cmd\\033[0m"
        [ -n "$TERMUX_VERSION" ] && pkg install -y python git || {{ echo "Please install $cmd"; exit 1; }}
    fi
done

# 2. DLM Setup/Update
if ! command -v dlm &> /dev/null; then
    echo "[*] Installing dlm..."
    [ ! -d "$HOME/dlm" ] && git clone https://github.com/abdu1rhmaan/dlm "$HOME/dlm"
    (cd "$HOME/dlm" && pip install -e .)
else
    echo "[*] dlm found. Checking for updates..."
    # Check common install locations
    IF_DIR="$HOME/dlm"
    if [ -d "$IF_DIR" ]; then 
        (cd "$IF_DIR" && git pull && pip install -e .)
    else
        # Try current dir
        [ -d "dlm" ] && (cd dlm && git pull && pip install -e .)
    fi
fi

# 3. Connection
echo -e "\\n\\033[1;34m[*] Connecting to room {room_id}...\\033[0m"
dlm share join --ip {host} --port {port} --token {token}
"""
        return script

    def _get_invite_html(self, token_hint: str = None) -> str:
        """Serve the Retro Terminal Web Client fully wired to the backend."""
        room_id = self.room.room_id if self.room else "N/A"
        token = self.room.token if self.room else "N/A"
        host = self.room.host_ip if self.room else "N/A"
        port = self.port
        
        # Bash script payload for JS
        bash_payload = self._get_join_bash("http://"+host+":"+str(port)).replace('`', '\\`').replace('$', '\\$').replace('\n', '\\n')
        
        # Auto-Auth logic
        auto_auth_js = ""
        if token_hint:
             auto_auth_js = f"localStorage.setItem('dlm_token', '{token_hint}');"

        return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DLM SHARE :: TERMINAL</title>
    <style>
        :root {{
            --bg: #000000;
            --fg: #00ff00;
            --dim: #005500;
            --border: #00ff00;
            --font: 'Courier New', Courier, monospace;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            background-color: var(--bg);
            color: var(--fg);
            font-family: var(--font);
            margin: 0;
            padding: 15px;
            font-size: 14px;
            overflow-x: hidden;
            border: 4px double var(--dim);
            min-height: 100vh;
        }}
        
        .header {{ 
            width: 100%;
            text-align: center; 
            border-bottom: 2px solid var(--dim); 
            padding-bottom: 10px; 
            margin-bottom: 20px;
        }}
        h1 {{ margin: 0; font-size: 22px; letter-spacing: 2px; }}

        .container {{
            display: grid;
            grid-template-columns: 350px 1fr;
            gap: 15px;
            max-width: 1300px;
            margin: 0 auto;
        }}
        
        @media (max-width: 900px) {{
            .container {{ grid-template-columns: 1fr; }}
        }}

        /* Retro Boxes */
        .box {{
            border: 1px solid var(--dim);
            padding: 12px;
            position: relative;
            background: #000;
            margin-bottom: 15px;
        }}
        .box-title {{
            position: absolute;
            top: -9px;
            left: 10px;
            background: var(--bg);
            padding: 0 5px;
            font-weight: bold;
            font-size: 12px;
            color: var(--fg);
            text-transform: uppercase;
        }}

        .stat-line {{ display: flex; justify-content: space-between; margin-bottom: 8px; }}
        .label {{ color: var(--dim); }}
        .value {{ color: var(--fg); font-weight: bold; }}

        /* Lists */
        .list-container {{
            max-height: 400px;
            overflow-y: auto;
        }}
        .list-item {{
            padding: 8px 6px;
            border-bottom: 1px dotted var(--dim);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .list-item:hover {{ background: #001100; }}
        .list-item:last-child {{ border-bottom: none; }}
        .item-info {{ flex: 1; }}
        .item-name {{ font-weight: bold; display: block; overflow: hidden; text-overflow: ellipsis; }}
        .item-sub {{ font-size: 11px; color: var(--dim); }}
        
        .status-dot {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 5px;
        }}
        .status-active {{ background: #00ff00; box-shadow: 0 0 5px #00ff00; }}
        .status-idle {{ background: var(--dim); }}

        /* Buttons */
        .btn {{
            background: transparent;
            border: 1px solid var(--fg);
            color: var(--fg);
            padding: 6px 14px;
            font-family: var(--font);
            font-size: 12px;
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
            transition: 0.2s;
        }}
        .btn:hover {{ background: var(--fg); color: #000; }}
        .btn-large {{ width: 100%; padding: 12px; margin-top: 10px; font-weight: bold; text-align: center; border-width: 2px; }}
        .btn-termux {{ border-color: #00aaff; color: #00aaff; }}
        .btn-termux:hover {{ background: #00aaff; color: #000; }}

        /* PROGRESS */
        .pipeline-container {{
            grid-column: 1 / -1;
            display: none; 
        }}
        .progress-bar-outer {{
            width: 100%;
            height: 30px;
            border: 1px solid var(--fg);
            margin-top: 10px;
            position: relative;
            background: #001100;
        }}
        .progress-bar-fill {{
            height: 100%;
            background: var(--fg);
            width: 0%;
            transition: width 0.5s ease;
        }}
        .progress-text {{
            position: absolute;
            top: 0; left: 0; width: 100%; height: 100%;
            display: flex; justify-content: center; align-items: center;
            color: #fff; text-shadow: 1px 1px 0 #000;
            font-weight: bold; font-size: 16px;
        }}

        #toast {{
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            background: #00ff00;
            color: #000;
            padding: 10px 20px;
            font-weight: bold;
            display: none;
            z-index: 1000;
        }}

        .terminal-logs {{
            height: 120px;
            background: #000500;
            border: 1px solid #002200;
            padding: 8px;
            font-size: 11px;
            overflow-y: auto;
            color: #00aa00;
            margin-top: 10px;
        }}
    </style>
</head>
<body onload="init()">

<div id="toast">COPIED TO CLIPBOARD</div>

<div class="header">
    <h1>DLM // SHARE TERMINAL</h1>
    <div id="connection-status" style="color: var(--dim); font-size: 12px; margin-top:5px;">[ STATUS: CONNECTING... ]</div>
</div>

<div class="container">
    <div>
        <div class="box">
            <span class="box-title">Lobby Properties</span>
            <div class="stat-line"><span class="label">ROOM ID:</span> <span class="value" id="room-id">{room_id}</span></div>
            <div class="stat-line"><span class="label">TOKEN:</span> <span class="value" id="room-token">{token}</span></div>
            <div class="stat-line"><span class="label">HOST:</span> <span class="value" id="room-host">{host}:{port}</span></div>
            
            <button class="btn btn-large" onclick="copyJoinScript()">COPY JOIN SCRIPT</button>
            <div id="android-actions" style="display: none;">
                <a href="intent://com.termux/#Intent;scheme=termux;end" class="btn btn-large btn-termux">OPEN TERMUX</a>
            </div>
        </div>

        <div class="box">
            <span class="box-title">Connected Nodes</span>
            <div id="device-list" class="list-container">
                <!-- Initial state hydrate -->
            </div>
        </div>
    </div>

    <div>
        <div id="pipeline" class="box pipeline-container">
            <span class="box-title">Transfer Pipeline</span>
            <div class="stat-line"><span class="label">FILE:</span> <span class="value" id="pipeline-file">--</span></div>
            <div class="stat-line"><span class="label">SPEED:</span> <span class="value" id="pipeline-speed">0.0 MB/s</span></div>
            <div class="progress-bar-outer">
                <div id="pipeline-fill" class="progress-bar-fill"></div>
                <div id="pipeline-text" class="progress-text">0%</div>
            </div>
        </div>

        <div class="box">
            <span class="box-title">Shared Directory</span>
            <div id="file-list" class="list-container">
                <!-- Initial state hydrate -->
            </div>
        </div>

        <div class="box">
            <span class="box-title">System Logs</span>
            <div id="logs" class="terminal-logs">
                [SYS] Terminal Session Initialized.<br>
            </div>
        </div>
    </div>
</div>

<script>
    let ws;
    {auto_auth_js}

    function log(msg) {{
        const logs = document.getElementById('logs');
        logs.innerHTML += `[${{new Date().toLocaleTimeString()}}] ${{msg}}<br>`;
        logs.scrollTop = logs.scrollHeight;
    }}

    function init() {{
        if (navigator.userAgent.toLowerCase().includes('android')) {{
            document.getElementById('android-actions').style.display = 'block';
        }}
        connectWS();
        hydrate();
    }}

    function connectWS() {{
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${{protocol}}//${{window.location.host}}/ws`);
        
        ws.onopen = () => {{
            document.getElementById('connection-status').style.color = '#00ff00';
            document.getElementById('connection-status').innerText = '[ STATUS: CONNECTED ]';
            log("WebSocket Connection Established.");
        }};

        ws.onmessage = (e) => {{
            const data = JSON.parse(e.data);
            if (data.type === 'state') {{
                renderState(data);
            }} else if (data.type === 'progress') {{
                updateProgress(data);
            }}
        }};

        ws.onclose = () => {{
            document.getElementById('connection-status').style.color = '#ff0000';
            document.getElementById('connection-status').innerText = '[ STATUS: DISCONNECTED ]';
            log("WebSocket Disconnected. Reconnecting...");
            setTimeout(connectWS, 2000);
        }};
    }}

    async function hydrate() {{
        try {{
            const token = localStorage.getItem('dlm_token');
            let devId = localStorage.getItem('dlm_device_id');
            if (!devId) {{
                devId = 'WEB-' + Math.random().toString(36).substr(2, 6).toUpperCase();
                localStorage.setItem('dlm_device_id', devId);
            }}
            
            const url = new URL('/api/room/state', window.location.origin);
            if (token) url.searchParams.set('token', token);
            url.searchParams.set('register', 'true');
            url.searchParams.set('device_id', devId);
            url.searchParams.set('device_name', 'Web Browser');

            const res = await fetch(url);
            if (res.ok) {{
                const data = await res.json();
                renderState(data);
                log("Hydrated & Registered as " + devId);
            }} else {{
                log("Hydration failed (Status: " + res.status + "). Check token.");
            }}
        }} catch(e) {{
            log("Hydration failed: Network Error.");
        }}
    }}

    function renderState(state) {{
        // UI Sync
        document.getElementById('room-id').innerText = state.room_id || "{room_id}";
        document.getElementById('room-token').innerText = state.token || "{token}";

        // Devices
        const devList = document.getElementById('device-list');
        devList.innerHTML = '';
        state.devices.forEach(d => {{
            const item = document.createElement('div');
            item.className = 'list-item';
            const statusClass = d.is_active ? 'status-active' : 'status-idle';
            item.innerHTML = `
                <div class="item-info">
                    <span class="item-name"><span class="status-dot ${{statusClass}}"></span>${{d.name}}</span>
                    <span class="item-sub">${{d.ip}} | ${{d.state}}</span>
                </div>
            `;
            devList.appendChild(item);
        }});

        // Files
        const fileList = document.getElementById('file-list');
        fileList.innerHTML = '';
        if (!state.files || state.files.length === 0) {{
             fileList.innerHTML = '<div style="padding:20px; color:var(--dim); text-align:center;">Empty Directory</div>';
        }} else {{
            state.files.forEach(f => {{
                const item = document.createElement('div');
                item.className = 'list-item';
                item.innerHTML = `
                    <div class="item-info">
                        <span class="item-name">${{f.name}}</span>
                        <span class="item-sub">${{formatSize(f.size)}}</span>
                    </div>
                    <button class="btn" onclick="downloadFile('${{f.id}}')">GET</button>
                `;
                fileList.appendChild(item);
            }});
        }}

        // Pipeline Logic (Aggregate)
        if (state.transfer && state.transfer.active) {{
            document.getElementById('pipeline').style.display = 'block';
            document.getElementById('pipeline-speed').innerText = state.transfer.speed.toFixed(2) + " MB/s";
            document.getElementById('pipeline-fill').style.width = state.transfer.progress.toFixed(1) + '%';
            document.getElementById('pipeline-text').innerText = state.transfer.progress.toFixed(1) + '%';
        }}
    }}

    function updateProgress(data) {{
        document.getElementById('pipeline').style.display = 'block';
        document.getElementById('pipeline-file').innerText = data.file;
        document.getElementById('pipeline-speed').innerText = data.speed;
        document.getElementById('pipeline-fill').style.width = data.percent.toFixed(1) + '%';
        document.getElementById('pipeline-text').innerText = data.percent.toFixed(1) + '%';
        
        if (data.percent >= 100) {{
             log("Downloaded: " + data.file);
        }}
    }}

    function formatSize(bytes) {{
        if (!bytes) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }}

    function downloadFile(id) {{
        fetch('/room/request-download', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ item_id: id }})
        }}).then(() => {{
            const token = localStorage.getItem('dlm_token');
            window.location.href = `/download/${{id}}${{token ? '?token=' + token : ''}}`;
            log("Download Request Sent.");
        }});
    }}

    function copyJoinScript() {{
        const raw = `{bash_payload}`;
        const script = raw.replace(/\\\\n/g, '\\n').replace(/\\\\`/g, '`').replace(/\\\\\\$/g, '$');
        navigator.clipboard.writeText(script).then(() => {{
            const toast = document.getElementById('toast');
            toast.style.display = 'block';
            setTimeout(() => toast.style.display = 'none', 2000);
        }});
    }}
</script>

</body>
</html>
"""

    def _format_size(self, size):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} TB"
