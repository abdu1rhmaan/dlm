import requests
import sys
import time
from dlm.app.commands import AddDownload
from pathlib import Path
import threading
from typing import Optional, List


class ShareClient:
    """Client for connecting to share servers and joining rooms."""
    
    def __init__(self, bus):
        self.bus = bus
        self.base_url = None
        self.session_id = None
        self.device_id = None
        self.room_id = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_heartbeat = threading.Event()

    def _get_output_template(self, filename: str, overrides: str = None) -> str:
        """
        Determine the absolute output path template.
        Structure: BASE/dlm/Category/
        """
        import os
        from pathlib import Path
        
        if overrides:
            return str(Path(overrides).expanduser().resolve())

        # 1. Determine Base Path
        base_path = Path.home() / "Desktop"
        
        # Check for Termux/Android
        is_termux = "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in os.environ.get("PATH", "")
        if is_termux:
            # Termux: Use /storage/emulated/0 if accessible, else ~
            if os.access("/storage/emulated/0", os.W_OK):
                base_path = Path("/storage/emulated/0")
            else:
                base_path = Path.home()
        
        # 2. Determine Category
        ext = filename.split('.')[-1].lower() if '.' in filename else ""
        category = "Others"
        
        video_exts = {'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm', '3gp', 'mpg', 'mpeg'}
        audio_exts = {'mp3', 'wav', 'flac', 'm4a', 'aac', 'ogg', 'wma'}
        image_exts = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'heic'}
        doc_exts = {'pdf', 'doc', 'docx', 'txt', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'rtf'}
        compressed_exts = {'zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'iso'}
        
        if ext in video_exts: category = "Video"
        elif ext in audio_exts: category = "Audio"
        elif ext in image_exts: category = "Images"
        elif ext in doc_exts: category = "Documents"
        elif ext in compressed_exts: category = "Compressed"
        
        # 3. Construct Path
        final_path = base_path / "dlm" / category
        return str(final_path)

    def connect(self, ip: str, port: int, token: str, save_to: str = None):
        self.base_url = f"http://{ip}:{port}"
        print(f"Connecting to {self.base_url}...")

        try:
            # 1. Auth via POST /auth
            resp = requests.post(f"{self.base_url}/auth", json={"token": token}, timeout=5)
            if resp.status_code != 200:
                print(f"❌ Connection failed: {resp.text}")
                return
            
            data = resp.json()
            self.session_id = data.get("session_id")
            print("✅ Connected and authenticated.")
            
            # 2. Get File List
            headers = {"Authorization": f"Bearer {self.session_id}"}
            resp = requests.get(f"{self.base_url}/list", headers=headers, timeout=5)
            if resp.status_code != 200:
                print(f"❌ Failed to get file list: {resp.text}")
                return
            
            files = resp.json()
            if not files:
                print("⚠️ No files shared.")
                return

            # Phase 1: Single file support
            target_file = files[0]
            print(f"\nFound file: {target_file['name']}")
            print(f"Size: {target_file['size_bytes']} bytes")
            
            # Auto-accept for dlm share unified flow
            # if input("Download this file? [Y/n] ").lower() == 'n':
            #     print("Aborted.")
            #     return

            # 3. Add to DLM Engine
            download_url = f"{self.base_url}/download/{target_file['file_id']}"
            final_url = f"{download_url}?token={self.session_id}"
            
            # Determine Output Path
            output_template = self._get_output_template(target_file['name'], overrides=save_to)
            print(f"Destination Folder: {output_template}")
            
            # output_template in DLM/services.py (after my fix) works as the Target Folder
            from dlm.app.commands import StartDownload
            
            dl_id = self.bus.handle(AddDownload(
                url=final_url, 
                output_template=output_template, 
                title=target_file['name'], 
                source='share',
                total_size=target_file['size_bytes'],
                ephemeral=True
            ))
            
            if dl_id:
                # print(f"🚀 Initializing download (ID: {dl_id})...") # Reduced noise
                from dlm.app.commands import StartDownload
                self.bus.handle(StartDownload(id=dl_id))
                
                # [SHARE-FIX] Force queue processing to start immediately
                try:
                    from dlm.app.commands import ProcessQueue
                    self.bus.handle(ProcessQueue())
                except:
                    pass  # ProcessQueue might not exist in older versions
                
                print(f"✅ Download started! Check progress with 'ls' command.")
            else:
                print("❌ Failed to add download to queue.")
        
        except requests.exceptions.RequestException as e:
            print(f"❌ Connection error: {e}")
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
    
    # Phase 2: Room methods
    def join_room(self, ip: str, port: int, token: str, device_name: str) -> bool:
        """
        Join a room and register as a device.
        
        Args:
            ip: Room host IP
            port: Room port
            token: Room token
            device_name: This device's name
        
        Returns:
            True if joined successfully, False otherwise
        """
        self.base_url = f"http://{ip}:{port}"
        
        try:
            # 1. Authenticate
            response = requests.post(
                f"{self.base_url}/auth",
                json={"token": token},
                timeout=5
            )
            
            if response.status_code != 200:
                return False
            
            self.session_id = response.json()["session_id"]
            
            # 2. Join room
            response = requests.post(
                f"{self.base_url}/room/join",
                json={
                    "device_name": device_name,
                    "device_ip": self._get_local_ip()
                },
                headers={"Authorization": f"Bearer {self.session_id}"},
                timeout=5
            )
            
            if response.status_code == 200:
                data = response.json()
                self.room_id = data["room_id"]
                self.device_id = data["device_id"]
                
                # Start heartbeat
                self._start_heartbeat()
                
                return True
            
            return False
        
        except Exception as e:
            print(f"Failed to join room: {e}")
            return False
    
    def get_room_info(self) -> dict:
        """Get current room information."""
        if not self.session_id:
            return None
        
        try:
            response = requests.get(
                f"{self.base_url}/room/info",
                headers={"Authorization": f"Bearer {self.session_id}"},
                timeout=5
            )
            
            if response.status_code == 200:
                return response.json()
            
            return None
        except Exception:
            return None
    
    def update_device_state(self, state: str):
        """Update this device's state (idle/sending/receiving)."""
        if not self.device_id:
            return
        
        try:
            requests.post(
                f"{self.base_url}/room/state",
                json={
                    "device_id": self.device_id,
                    "state": state
                },
                headers={"Authorization": f"Bearer {self.session_id}"},
                timeout=5
            )
        except Exception:
            pass
    
    def queue_transfer(self, targets: List[str], files: List[dict]) -> bool:
        """Tell room host to queue transfers for targets."""
        if not self.session_id:
            return False
            
        try:
            response = requests.post(
                f"{self.base_url}/transfer/queue",
                json={
                    "target_devices": targets,
                    "files": files
                },
                headers={"Authorization": f"Bearer {self.session_id}"},
                timeout=5
            )
            return response.status_code == 200
        except Exception:
            return False

    def control_transfer(self, action: str, device_id: str) -> bool:
        """Send transfer control command (cancel/skip)."""
        if not self.session_id:
            return False
            
        try:
            response = requests.post(
                f"{self.base_url}/transfer/control",
                json={
                    "action": action,
                    "device_id": device_id
                },
                headers={"Authorization": f"Bearer {self.session_id}"},
                timeout=5
            )
            return response.status_code == 200
        except Exception:
            return False
    
    def _start_heartbeat(self):
        """Start heartbeat thread."""
        self._stop_heartbeat.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
    
    def _heartbeat_loop(self):
        """Heartbeat loop to maintain presence and process pending transfers."""
        active_share_dls = set()
        
        while not self._stop_heartbeat.is_set():
            try:
                # 1. Check current downloads status
                if active_share_dls:
                    from dlm.app.commands import ListDownloads
                    all_dls = self.bus.handle(ListDownloads())
                    still_active = set()
                    for dl_id in active_share_dls:
                        # Find the download in the list
                        match = next((d for d in all_dls if d.id == dl_id), None)
                        if match and match.status in ('downloading', 'pending', 'discovery'):
                            still_active.add(dl_id)
                    
                    if not still_active and active_share_dls:
                        # All shared downloads completed
                        self.update_device_state("idle")
                    
                    active_share_dls = still_active

                # 2. Send Heartbeat and get pending transfers
                response = requests.post(
                    f"{self.base_url}/room/heartbeat",
                    json={"device_id": self.device_id},
                    headers={"Authorization": f"Bearer {self.session_id}"},
                    timeout=5
                )
                
                if response.status_code == 200:
                    data = response.json()
                    pending = data.get("pending_transfers", [])
                    for transfer in pending:
                        if transfer.get("action") == "download":
                            dl_id = self._handle_incoming_transfer(transfer)
                            if dl_id:
                                active_share_dls.add(dl_id)
            except Exception:
                pass
            
            # Wait 10 seconds between heartbeats (slightly faster for responsiveness)
            self._stop_heartbeat.wait(10)

    def _handle_incoming_transfer(self, transfer: dict) -> Optional[str]:
        """Process an incoming transfer request. Returns the DL ID if started."""
        target_file = {
            "file_id": transfer["file_id"],
            "name": transfer["name"],
            "size_bytes": transfer["size"]
        }
        
        # Determine URL
        sender_url = f"http://{transfer['sender_ip']}:{transfer['sender_port']}"
        download_url = f"{sender_url}/download/{target_file['file_id']}"
        final_url = f"{download_url}?token={self.session_id}"
        
        # Determine Output Path
        output_template = self._get_output_template(target_file['name'])
        
        from dlm.app.commands import AddDownload, StartDownload
        
        dl_id = self.bus.handle(AddDownload(
            url=final_url,
            output_template=output_template,
            title=target_file['name'],
            source='share',
            total_size=target_file['size_bytes'],
            ephemeral=True
        ))
        
        if dl_id:
            self.bus.handle(StartDownload(id=dl_id))
            self.update_device_state("receiving")
            return dl_id
        return None
    
    def stop_heartbeat(self):
        """Stop heartbeat thread."""
        self._stop_heartbeat.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)
    
    def _get_local_ip(self) -> str:
        """Get local IP address."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
