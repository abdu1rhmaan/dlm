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
