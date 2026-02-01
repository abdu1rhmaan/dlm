from abc import ABC, abstractmethod
from typing import Iterator, Optional, Tuple, Dict
from pathlib import Path

class NetworkAdapter(ABC):
    @abstractmethod
    def get_content_length(self, url: str, referer: Optional[str] = None, headers: Optional[Dict] = None, cookies: Optional[Dict] = None, user_agent: Optional[str] = None) -> Optional[int]:
        """Returns the content length in bytes, or None if unknown."""
        pass

    @abstractmethod
    def supports_ranges(self, url: str, referer: Optional[str] = None, headers: Optional[Dict] = None, cookies: Optional[Dict] = None, user_agent: Optional[str] = None) -> bool:
        """Checks if the server supports byte ranges."""
        pass

    @abstractmethod
    def download_range(self, url: str, start: int, end: int, referer: Optional[str] = None, headers: Optional[Dict] = None, cookies: Optional[Dict] = None, user_agent: Optional[str] = None) -> Iterator[bytes]:
        """Yields chunks of bytes for the specified range."""
        pass
    
    @abstractmethod
    def download_stream(self, url: str, referer: Optional[str] = None, headers: Optional[Dict] = None, cookies: Optional[Dict] = None, user_agent: Optional[str] = None) -> Iterator[bytes]:
        """Yields chunks of bytes for the whole file (no range)."""
        pass

class FileAdapter(ABC):
    @abstractmethod
    def write_chunk(self, filepath: Path, offset: int, data: bytes) -> None:
        pass
