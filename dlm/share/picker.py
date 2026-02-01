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
    if not shutil.which("termux-storage-get"):
        return False, ""
        
    try:
        # termux-storage-get copies the selected file to a specific path? 
        # No, it "Requests a file from the system and outputs to the specified file."
        # Usage: termux-storage-get output-file
        
        # We need a temp location
        tmp_dir = Path.home() / ".dlm" / "share_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target_file = tmp_dir / "picked_file"
        
        # This command blocks until user picks a file
        result = subprocess.run(
            ["termux-storage-get", str(target_file)], 
            capture_output=True
        )
        
        if result.returncode == 0 and target_file.exists() and target_file.stat().st_size > 0:
            # The file is COPIED here. We return this path.
            # Ideally we want the original path to avoid copy, but Android SAF (Storage Access Framework)
            # doesn't give direct paths easily. Copying is safe.
            # But wait, 'dlm share' likely wants to just READ.
            # But termux-storage-get output is the only way to get the content.
            # We must rename it to preserve extension if possible? 
            # termux-storage-get doesn't tell us the original name :(
            # That's a limitation.
            
            # Let's try 'termux-file-editor' approach? No, that's for "Share -> Termux".
            
            # For Phase 1, if we use termux-storage-get, we lose the filename unless we ask user.
            # Fallback: Let's stick to manual path for now if termux-storage-get is weak.
            # OR: usage of 'am' with a custom intent is complex.
            
            # Re-reading prompt: 
            # "Attempt to open Android system file picker using intent: am start ... Capture returned content URI"
            # This implies the user *thinks* we can capture it. 
            # In standard shell, 'am start' returns immediately.
            # Without a resident Java helper, we can't get the result Intent.
            
            # BEST EFFORT: Check for 'termux-dialog file' (part of termux-api widget)? 
            # 'termux-dialog file' is a thing! 
            # Let's try that first.
            pass
        
        # Try termux-dialog file
        if shutil.which("termux-dialog"):
            # termux-dialog file
            # Output: JSON with 'text' field containing path? No, 'code' and 'text'.
            res = subprocess.run(
                ["termux-dialog", "file"], 
                capture_output=True, 
                text=True
            )
            # Output format: {"code":0, "text":"/storage/emulated/0/Download/foo.mp4"}
            # This only works if Termux has storage permission and can see the file.
            import json
            try:
                data = json.loads(res.stdout)
                if data.get("code") == -1: # Cancelled
                    return True, "" # Gui worked but cancelled
                if data.get("code") == 0 and data.get("text"):
                    return True, data["text"]
            except:
                pass

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
