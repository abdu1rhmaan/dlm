from dataclasses import dataclass, field
from typing import Any, List, Optional

@dataclass
class ExtractResult:
    """
    Unified result contract for all media extractors.
    
    This represents the output of the metadata extraction phase.
    It does NOT contain downloaded data, but rather the instructions 
    and metadata required to perform a download later.
    """
    platform: str
    source_url: str
    metadata: Any  # Platform-specific metadata object (e.g. YouTubeMetadata)
    streams: List[Any] = field(default_factory=list) # Placeholder for stream/format info
    is_collection: bool = False # True for playlists, albums, etc.
    entries: List[Any] = field(default_factory=list) # List of sub-results or dicts for collection items
