import os
import sys
from setuptools import setup, find_packages

def is_termux():
    path = os.environ.get("PATH", "")
    return "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in path

# --- AUTOMATED SYSTEM SETUP ---
if is_termux() and "install" in sys.argv:
    import subprocess
    print("📱 Termux detected. Attempting to install system dependencies (libtorrent, ffmpeg)...")
    try:
        # Try to install system dependencies silently
        subprocess.run(["pkg", "install", "-y", "python-libtorrent", "ffmpeg"], check=False)
    except Exception:
        print("⚠️ Warning: Failed to run 'pkg install' automatically. Please run 'dlm setup' after installation.")
# ------------------------------

CORE_DEPS = [
    "yt-dlp",
    "requests",
    "python-dotenv",
    "colorama",
    "fastapi",
    "uvicorn",
    "prompt_toolkit",
    "zeroconf",
    "qrcode",
    "psutil",
    "netifaces",
]

DESKTOP_DEPS = [
    "cryptography",
    "curl_cffi",
    "tk",
    "libtorrent",
    "numpy<2",
    "torch",
    "torchaudio==2.1.0",
    "demucs==4.0.0",
    "soundfile",
]

install_requires = CORE_DEPS
if not is_termux():
    # Automatically include desktop deps on non-termux environments
    install_requires += DESKTOP_DEPS

setup(
    name="dlm",
    version="0.1.0",
    packages=find_packages(),
    install_requires=install_requires,
    extras_require={
        "desktop": DESKTOP_DEPS,
        "torrent": ["libtorrent"],
        "vocals": [
            "numpy<2",
            "torch",
            "torchaudio==2.1.0",
            "demucs==4.0.0",
            "soundfile",
        ],
    },
    entry_points={
        "console_scripts": [
            "dlm=dlm.main:main",
        ],
    },
)
