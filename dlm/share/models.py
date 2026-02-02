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
    is_dir: bool = False
    owner_device_id: str = None  # To track who shared it

    @classmethod
    def from_path(cls, path: str) -> 'FileEntry':
        import os
        from pathlib import Path
        
        p = Path(path).resolve()
        if not p.exists():
             raise ValueError(f"Path does not exist: {path}")
            
        is_dir = p.is_dir()
        size = 0
        if not is_dir:
             size = p.stat().st_size
        else:
             # Calculate recursive size
             try:
                 size = sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
             except:
                 pass

        return cls(
            file_id=str(uuid.uuid4()),
            name=p.name,
            size_bytes=size,
            absolute_path=str(p),
            is_dir=is_dir
        )
