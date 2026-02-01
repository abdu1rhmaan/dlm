import requests
import sys
import time
from dlm.app.commands import AddDownload

class ShareClient:
    def __init__(self, bus):
        self.bus = bus
        self.session_id = None
        self.base_url = None

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
            
            if input("Download this file? [Y/n] ").lower() == 'n':
                print("Aborted.")
                return

            # 3. Add to DLM Engine
            download_url = f"{self.base_url}/download/{target_file['file_id']}"
            final_url = f"{download_url}?token={self.session_id}"
            
            # Determine Output Path
            output_template = self._get_output_template(target_file['name'], overrides=save_to)
            print(f"Queueing download: {final_url}")
            print(f"Destination Folder: {output_template}")
            
            # output_template in DLM/services.py (after my fix) works as the Target Folder
            from dlm.app.commands import StartDownload
            
            dl_id = self.bus.handle(AddDownload(url=final_url, output_template=output_template, title=target_file['name']))
            
            if dl_id:
                print(f"🚀 Initializing download (ID: {dl_id})...")
                self.bus.handle(StartDownload(id=dl_id))
            else:
                print("❌ Failed to queue download.")

        except requests.exceptions.ConnectionError:
            print("❌ Could not connect to sender. Check IP/Port and Firewall.")
        except Exception as e:
            print(f"❌ Error: {e}")
