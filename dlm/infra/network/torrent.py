import threading
import time
from typing import Iterator, Optional, Dict, List, Tuple
from pathlib import Path
from dlm.core.interfaces import NetworkAdapter
import os

# We try to import libtorrent. If it fails, the adapter will fail gracefully at runtime
# or prompt for installation.
# Fix for Windows: Explicitly add OpenSSL bin to DLL search path
if os.name == 'nt':
    for p in [r"C:\Program Files\OpenSSL-Win64\bin", r"C:\Program Files\OpenSSL\bin"]:
        if os.path.exists(p):
            try:
                os.add_dll_directory(p)
            except Exception: pass

try:
    import libtorrent as lt
except ImportError as e:
    # Check for Termux
    path = os.environ.get("PATH", "")
    is_termux = "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in path
    
    if is_termux:
        # On Termux, python-libtorrent is often installed via pkg in /usr/lib/python3.x/site-packages
        # We search for it dynamically to handle version updates
        import sys
        import glob
        system_sites = glob.glob("/data/data/com.termux/files/usr/lib/python3.*/site-packages")
        
        found = False
        for system_site in system_sites:
            if os.path.exists(system_site) and system_site not in sys.path:
                sys.path.append(system_site)
                try:
                    import libtorrent as lt
                    found = True
                    break
                except ImportError:
                    continue
        if not found:
            lt = None
    else:
        lt = None

    if lt is None:
        msg = f"Warning: Failed to import libtorrent: {e}. "
        if is_termux:
            msg += "On Termux, please install it via: pkg install python-libtorrent"
        else:
            msg += "Torrent features will be disabled. Install via: pip install dlm[torrent]"
        print(msg)

