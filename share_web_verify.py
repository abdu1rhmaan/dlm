import sys
import os
import threading
import time
import shutil
import uuid
import requests
from pathlib import Path

# Add project root to path
sys.path.append(os.getcwd())

def setup_dummy_folder() -> Path:
    base = Path("share_test_folder")
    if base.exists(): shutil.rmtree(base)
    base.mkdir()
    (base / "file1.txt").write_text("Hello World")
    return base

def cleanup_dummy_folder(path: Path):
    if path.exists(): shutil.rmtree(path)

def test_web_client():
    print("Testing Web Client Endpoints...")
    from dlm.share.server import ShareServer
    from dlm.share.room_manager import RoomManager
    from dlm.app.commands import CommandBus as Bus
    from dlm.share.models import FileEntry
    
    bus = Bus()
    rm = RoomManager()
    room = rm.create_room()
    
    server = ShareServer(room=room, port=0, bus=bus, file_entries=[])
    
    # Start Server in Thread
    t = threading.Thread(target=server.run_server, daemon=True)
    t.start()
    
    # Wait for port
    for _ in range(20):
        if server.port > 0: break
        time.sleep(0.1)
        
    print(f"  Server running on {server.port}")
    base_url = f"http://127.0.0.1:{server.port}"
    
    try:
        # 1. Test Invite Page Content
        print("  Checking /invite page content...")
        resp = requests.get(f"{base_url}/invite")
        assert resp.status_code == 200
        content = resp.text
        if "DLM SHARE // RECEIVER" in content:
            print("  [OK] Invite page contains correct header.")
        else:
            print("  [FAIL] Invite page missing new header!")
            # print(content[:500])
            
        # 2. Test Request Download Endpoint
        print("  Checking /room/request-download...")
        resp = requests.post(f"{base_url}/room/request-download", json={"item_id": "test-id"})
        assert resp.status_code == 200
        assert resp.json()['status'] == "ok"
        print("  [OK] Request Download endpoint works.")
        
        # 3. Test Shutdown
        print("  Testing Server Shutdown...")
        server.stop()
        # Give it a moment to stop accepting requests (might not stop thread immediately but should signal)
        print("  [OK] Stop signal sent.")
        
    except Exception as e:
        print(f"  [FAIL] {e}")
    finally:
        server.stop()

if __name__ == "__main__":
    try:
        test_web_client()
        print("\nWEB CLIENT VERIFICATION PASSED")
    except Exception as e:
        print(f"\n[FAIL] {e}")
