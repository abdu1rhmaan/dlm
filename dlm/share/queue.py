"""Transfer queue for multi-file sharing."""

from dataclasses import dataclass, field
from typing import List, Optional
from pathlib import Path


@dataclass
class QueuedFile:
    """Represents a file in the transfer queue."""
    file_path: Path
    target_devices: List[str]  # Device IDs to send to
    status: str = "pending"  # pending, transferring, completed, failed, skipped
    progress: float = 0.0
    error: Optional[str] = None
    
    @property
    def file_name(self) -> str:
        """Get file name."""
        return self.file_path.name
    
    @property
    def file_size(self) -> int:
        """Get file size in bytes."""
        try:
            return self.file_path.stat().st_size
        except:
            return 0


class TransferQueue:
    """Manages multi-file transfer queue."""
    
    def __init__(self):
        self.queue: List[QueuedFile] = []
        self.current_index: int = 0
    
    def add_file(self, file_path: Path, target_devices: List[str]):
        """Add file to queue."""
        queued_file = QueuedFile(
            file_path=file_path,
            target_devices=target_devices
        )
        self.queue.append(queued_file)
    
    def add_files(self, file_paths: List[Path], target_devices: List[str]):
        """Add multiple files to queue."""
        for file_path in file_paths:
            self.add_file(file_path, target_devices)
    
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
        return self.current_index >= len(self.queue)
    
    def get_pending_count(self) -> int:
        """Get number of pending files."""
        return sum(1 for f in self.queue if f.status == "pending")
    
    def get_completed_count(self) -> int:
        """Get number of completed files."""
        return sum(1 for f in self.queue if f.status == "completed")
    
    def get_failed_count(self) -> int:
        """Get number of failed files."""
        return sum(1 for f in self.queue if f.status == "failed")
    
    def clear(self):
        """Clear the queue."""
        self.queue = []
        self.current_index = 0
    
    def __len__(self) -> int:
        """Get total number of files in queue."""
        return len(self.queue)
