from dataclasses import dataclass, field
from typing import List, Optional
import shutil
from importlib.metadata import distribution, PackageNotFoundError

@dataclass
class Feature:
    id: str
    name: str
    description: str
    pip_dependencies: List[str] = field(default_factory=list)
    system_dependencies: List[str] = field(default_factory=list) # e.g. ffmpeg
    is_core: bool = False
    estimated_size: str = "~50MB"

    def is_installed(self) -> bool:
        """Check if all dependencies are satisfied."""
        # 1. Check Pip Dependencies
        for dep in self.pip_dependencies:
            try:
                distribution(dep)
            except PackageNotFoundError:
                return False
        
        # 2. Check System Dependencies
        for bin_name in self.system_dependencies:
            if not shutil.which(bin_name):
                return False
                
        return True

FEATURES = [
    Feature(
        id="downloader",
        name="Downloader Core",
        description="High-speed HTTP/S multi-part downloads",
        pip_dependencies=["requests", "urllib3"],
        is_core=True
    ),
    Feature(
        id="share",
        name="LAN Share (Phase 2)",
        description="Keyboard-first local file sharing TUI",
        pip_dependencies=["fastapi", "uvicorn", "zeroconf", "qrcode", "prompt_toolkit"]
    ),
    Feature(
        id="youtube",
        name="Social - YouTube",
        description="Download videos and playlists from YouTube",
        pip_dependencies=["yt-dlp"],
        system_dependencies=["ffmpeg"]
    ),
    Feature(
        id="tiktok",
        name="Social - TikTok",
        description="Download videos and profiles from TikTok",
        pip_dependencies=["yt-dlp"],
        system_dependencies=["ffmpeg"]
    ),
    Feature(
        id="facebook",
        name="Social - Facebook",
        description="Download videos from Facebook",
        pip_dependencies=["yt-dlp"],
        system_dependencies=["ffmpeg"]
    ),
    Feature(
        id="spotify",
        name="Media - Spotify",
        description="Extract metadata and stream from Spotify",
        pip_dependencies=["curl_cffi", "requests"]
    ),
    Feature(
        id="torrent",
        name="BitTorrent",
        description="High-performance torrent downloading",
        pip_dependencies=["libtorrent"] # Note: Installer handles Termux pkg vs pip
    ),
    Feature(
        id="browser",
        name="Browser Capture",
        description="Capture links and sessions from anti-bot sites",
        pip_dependencies=["playwright"]
    ),
    Feature(
        id="vocals",
        name="Vocals Separator",
        description="AI-powered vocal and music separation",
        pip_dependencies=["demucs"],
        system_dependencies=["ffmpeg"]
    ),
]

def get_feature(feature_id: str) -> Optional[Feature]:
    """Get a feature by ID."""
    for f in FEATURES:
        if f.id == feature_id:
            return f
    return None
