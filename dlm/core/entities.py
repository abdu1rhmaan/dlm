from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Tuple
from datetime import datetime
import uuid

class DownloadState(Enum):
    QUEUED = "QUEUED"
    INITIALIZING = "INITIALIZING"
    WAITING = "WAITING"
    DOWNLOADING = "DOWNLOADING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

@dataclass
class Segment:
    """Represents a byte range of the file."""
    start_byte: int
    end_byte: int
    downloaded_bytes: int = 0
    last_checkpoint: int = 0  # Validated safe offset
    start_hash: Optional[str] = None  # Hash of first N bytes
    end_hash: Optional[str] = None    # Hash of last N bytes
    part_number: Optional[int] = None # Original part number for partial downloads
    
    @property
    def is_complete(self) -> bool:
        return self.downloaded_bytes >= (self.end_byte - self.start_byte + 1)
    
    # Torrent-specific extensions
    @property
    def piece_range(self) -> Optional[Tuple[int, int]]:
        """Get piece range for torrent downloads"""
        if not hasattr(self, '_piece_range'):
            return None
        return self._piece_range
    
    def set_piece_range(self, start_piece: int, end_piece: int):
        """Set piece range for torrent mapping"""
        self._piece_range = (start_piece, end_piece)

class ResumeState(Enum):
    STABLE = "STABLE"
    UNSTABLE = "UNSTABLE"

class IntegrityState(Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    CORRUPT = "CORRUPT"

@dataclass
class Download:
    """Aggregate root for a download task."""
    url: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    target_filename: Optional[str] = None
    total_size: int = 0
    state: DownloadState = DownloadState.QUEUED
    segments: List[Segment] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    error_message: Optional[str] = None
    last_update: datetime = field(default_factory=datetime.now)
    speed_bps: float = 0.0  # bytes per second
    resumable: bool = True  # Defaults to True until detected
    resume_state: ResumeState = ResumeState.STABLE
    integrity_state: IntegrityState = IntegrityState.PENDING
    max_connections: int = 4
    partial: bool = False  # True if this is a partial download from manifest
    task_id: Optional[str] = None  # Original task ID for collaborative downloads
    assigned_parts_summary: Optional[str] = None
    
    # Media Extraction Metadata
    source: Optional[str] = None  # e.g., 'youtube', 'torrent', 'http'
    media_type: Optional[str] = None  # 'video' | 'audio'
    quality: Optional[str] = None  # e.g., '720p'
    conversion_required: bool = False
    cut_range: Optional[str] = None  # '00:00:10-00:00:20'
    duration: Optional[float] = None  # Total duration in seconds
    current_stage: Optional[str] = None  # 'resolving', 'downloading', 'converting', 'cutting', 'done'
    audio_mode: Optional[str] = None  # e.g., 'vocals'
    vocals_gpu: bool = False
    vocals_keep_all: bool = False
    # Internal override for non-segment based progress (e.g. yt-dlp hook)
    _manual_progress: Optional[float] = None
    _downloaded_bytes_override: Optional[int] = None
    
    # Output path for the download
    output_path: Optional[str] = None
    
    # Source URL for the download
    source_url: Optional[str] = None
    
    # Referer for the download
    referer: Optional[str] = None

    # Storage state (cookies/headers) for browser downloads
    storage_state: Optional[str] = None

    # Link to browser_downloads table ID if this download was started from a capture
    browser_capture_id: Optional[int] = None

    # User Agent used during capture
    user_agent: Optional[str] = None

    # Captured session data
    captured_headers: list | dict = field(default_factory=list)
    captured_cookies: dict = field(default_factory=dict)

    # Fallback size probe flag
    probed_via_stream: bool = False

    # Real download probe flag (1DM-style)
    browser_probe_done: bool = False

    # Torrent-specific data
    torrent_files: List[int] = field(default_factory=list)
    torrent_info_hash: Optional[str] = None
    torrent_piece_length: Optional[int] = None
    torrent_file_offset: int = 0

    # Folder Link
    folder_id: Optional[int] = None
    
    # Live/Ephemeral Flag (No DB persistence)
    ephemeral: bool = False

    @property
    def is_cut(self) -> bool:
        return bool(self.cut_range)

    def reset_progress(self):
        """Reset all progress-related fields for a fresh start."""
        self._manual_progress = None
        self._downloaded_bytes_override = None
        self.error_message = None
        self.current_stage = None
        self.segments = []
        self.speed_bps = 0.0

    @property
    def progress_mode(self) -> str:
        """Returns 'stages' for non-byte downloads (like YouTube), else 'bytes'."""
        if self.source == 'youtube':
            if self.partial:
                return 'youtube-partial'
            return 'stages'
        elif self.source == 'torrent':
            return 'torrent-pieces'
        return 'bytes'
    
    @property
    def progress(self) -> float:
        """Returns the progress percentage (0-100)."""
        # Prioritize manual override (from yt-dlp hooks)
        if self._manual_progress is not None:
            return self._manual_progress
            
        p = self.calculate_progress()
        return p if p is not None else 0.0

    def calculate_progress(self) -> Optional[float]:
        """Calculate progress percentage. Returns None for stage-based downloads."""
        if self.progress_mode == 'stages':
            # Support byte-based progress in stages mode if total_size is known
            if self.total_size and self.total_size > 0:
                return (self.get_downloaded_bytes() / self.total_size) * 100.0
            return None
        elif self.progress_mode == 'torrent-pieces':
            # For torrents, calculate based on piece completion
            if not self.segments or not self.torrent_piece_length:
                return 0.0
            
            total_pieces = sum(
                (seg.piece_range[1] - seg.piece_range[0] + 1) 
                for seg in self.segments 
                if seg.piece_range
            ) if all(seg.piece_range for seg in self.segments) else 0
            
            if total_pieces == 0:
                return 0.0
                
            completed_pieces = sum(
                seg.downloaded_bytes // self.torrent_piece_length
                for seg in self.segments
            )
            
            return (completed_pieces / total_pieces) * 100.0
            
        if not self.segments and (not self.total_size or self.total_size == 0):
            return 0.0
        
        # For partial downloads, calculate based on assigned segments only
        if self.partial:
            total_assigned = sum(seg.end_byte - seg.start_byte + 1 for seg in self.segments)
            if total_assigned == 0:
                return 0.0
            total_downloaded = self.get_downloaded_bytes()
            return (total_downloaded / total_assigned) * 100.0
        
        # For full downloads, use total_size
        if not self.total_size or self.total_size == 0:
            return 0.0
        total_downloaded = self.get_downloaded_bytes()
        return (total_downloaded / self.total_size) * 100.0
    
    def get_downloaded_bytes(self) -> int:
        if self._downloaded_bytes_override is not None:
            return self._downloaded_bytes_override
        return sum(s.downloaded_bytes for s in self.segments)

    def fail(self, message: str) -> None:
        self.state = DownloadState.FAILED
        self.error_message = message

    def complete(self) -> None:
        self.state = DownloadState.COMPLETED