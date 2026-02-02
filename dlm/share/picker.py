import os
import sys
import shutil
import subprocess
from pathlib import Path
from typing import Tuple, Optional

def is_termux() -> bool:
    return "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in os.environ.get("PATH", "")

from typing import Tuple, Optional, List, Callable
from .ranger import pick_with_ranger

def is_termux() -> bool:
    return "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in os.environ.get("PATH", "")

def _try_tkinter_picker() -> List[Path]:
    """Attempt to use Tkinter file/folder dialog (PC) for multi-selection."""
    try:
        if os.name == 'posix' and not os.environ.get('DISPLAY'):
            return []
            
        import tkinter as tk
        from tkinter import filedialog, messagebox
        
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        
        # 1. Ask user if they want to add files or folders
        # For simplicity, we just ask for files first, then ask if they want to add a folder.
        paths = []
        
        # Ask for files
        files = filedialog.askopenfilenames(
            title="Select Files to Share (Cancel to skip to Folder)",
            filetypes=[("All Files", "*.*")]
        )
        if files:
            paths.extend([Path(p) for p in files])
            
        # Ask for folder too?
        if messagebox.askyesno("Add Folder?", "Would you like to add a folder as a unit (preserving structure)?"):
            folder = filedialog.askdirectory(title="Select Folder to Share")
            if folder:
                paths.append((Path(folder), True))
        
        # Files are never folder-units
        final_paths = [(Path(p), False) for p in files] + ([p for p in paths if isinstance(p, tuple)] if paths else [])
        
        root.destroy()
        return final_paths
    except Exception:
        return []

def launch_picker(on_add: Callable[[Path, bool], int], bus=None):
    """
    Launch the appropriate picker for the platform.
    Calls on_add(Path, as_folder) for each selected item.
    """
    if is_termux():
        # Use our new Ranger browser
        pick_with_ranger(on_add)
    else:
        # Use Tkinter for Desktop
        items = _try_tkinter_picker()
        for p, is_dir in items:
            on_add(p, is_dir)

# Legacy compatibility for single file picking (if needed elsewhere)
def pick_file() -> Optional[str]:
    """Legacy: pick a single file."""
    paths = []
    def adder(p, is_dir): paths.append(p); return 1
    launch_picker(adder)
    return str(paths[0]) if paths else None
