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
from dlm.app.commands import ShareNotify, UpdateExternalTask

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
            
    def _notify(self, msg: str, is_error: bool = False):
        """Send notification via bus."""
        try:
            self.bus.handle(ShareNotify(message=msg, is_error=is_error))
        except:
             print(f"{'[ERR] ' if is_error else ''}{msg}")

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
            "owner_device_id": self.room.owner_device_id,
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

    async def broadcast_migration(self, new_host_device_id: str, new_host_ip: str, new_host_port: int):
        """Notify all clients to reconnect to a new host."""
        msg = {
            "type": "migration",
            "new_host_id": new_host_device_id,
            "new_host_ip": new_host_ip,
            "new_host_port": new_host_port
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
            self.bus.handle(UpdateExternalTask(
                id=self.upload_task_id,
                downloaded_bytes=bytes_sent,
                speed=speed
            ))

    def _setup_routes(self):
        # 1. Auth Endpoint
        @self.app.get("/")
        async def root():
            return {"status": "ok", "app": "dlm-share", "version": "2.0"}
        
        @self.app.post("/auth")
        async def auth(request: Request):
            data = await request.json()
            token = data.get("token")
            if not self.room:
                 raise HTTPException(status_code=500, detail="Room not initialized")
            session = self.auth_manager.create_session(token, self.room)
            if session:
                return {"session_id": session.session_id}
            raise HTTPException(status_code=401, detail="Invalid token")

        # 2. Dependency for protected routes
        def verify_session(request: Request):
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                session_id = auth_header.split(" ")[1]
                if self.auth_manager.validate_session(session_id):
                    return session_id
            
            token = request.query_params.get("token")
            if token:
                if self.auth_manager.validate_session(token):
                    return token
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
                    self._notify(f"Web browser joined: {ip}")
                    asyncio.create_task(self.broadcast_state())

            return self.get_full_state()

        # 3. List Files
        @self.app.get("/list")
        async def list_files(request: Request, session_id: str = Depends(verify_session)):
            if not session_id:
                 raise HTTPException(status_code=401, detail="Unauthorized")
            
            return [{
                "file_id": fe.file_id,
                "name": fe.name,
                "size_bytes": fe.size_bytes,
                "is_dir": getattr(fe, 'is_dir', False),
                "owner_id": getattr(fe, 'owner_device_id', 'HOST')
            } for fe in self.file_entries]

        # 4. Explicit Leave
        @self.app.post("/room/leave")
        async def leave_room(request: Request, session_id: str = Depends(verify_session)):
            """Explicitly leave the room."""
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            data = await request.json()
            device_id = data.get("device_id")
            if not device_id:
                raise HTTPException(status_code=400, detail="device_id required")
            
            if self.room:
                # If host is leaving, we might want to notify or trigger handover
                # For now, just remove
                self.room.remove_device(device_id)
                self._notify(f"Device left: {device_id}")
                asyncio.create_task(self.broadcast_state())
            
            return {"status": "ok"}

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
            
            self._notify(f"Transfer started: {fe.name} -> {request.client.host}")
            
            if self.bus and self.upload_task_id:
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
                "owner_id": self.room.owner_device_id,
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
                ],
                "migration": getattr(self, '_migrating_to', None)
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
            if device_ip == self.room.host_ip and (device_id == self.room.host_device_id or device_id == "HOST" or "(you)" in device_name.lower()):
                 # Update host state instead of adding new device
                 for d in self.room.devices:
                      if "(you)" in d.name or d.device_id == self.room.host_device_id:
                           d.update_heartbeat()
                           # Ensure name still has (you)
                           if "(you)" not in d.name: d.name += " (you)"
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

        @self.app.post("/room/add-file")
        async def add_files_remote(request: Request, session_id: str = Depends(verify_session)):
            """Allows participants to announce files they want to share."""
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
                
            data = await request.json()
            files = data.get("files", [])
            device_id = data.get("device_id")
            
            count = 0
            for f in files:
                fe = FileEntry(
                    file_id=f["file_id"],
                    name=f["name"],
                    size_bytes=f["size"],
                    absolute_path="", # Path is local to the participant
                    owner_device_id=device_id,
                    is_dir=f.get("is_dir", False)
                )
                # Deduplicate
                if not any(existing.file_id == fe.file_id for existing in self.file_entries):
                    self.file_entries.append(fe)
                    count += 1
            
            if count > 0:
                self._notify(f"Node {device_id} announced {count} new files.")
                asyncio.create_task(self.broadcast_state())
                
            return {"status": "ok", "added": count}

        @self.app.post("/room/handover")
        async def handover_room(request: Request, session_id: str = Depends(verify_session)):
            """Coordinate ownership handover to a new device."""
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            data = await request.json()
            new_owner_id = data.get("new_owner_id")
            new_owner_ip = data.get("new_owner_ip")
            new_owner_port = data.get("new_owner_port")
            
            if not new_owner_id or not new_owner_ip:
                raise HTTPException(status_code=400, detail="Missing handover details")
                
            self.room.owner_device_id = new_owner_id
            self._notify(f"Room ownership migrating to {new_owner_id}...")
            
            # Phase 16: Track migration for polling clients
            self._migrating_to = {
                "id": new_owner_id,
                "ip": new_owner_ip,
                "port": new_owner_port
            }
            
            # Broadcast migration to all connected clients
            asyncio.create_task(self.broadcast_migration(new_owner_id, new_owner_ip, new_owner_port))
            
            return {"status": "migrating"}

        @self.app.post("/room/handover-ready")
        async def handover_ready(request: Request, session_id: str = Depends(verify_session)):
            """Called by successor when their server is up and ready."""
            if not session_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            data = await request.json()
            new_host_ip = data.get("ip")
            new_host_port = data.get("port")
            new_host_id = data.get("device_id")
            
            self._notify(f"Successor {new_host_id} is READY at {new_host_ip}:{new_host_port}")
            
            # Final broadcast to room
            asyncio.create_task(self.broadcast_migration(new_host_id, new_host_ip, new_host_port))
            
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
        
        # Phase 20: Silence logging to avoid TUI corruption
        import logging
        uvicorn_logger = logging.getLogger("uvicorn")
        uvicorn_logger.setLevel(logging.ERROR)
        
        # Silence root logger as well
        logging.getLogger().setLevel(logging.ERROR)
        
        # Redirect all INFO to a log file instead
        fh = logging.FileHandler("dlm_share_server.log")
        fh.setLevel(logging.INFO)
        logging.getLogger().addHandler(fh)

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
                # Phase 20: Ensure it's not a common internal bridge if possible
                for _, ip in candidates:
                    if not ip.startswith('172.17.') and not ip.startswith('172.18.'):
                        return ip
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
        
        # Method 3: Socket trick (robust version)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Use a dummy address that doesn't actually send packets
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            s.close()
            
            # Avoid docker/bridge/localhost bridge IPs if possible
            if not ip.startswith('127.') and not ip.startswith('172.17.'):
                 return ip
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

    async def _get_invite_html(self, token_hint: str = None):
        return "Dashboard Removed. Use DLM Terminal Client."

    def _format_size(self, size):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} TB"
