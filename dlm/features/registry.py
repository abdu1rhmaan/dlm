from .models import Feature, PipDependency, BinaryDependency, PythonImportDependency
import os

def is_termux():
    return "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in os.environ.get("PATH", "")

# Common Dependencies
FFMPEG = BinaryDependency("ffmpeg", shared=True)
UVICORN = PipDependency("uvicorn", shared=True)
FASTAPI = PipDependency("fastapi", shared=True)
REQUESTS = PipDependency("requests", shared=True)
YT_DLP = PipDependency("yt_dlp") # Specific to Social, not Core

FEATURES = [
    Feature(
        id="downloader",
        name="Downloader Core",
        description="High-speed HTTP/S multi-part downloads",
        dependencies=[
            REQUESTS,
            PipDependency("urllib3", shared=True)
        ],
        is_core=True,
        category="Core System"
    ),

    Feature(
        id="social",
        name="Social Media Support",
        description="YouTube, TikTok, Facebook, etc.",
        dependencies=[YT_DLP, FFMPEG],
        category="Social Media"
    ),
    Feature(
        id="spotify",
        name="Spotify",
        description="Extract metadata and stream",
        dependencies=[
            PipDependency("curl_cffi"), # Not shared with Core
            REQUESTS
        ],
        category="Social Media"
    ),
    Feature(
        id="vocab", # Typo fixed
        name="Vocals Separator",
        description="AI-powered vocal separation",
        dependencies=[
            PipDependency("demucs"),
            FFMPEG
        ],
        category="AI Tools"
    ),
    Feature(
        id="torrent",
        name="BitTorrent",
        description="High-performance torrent downloading",
        dependencies=[
            PipDependency("libtorrent") 
        ],
        category="Tools"
    ),
    Feature(
        id="browser",
        name="Browser Capture",
        description="Capture links from anti-bot sites",
        dependencies=[
            PipDependency("playwright")
        ],
        category="Tools"
    ),
    Feature(
        id="share",
        name="Share - Local Network",
        description="Share files on local network (LAN)",
        dependencies=[
            PipDependency("textual"),
            FASTAPI,
            UVICORN,
            PipDependency("zeroconf"),
            PipDependency("netifaces"), # Better than socket
            PipDependency("qrcode"),
            PipDependency("psutil")
        ],
        category="Tools"
    )
]

def get_feature(feature_id: str):
    for f in FEATURES:
        if f.id == feature_id: return f
    return None