class TorrentClient:
    """
    Singleton Torrent Client to manage a single Session across the application.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TorrentClient, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized: return
        self._initialized = True
        
        if not lt:
            self.session = None
            self.handles = {}
            self.metadata_resolving = {}
            return

        # Configure session with optimal settings for split downloads
        settings = {
            'listen_interfaces': '0.0.0.0:6881',
            'alert_mask': lt.alert.category_t.error_notification | 
                         lt.alert.category_t.status_notification |
                         lt.alert.category_t.storage_notification,
            'connection_speed': 50,  # Connections per second
            'connections_limit': 200,  # Max connections
            'upload_rate_limit': 0,   # No upload limit
            'download_rate_limit': 0, # No download limit
            'request_timeout': 10,    # 10 second timeout
            'piece_timeout': 20,      # 20 second piece timeout
            'peer_connect_timeout': 15,
        }
        self.session = lt.session(settings) if lt else None
        self.handles = {} # info_hash: handle
        self.metadata_resolving = {} # info_hash: threading.Event

    def add_torrent(self, torrent_info_or_magnet: str, save_path: str):
        if not self.session:
            raise RuntimeError("libtorrent not installed. Cannot add torrent.")
        
        params = {
            'save_path': save_path,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
            'flags': lt.add_torrent_params_flags_t.flag_auto_managed
        }
        
        if torrent_info_or_magnet.startswith('magnet:'):
            handle = lt.add_magnet_uri(self.session, torrent_info_or_magnet, params)
        else:
            # Assume it's a path to a torrent file or bencoded data
            info = lt.torrent_info(torrent_info_or_magnet)
            params['ti'] = info
            handle = self.session.add_torrent(params)
            
        info_hash = str(handle.info_hash())
        self.handles[info_hash] = handle
        return handle

    def remove_torrent(self, info_hash: str):
        """Remove a torrent from the session."""
        with self._lock:
            if info_hash in self.handles:
                handle = self.handles[info_hash]
                try:
                    self.session.remove_torrent(handle)
                except Exception: pass
                del self.handles[info_hash]

class PieceScheduler:
    """
    Advanced piece scheduler for out-of-order downloads with availability checking.
    """
    def __init__(self, handle):
        self.handle = handle
        self.torrent_info = handle.get_torrent_info()
        self.piece_length = self.torrent_info.piece_length()
        self.total_pieces = self.torrent_info.num_pieces()
        
    def byte_range_to_pieces(self, start_byte: int, end_byte: int) -> List[int]:
        """Convert byte range to piece indices."""
        start_piece = start_byte // self.piece_length
        end_piece = min(end_byte // self.piece_length, self.total_pieces - 1)
        return list(range(start_piece, end_piece + 1))
    
    def prioritize_range(self, start_byte: int, end_byte: int, 
                        priority: int = 3, deadline_ms: int = 3000):
        """Prioritize a byte range for download with availability checking."""
        pieces = self.byte_range_to_pieces(start_byte, end_byte)
        
        # Check available pieces among peers to avoid requesting unavailable ones
        available_pieces = self.get_available_pieces()
        
        # Set priority and deadlines only for available pieces
        prioritized_count = 0
        for piece_idx in pieces:
            if piece_idx in available_pieces:
                self.handle.piece_priority(piece_idx, priority)
                if deadline_ms > 0:
                    self.handle.set_piece_deadline(piece_idx, deadline_ms)
                prioritized_count += 1
            else:
                # Skip unavailable pieces to avoid choking
                self.handle.piece_priority(piece_idx, 0)  # Skip
        
        return prioritized_count
    
    def get_available_pieces(self) -> set:
        """Get set of all available pieces from connected peers."""
        peer_info = self.handle.get_peer_info()
        available_pieces = set()
        
        for peer in peer_info:
            if hasattr(peer, 'pieces') and peer.pieces:
                # peer.pieces is a bitfield - convert to set of piece indices
                for i, has_piece in enumerate(peer.pieces):
                    if has_piece and i < self.total_pieces:
                        available_pieces.add(i)
                        
        return available_pieces
    
    def get_piece_progress(self) -> Dict[int, bool]:
        """Get download status of all pieces."""
        status = self.handle.status()
        return {i: status.pieces[i] for i in range(self.total_pieces) if i < len(status.pieces)}
    
    def get_peer_statistics(self) -> Dict:
        """Get statistics about peer connections."""
        peer_info = self.handle.get_peer_info()
        stats = {
            'total_peers': len(peer_info),
            'connecting_peers': 0,
            'connected_peers': 0,
            'seeds': 0,
            'downloaders': 0
        }
        
        for peer in peer_info:
            if peer.flags & lt.peer_info.connecting:
                stats['connecting_peers'] += 1
            elif peer.flags & lt.peer_info.handshake:
                stats['connected_peers'] += 1
                
            if peer.flags & lt.peer_info.seed:
                stats['seeds'] += 1
            else:
                stats['downloaders'] += 1
                
        return stats

class TorrentNetworkAdapter(NetworkAdapter):
    """
    Network Adapter for Torrent-based downloads with advanced piece control.
    Compatible with existing split download architecture.
    """
    def __init__(self):
        self.client = TorrentClient()
        self.schedulers = {}  # info_hash: PieceScheduler

    def get_content_length(self, url: str, **kwargs) -> Optional[int]:
        # For torrents, the "length" is resolved during metadata phase
        return None 

    def supports_ranges(self, url: str, **kwargs) -> bool:
        return True # Torrents naturally support piecewise downloads

    def download_range(self, url: str, start: int, end: int, **kwargs) -> Iterator[bytes]:
        """
        Implementation of the standard download_range interface.
        For torrents, this means:
        1. Identify which pieces correspond to this byte range.
        2. Prioritize those pieces with appropriate priority/deadlines.
        3. Wait for them to download.
        4. Yield data from disk/cache.
        """
        if not lt:
            raise RuntimeError("libtorrent not found. Please install it to use Torrent support.")

        # Extract info_hash from URL/metadata
        info_hash = kwargs.get('info_hash')
        if not info_hash:
            raise ValueError("info_hash required for torrent range download")
            
        handle = self.client.handles.get(info_hash)
        if not handle:
            raise ValueError(f"No active torrent for info_hash: {info_hash}")
            
        # Create scheduler if not exists
        if info_hash not in self.schedulers:
            self.schedulers[info_hash] = PieceScheduler(handle)
        
        scheduler = self.schedulers[info_hash]
        
        # Prioritize the requested range with moderate settings
        pieces_prioritized = scheduler.prioritize_range(
            start, end, 
            priority=kwargs.get('priority', 3),  # Moderate priority
            deadline_ms=kwargs.get('deadline_ms', 3000)  # 3 second deadline
        )
        
        if pieces_prioritized == 0:
            raise Exception("No available pieces in requested range")
        
        # Wait for pieces to download
        max_wait = kwargs.get('timeout', 30)
        start_time = time.time()
        piece_indices = scheduler.byte_range_to_pieces(start, end)
        
        while time.time() - start_time < max_wait:
            progress = scheduler.get_piece_progress()
            if all(progress.get(p, False) for p in piece_indices):
                # All pieces downloaded, read from file
                # Note: In practice, you'd read the actual data from the downloaded file
                # This is a simplified placeholder
                yield b'downloaded_data_placeholder'
                return
            time.sleep(0.1)
            
        # Timeout reached
        raise TimeoutError(f"Failed to download pieces {min(piece_indices)}-{max(piece_indices)} within {max_wait}s")

    def download_stream(self, url: str, **kwargs) -> Iterator[bytes]:
        raise NotImplementedError("Sequential streaming not supported yet for Torrents. Use ranges.")