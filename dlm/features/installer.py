import sys
import subprocess
import os
import importlib
from typing import List, Callable, Optional
from .models import Feature, Dependency, PipDependency, BinaryDependency

class FeatureInstaller:
    """Handles installation of feature dependencies."""
    
    @staticmethod
    def is_termux():
        return "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in os.environ.get("PATH", "")

    @classmethod
    def install_feature(cls, feature: Feature, on_progress: Callable[[str], None] = print) -> bool:
        """Install missing dependencies for a feature."""
        missing = [d for d in feature.dependencies if not d.is_met()]
        
        if not missing:
            on_progress(f"All dependencies for {feature.name} are already met.")
            return True

        on_progress(f"Installing dependencies for {feature.name}...")
        
        for dep in missing:
            on_progress(f"Installing {dep.name}...")
            if not cls._install_dependency(dep, on_progress):
                on_progress(f"Failed to install {dep.name}.")
                return False
                
        on_progress(f"Successfully installed {feature.name}.")
        return True

    @classmethod
    def _install_dependency(cls, dep: Dependency, on_progress: Callable[[str], None]) -> bool:
        """Dispatch installation based on dependency type and environment."""
        
        # SPECIAL HANDLING FOR TERMUX
        if cls.is_termux():
            if isinstance(dep, BinaryDependency) and dep.name == "ffmpeg":
                return cls._run_command(["pkg", "install", "-y", "ffmpeg"], on_progress)
            if isinstance(dep, PipDependency) and dep.package_name == "libtorrent":
                # On Termux, libtorrent is a pkg
                return cls._run_command(["pkg", "install", "-y", "python-libtorrent"], on_progress)

        # DEFAULT PIP / COMMAND HANDLING
        cmd = dep.install_command()
        if not cmd:
            on_progress(f"Manual installation required for {dep.name}.")
            return False
            
        return cls._run_command(cmd, on_progress)

    @classmethod
    def uninstall_feature(cls, feature: Feature, on_progress: Callable[[str], None] = print) -> bool:
        """Uninstall dependencies for a feature."""
        # Only uninstall dependencies that are actually met
        installed = [d for d in feature.dependencies if d.is_met()]
        
        if not installed:
            on_progress(f"No installed dependencies found for {feature.name}.")
            return True

        on_progress(f"Uninstalling dependencies for {feature.name}...")
        
        for dep in installed:
            on_progress(f"Removing {dep.name}...")
            if not cls._uninstall_dependency(dep, on_progress):
                on_progress(f"Failed to remove {dep.name} (might be shared or manual).")
                # Continue trying others
        
        on_progress(f"Finished uninstalling {feature.name}.")
        return True

    @classmethod
    def _uninstall_dependency(cls, dep: Dependency, on_progress: Callable[[str], None]) -> bool:
        if dep.shared:
            on_progress(f"Skipping shared dependency: {dep.name}")
            return True

        # SPECIAL HANDLING FOR TERMUX
        if cls.is_termux():
            # Be careful uninstalling system packages like ffmpeg if other things use them!
            # For safety, maybe ASK or just Skip system binaries?
            # User asked for uninstall capability.
            if isinstance(dep, BinaryDependency) and dep.name == "ffmpeg":
                # Assuming user wants to remove it.
                return cls._run_command(["pkg", "uninstall", "-y", "ffmpeg"], on_progress)
            if isinstance(dep, PipDependency) and dep.package_name == "libtorrent":
                return cls._run_command(["pkg", "uninstall", "-y", "python-libtorrent"], on_progress)

        cmd = dep.uninstall_command()
        if not cmd:
            on_progress(f"Skipping {dep.name} (Manual uninstall only).")
            return True # Not a failure, just skipped
            
        return cls._run_command(cmd, on_progress)

    @staticmethod
    def _run_command(cmd: List[str], on_progress: Callable[[str], None]) -> bool:
        try:
            # Programmatically call pip
            # cmd is already a list
            
            if on_progress:
                # Truncate command for display
                display_cmd = " ".join(cmd)
                if len(display_cmd) > 50: display_cmd = display_cmd[:47] + "..."
                # on_progress(f"Running: {display_cmd}")
                
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
                    if "Running command" in line: continue
                    
                    clean_line = line.strip()
                    if clean_line:
                        on_progress(clean_line[:65]) # Truncate for TUI
                    
            process.wait()
            
            # CRITICAL: Invalidate import caches so is_met() checks are fresh
            importlib.invalidate_caches()
            
            return process.returncode == 0
            
        except Exception as e:
            if on_progress:
                on_progress(f"Error: {e}")
            return False
