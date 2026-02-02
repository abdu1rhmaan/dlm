"""Transfer queue for multi-file sharing."""

from dataclasses import dataclass, field
from typing import List, Optional
from pathlib import Path


@dataclass
class QueuedItem:
    """Represents a file or folder in the transfer queue."""
    file_path: Path
    is_dir: bool = False
    # Phase 2 Targeting
    target_device_id: str = "ALL"
    added_by: str = "local"
    
    status: str = "pending"  # pending, transferring, completed, failed, skipped
    progress: float = 0.0
    error: Optional[str] = None
    
    @property
    def file_name(self) -> str:
        """Get file name."""
        return self.file_path.name
    
    @property
    def file_size(self) -> int:
        """Get file or directory size in bytes."""
        try:
            if not self.is_dir:
                return self.file_path.stat().st_size
            else:
                return sum(f.stat().st_size for f in self.file_path.rglob('*') if f.is_file())
        except:
            return 0

# Alias for compatibility
QueuedFile = QueuedItem


class TransferQueue:
    """Manages multi-file/folder transfer queue."""
    
    def __init__(self):
        self.queue: List[QueuedItem] = []
        self.current_index: int = 0
    
    def add_path(self, path: Path, target_device_id: str = "ALL", as_folder: bool = False):
        """Add file or folder to queue."""
        if path.is_file():
            self._add_single_item(path, target_device_id, is_dir=False)
            return 1
        elif path.is_dir():
            if as_folder:
                # Add as a single directory unit
                self._add_single_item(path, target_device_id, is_dir=True)
                return 1
            else:
                # Legacy: Expand recursively (add independent files)
                count = 0
                for p in path.rglob("*"):
                    if p.is_file():
                        self._add_single_item(p, target_device_id, is_dir=False)
                        count += 1
                return count
        return 0

    def _add_single_item(self, file_path: Path, target_device_id: str, is_dir: bool):
        # Prevent duplicates
        if any(f.file_path == file_path for f in self.queue):
            return
            
        queued_item = QueuedItem(
            file_path=file_path,
            is_dir=is_dir,
            target_device_id=target_device_id
        )
        self.queue.append(queued_item)
    
    def remove_item(self, index: int):
        """Remove item by index."""
        if 0 <= index < len(self.queue):
            self.queue.pop(index)
            if self.current_index > 0 and self.current_index > index:
                self.current_index -= 1
    
    def get_current(self) -> Optional[QueuedFile]:
        """Get current file being transferred."""
        if 0 <= self.current_index < len(self.queue):
            return self.queue[self.current_index]
        return None
    
    def mark_completed(self):
        """Mark current file as completed and move to next."""
        if self.current_index < len(self.queue):
            self.queue[self.current_index].status = "completed"
            self.queue[self.current_index].progress = 100.0
            self.current_index += 1
    
    def mark_failed(self, error: str):
        """Mark current file as failed and move to next."""
        if self.current_index < len(self.queue):
            self.queue[self.current_index].status = "failed"
            self.queue[self.current_index].error = error
            self.current_index += 1
    
    def skip_current(self):
        """Skip current file and move to next."""
        if self.current_index < len(self.queue):
            self.queue[self.current_index].status = "skipped"
            self.current_index += 1
    
    def update_progress(self, progress: float):
        """Update progress of current file."""
        if self.current_index < len(self.queue):
            self.queue[self.current_index].progress = progress
            if self.queue[self.current_index].status == "pending":
                self.queue[self.current_index].status = "transferring"
    
    def is_complete(self) -> bool:
        """Check if all files are processed."""
        # A queue is complete if there are no more 'pending' or 'transferring' items starting from current_index
        return self.current_index >= len(self.queue)
    
    def get_pending_items(self) -> List[QueuedFile]:
        """Get all pending files."""
        return [f for f in self.queue[self.current_index:] if f.status == "pending"]

    def clear_completed(self):
        """Remove completed and failed items from queue."""
        self.queue = [f for f in self.queue if f.status not in ("completed", "failed", "skipped")]
        self.current_index = 0

    def clear(self):
        """Clear the entire queue."""
        self.queue = []
        self.current_index = 0
    
    def __len__(self) -> int:
        """Get total number of files in queue."""
        return len(self.queue)
