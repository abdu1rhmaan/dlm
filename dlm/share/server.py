from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import socket
import threading
from typing import Optional
import os

from .models import Room, FileEntry
from .auth import AuthManager

class ShareServer:
    def __init__(self, file_entry: FileEntry, port: int = 0, bus=None, upload_task_id: str = None):
        self.app = FastAPI(title="dlm-share")
        self.auth_manager = AuthManager()
        self.file_entry = file_entry
        self.room: Optional[Room] = None
        self.port = port
        self.host = "0.0.0.0"
        self._server_thread = None
        self._server = None
        self.bus = bus
        self.upload_task_id = upload_task_id
        
        self._bytes_sent = 0
        self._lock = threading.Lock()
        self._last_update = 0
        
        # Track connected clients for UI
        self.connected_clients = set()
        
        # Add Middleware for Progress
        if self.bus and self.upload_task_id:
            self.app.middleware("http")(self.progress_middleware)
            
        self._setup_routes()

    async def progress_middleware(self, request: Request, call_next):
        response = await call_next(request)
        
        # Check if it's the download route
        if "download" in request.url.path and self.upload_task_id:
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
        
        # Calculate speed? Simple implementation: Let DLM handle speed if we send bytes?
        # UpdateExternalTask takes speed.
        # We need to track speed.
        # Simple diff:
        # self._bytes_sent / (now - start)? No, instantaneous.
        # Let's just send bytes for now. DLM TUI might calculate speed from delta if it was polling.
        # But here we are pushing updates.
        # UpdateExternalTask handler just saves.
        # So we should calculate speed here.
        # TODO: Better speed calc. For now 0.
        
        from dlm.app.commands import UpdateExternalTask
        self.bus.handle(UpdateExternalTask(
            id=self.upload_task_id,
            downloaded_bytes=bytes_sent,
            speed=0.0 # Placeholder
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

            # Explicitly set Content-Length to ensure receiver can see it
            headers = {
                "Content-Length": str(self.file_entry.size_bytes),
                "Accept-Ranges": "bytes"
            }

            return FileResponse(
                path, 
                filename=self.file_entry.name,
                media_type='application/octet-stream',
                background=BackgroundTask(on_complete),
                headers=headers
            )

    def prepare(self):
        """Prepare server (bind port, gen token) without running."""
        # Generate Room
        token = self.auth_manager.generate_token()
        self.room = Room(
            room_id="room1", 
            token=token,
            files=[self.file_entry]
        )

        # Get local IP
        local_ip = self._get_local_ip()
        
        # Determine port if 0
        if self.port == 0:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('0.0.0.0', 0))
            self.port = sock.getsockname()[1]
            sock.close()
            
        return {
            "ip": local_ip,
            "port": self.port,
            "token": token
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
        try:
            # Connect to a public DNS to get the preferred outgoing IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    def _format_size(self, size):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} TB"
