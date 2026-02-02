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
    """Attempt to use Tkinter file dialog (PC) for multi-selection."""
    try:
        if os.name == 'posix' and not os.environ.get('DISPLAY'):
            return []
            
        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        
        # 1. Ask for files
        files = filedialog.askopenfilenames(
            title="Select files to share",
            filetypes=[("All Files", "*.*")]
        )
        
        # 2. Ask for folders (Optional secondary step for Desktop)
        # Note: tkinter filedialog can't do both simultaneously easily.
        # For now, if files are empty, or as a supplement, we could ask.
        # But askopenfilenames is usually enough for files.
        
        root.destroy()
        return [Path(p) for p in files] if files else []
    except Exception:
        return []

def launch_picker(on_add: Callable[[Path], int], bus=None):
    """
    Launch the appropriate picker for the platform.
    Calls on_add(Path) for each selected item.
    """
    if is_termux():
        # Use our new Ranger browser
        pick_with_ranger(on_add)
    else:
        # Use Tkinter for Desktop
        paths = _try_tkinter_picker()
        for p in paths:
            on_add(p)
            
        # Also option for folder selection on Desktop?
        # Let's keep it simple for now as requested.
        pass

# Legacy compatibility for single file picking (if needed elsewhere)
def pick_file() -> Optional[str]:
    """Legacy: pick a single file."""
    paths = []
    def adder(p): paths.append(p); return 1
    launch_picker(adder)
    return str(paths[0]) if paths else None
