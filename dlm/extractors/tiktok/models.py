from dataclasses import dataclass

@dataclass
class TikTokMetadata:
    """Metadata for TikTok videos."""
    video_id: str
    title: str = ""
    author: str = ""
    duration: float = 0.0
    total_count: int = 0  # To store total videos in profile

