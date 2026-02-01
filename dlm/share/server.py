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
    def __init__(self, file_entry: FileEntry, port: int = 0):
        self.app = FastAPI(title="dlm-share")
        self.auth_manager = AuthManager()
        self.file_entry = file_entry
        self.room: Optional[Room] = None
        self.port = port
        self.host = "0.0.0.0"
        self._server_thread = None
        self._server = None
        
        self._setup_routes()

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
        async def list_files(session_id: str = Depends(verify_session)):
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

            # Support Range requests (critically important for dlm downloader)
            # Support Range requests (critically important for dlm downloader)
            print(f"[INFO] Transfer started: {self.file_entry.name} -> {request.client.host}")
            
            # Hook into response to log completion?
            # FileResponse streams. We can subclass or use background task.
            # Simple way: Background task runs after response sends.
            from starlette.background import BackgroundTask
            
            def on_complete():
                print(f"[INFO] Transfer completed: {self.file_entry.name}")

            return FileResponse(
                path, 
                filename=self.file_entry.name,
                media_type='application/octet-stream',
                background=BackgroundTask(on_complete)
            )

    def start(self):
        """Start server and block until stopped (or run in thread)."""
        # Generate Room
        token = self.auth_manager.generate_token()
        self.room = Room(
            room_id="room1", # Single room for now
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

        print("\n" + "="*40)
        print(" 🚀 SHARE STARTED")
        print("="*40)
        print(f" 📂 File:  {self.file_entry.name}")
        print(f" 📏 Size:  {self._format_size(self.file_entry.size_bytes)}")
        print("-" * 40)
        print(f" 📡 IP:    {local_ip}")
        print(f" 🔌 Port:  {self.port}")
        print(f" 🔑 Token: {token}")
        print("="*40)
        print("\nWaiting for receiver... (Ctrl+C to stop)")

        # Run Uvicorn
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="error")
        server = uvicorn.Server(config)
        server.run()

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
