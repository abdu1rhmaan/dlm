"""On-demand dependency installer for DLM features."""

import sys
import subprocess
from typing import List, Callable, Optional


class FeatureInstaller:
    """Handles programmatic pip installation for features."""
    
    @staticmethod
    def install(dependencies: List[str], on_progress: Optional[Callable[[str], None]] = None) -> bool:
        """Install a list of pip packages."""
        if not dependencies:
            return True
            
        try:
            # Programmatically call pip
            cmd = [sys.executable, "-m", "pip", "install"] + dependencies
            
            if on_progress:
                on_progress(f"Installing {', '.join(dependencies)}...")
                
            # Use subprocess to see output if needed, or pipe it
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )
            
            if on_progress:
                for line in process.stdout:
                    # Filter noise
                    if "Requirement already satisfied" in line: continue
                    on_progress(line.strip()[:60]) # Truncate for TUI
                    
            process.wait()
            return process.returncode == 0
            
        except Exception as e:
            if on_progress:
                on_progress(f"Error: {e}")
            return False

    @classmethod
    def install_feature(cls, feature_id: str, registry, on_progress: Optional[Callable[[str], None]] = None) -> bool:
        feature = registry.get_feature(feature_id)
        if not feature:
            return False
            
        # Termux: specific system dependencies
        import os
        is_termux = "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in os.environ.get("PATH", "")
        
        if is_termux:
            if feature_id == "torrent":
                if on_progress: on_progress("Termux detected: Installing system libtorrent...")
                subprocess.run(["pkg", "install", "-y", "python-libtorrent"], check=False)
            elif feature_id in ["youtube", "downloader"]:
                if on_progress: on_progress("Termux detected: Installing ffmpeg...")
                subprocess.run(["pkg", "install", "-y", "ffmpeg"], check=False)

        return cls.install(feature.pip_dependencies, on_progress)
