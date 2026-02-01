import os
import sys
import shutil
import subprocess
from pathlib import Path
from typing import Tuple, Optional

def is_termux() -> bool:
    return "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in os.environ.get("PATH", "")

def _try_tkinter_picker() -> Tuple[bool, str]:
    """Attempt to use Tkinter file dialog (PC)."""
    try:
        if os.name == 'posix' and not os.environ.get('DISPLAY'):
            return False, ""
            
        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        
        file_path = filedialog.askopenfilename(
            title="Select file to share",
            filetypes=[("All Files", "*.*")]
        )
        root.destroy()
        return True, file_path or ""
    except Exception:
        return False, ""

def _try_termux_picker() -> Tuple[bool, str]:
    """
    Attempt to use Android intent via 'am' and 'termux-api' or fallback.
    
    Since 'am start' is fire-and-forget and doesn't return result to stdout easily without
    a helper app that catches the resultURI, this is tricky in pure script.
    
    However, if 'termux-api' is installed, 'termux-storage-get' opens a picker and returns the file path.
    This is the preferred way on Termux.
    """
    # Priority 1: termux-dialog file (Native Picker - Returns Path)
    if shutil.which("termux-dialog"):
        try:
            res = subprocess.run(
                ["termux-dialog", "file"], 
                capture_output=True, 
                text=True,
                timeout=15 # Prevents eternal freeze if API hangs
            )
            import json
            data = json.loads(res.stdout)
            if data.get("code") == 0 and data.get("text"):
                return True, data["text"].strip()
            elif data.get("code") == -1:
                return True, "" # User Cancelled explicitly
        except subprocess.TimeoutExpired:
            print("⚠️  Android picker timed out.")
        except Exception:
            pass

    # Priority 2: termux-storage-get (Copies file - Fallback)
    if shutil.which("termux-storage-get"):
        try:
            tmp_dir = Path.home() / ".dlm" / "share_tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            target_file = tmp_dir / "picked_file"
            
            # This copies content. It is heavy.
            # Timeout is crucial here.
            subprocess.run(
                ["termux-storage-get", str(target_file)], 
                capture_output=True,
                timeout=15
            )
            
            if target_file.exists() and target_file.stat().st_size > 0:
                return True, str(target_file)
        except subprocess.TimeoutExpired:
            print("⚠️  Storage picker timed out.")
        except Exception:
            pass

    return False, ""

def _try_ranger_picker() -> Tuple[bool, str]:
    """Attempt to use ranger console file manager."""
    if not shutil.which("ranger"):
        return False, ""
        
    try:
        # Create temp file for ranger to write validation
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
            
        # ranger --choosefile=TARGET
        # This opens ranger, user picks file, it writes path to TARGET and exits
        subprocess.run(["ranger", f"--choosefile={tmp_path}"], check=False)
        
        chosen_path = ""
        if os.path.exists(tmp_path):
            with open(tmp_path, 'r') as f:
                chosen_path = f.read().strip()
            os.unlink(tmp_path)
            
        if chosen_path:
            return True, chosen_path
            
        return False, ""
    except Exception:
        return False, ""

def pick_file() -> Optional[str]:
    """
    Select a file using available GUI or fallback to manual input.
    Returns absolute path or None if aborted.
    """
    path = ""
    picked_via_gui = False
    
    if is_termux():
        print("📱 Launching Android file picker...")
        success, path = _try_termux_picker()
        if success:
            picked_via_gui = True
        else:
            print("⚠️  Android picker failed/missing. Trying ranger...")
            success, path = _try_ranger_picker()
            if success:
                picked_via_gui = True
            else:
                 print("⚠️  ranger not found. Falling back to manual input.")
    else:
        print("🖥️  Launching System file picker...")
        success, path = _try_tkinter_picker()
        if success:
            picked_via_gui = True

    # If GUI failed or was cancelled (but picking was attempted), 
    # and we have no path, ask manually.
    if not path:
        if picked_via_gui:
            print("⚠️  File selection cancelled or failed.")
        
        print("⌨️  Please enter the absolute path to the file:")
        try:
            path = input("Path > ").strip().strip('"').strip("'")
        except KeyboardInterrupt:
            return None

    if not path:
        return None
        
    # Validate
    p = Path(path).expanduser().resolve()
    if not p.exists():
        print(f"❌ Error: File not found: {p}")
        return None
    if not p.is_file():
        print(f"❌ Error: Not a file: {p}")
        return None
        
    return str(p)
