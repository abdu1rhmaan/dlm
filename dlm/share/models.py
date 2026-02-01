from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional
import uuid

@dataclass
class FileEntry:
    file_id: str
    name: str
    size_bytes: int
    absolute_path: str

    @classmethod
    def from_path(cls, path: str) -> 'FileEntry':
        import os
        from pathlib import Path
        
        p = Path(path).resolve()
        if not p.is_file():
            raise ValueError(f"Path is not a file: {path}")
            
        stats = p.stat()
        return cls(
            file_id=str(uuid.uuid4()),
            name=p.name,
            size_bytes=stats.st_size,
            absolute_path=str(p)
        )

@dataclass
class Room:
    room_id: str
    token: str
    files: List[FileEntry]
    created_at: datetime = field(default_factory=datetime.now)
    ttl_minutes: int = 15
    
    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(minutes=self.ttl_minutes)
    
    @property
    def is_expired(self) -> bool:
        return datetime.now() > self.expires_at

@dataclass
class Session:
    session_id: str
    token: str
    expires_at: datetime
    created_at: datetime = field(default_factory=datetime.now)
    
    @property
    def is_valid(self) -> bool:
        return datetime.now() <= self.expires_at
