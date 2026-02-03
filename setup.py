import os
import sys
from setuptools import setup, find_packages

def is_termux():
    path = os.environ.get("PATH", "")
    return "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in path

# --- CORE DEPENDENCIES (Always installed) ---
# These are the MINIMUM required for the downloader core to function
CORE_DEPS = [
    "requests",      # HTTP client (Core)
    "urllib3",       # HTTP connection pooling (Core)
    "colorama",      # Terminal colors
    "python-dotenv", # Environment variables
    "prompt_toolkit", # TUI for Feature Manager and REPL
]

# --- OPTIONAL FEATURE DEPENDENCIES ---
# Users install these via Feature Manager (dlm launcher)
SHARE_DEPS = [
    "textual",
    "fastapi",
    "uvicorn",
    "zeroconf",
    "netifaces",
    "qrcode",
    "psutil",
]

SOCIAL_DEPS = [
    "yt-dlp",
]

SPOTIFY_DEPS = [
    "curl_cffi",
]

TORRENT_DEPS = [
    "libtorrent",
]

VOCALS_DEPS = [
    "numpy<2",
    "torch",
    "torchaudio==2.1.0",
    "demucs==4.0.0",
    "soundfile",
]

# On Termux, exclude heavy desktop-only packages
if is_termux():
    TORRENT_DEPS = []  # Use pkg install python-libtorrent
    VOCALS_DEPS = []   # Too heavy for mobile

setup(
    name="dlm",
    version="0.1.0",
    packages=find_packages(),
    install_requires=CORE_DEPS,  # Only Core
    extras_require={
        "share": SHARE_DEPS,
        "social": SOCIAL_DEPS,
        "spotify": SPOTIFY_DEPS,
        "torrent": TORRENT_DEPS,
        "vocals": VOCALS_DEPS,
        "all": SOCIAL_DEPS + SPOTIFY_DEPS + TORRENT_DEPS + VOCALS_DEPS + SHARE_DEPS,
    },
    entry_points={
        "console_scripts": [
            "dlm=dlm.main:main",
        ],
    },
)
