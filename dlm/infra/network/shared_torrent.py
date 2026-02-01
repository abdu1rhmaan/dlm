import threading
import time
from typing import Dict, Set, List, Optional
try:
    import libtorrent as lt
except ImportError:
    lt = None

class SharedTorrentController:
    """
    Manages a single libtorrent session shared among multiple 'Split Tasks'.
    Enforces piece priorities based on which tasks are currently ACTIVE.
    """
    _instance = None
    _lock = threading.RLock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(SharedTorrentController, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized: return
        self._initialized = True
        
        # Internal Libtorrent Session (Re-used)
        settings = {
            'user_agent': 'dlm/0.1.0',
            'listen_interfaces': '0.0.0.0:6881',
            'alert_mask': lt.alert.category_t.error_notification,
            'enable_dht': True,
            'enable_lsd': True,
            'enable_upnp': True,
            'enable_natpmp': True,
        }
        self.session = lt.session(settings)
        
        # State
        self.handles: Dict[str, lt.torrent_handle] = {} # info_hash -> handle
        self.claims: Dict[str, Dict[str, Set[int]]] = {} # info_hash -> { task_id: {pieces} }
        self.meta_waiters: Dict[str, threading.Event] = {} # url -> Event

    def get_handle(self, url: str, save_path: str) -> Optional[lt.torrent_handle]:
        """Get or Add torrent handle. Idempotent."""
        with self._lock:
            # Check if we already have it (by iterating? libtorrent handles duplication gracefully usually)
            # We assume URL is magnet or file path.
            # Ideally we identify by info_hash, but we might not have it yet.
            
            print(f"[SharedController] Getting handle for: {url[:30]}...")
            
            params = {
                'save_path': save_path,
                'storage_mode': lt.storage_mode_t.storage_mode_sparse,
                'flags': lt.add_torrent_params_flags_t.flag_auto_managed | 
                         lt.add_torrent_params_flags_t.flag_update_subscribe
            }
            
            handle = None
            if url.startswith('magnet:'):
                handle = lt.add_magnet_uri(self.session, url, params)
            else:
                info = lt.torrent_info(url)
                params['ti'] = info
                handle = self.session.add_torrent(params)
            
            # Request metadata explicitly
            handle.resume()
                
            # Store/Cache
            ih = str(handle.info_hash())
            self.handles[ih] = handle
            if ih not in self.claims:
                self.claims[ih] = {}
            
            print(f"[SharedController] Handle acquired. InfoHash: {ih}, HasMeta: {handle.status().has_metadata}")
            return handle

    def register_interest(self, handle, task_id: str, pieces: List[int]):
        """A task claims interest in specific pieces."""
        with self._lock:
            ih = str(handle.info_hash())
            if ih not in self.claims: self.claims[ih] = {}
            
            print(f"[SharedController] Register interest - Task: {task_id}, Pieces: {len(pieces)} (First: {pieces[0] if pieces else 'None'})")
            
            # Update claim
            self.claims[ih][task_id] = set(pieces)
            
            # Enforce Priorities
            self._sync_priorities(handle, ih)
            
            # Ensure not paused
            handle.resume()

    def deregister_interest(self, handle, task_id: str):
        """A task removes its interest (e.g. paused/stopped)."""
        with self._lock:
            ih = str(handle.info_hash())
            if ih in self.claims and task_id in self.claims[ih]:
                del self.claims[ih][task_id]
                
            # Enforce Priorities
            self._sync_priorities(handle, ih)
            print(f"[SharedController] Deregistered interest - Task: {task_id}")

    def _sync_priorities(self, handle, info_hash: str):
        """Calculate and apply union of all interests."""
        # 1. Calculate Union of WANTED pieces
        wanted = set()
        if info_hash in self.claims:
            for task_pieces in self.claims[info_hash].values():
                wanted.update(task_pieces)
        
        print(f"[SharedController] Sync Priorities. Total Wanted: {len(wanted)}")
        
        # 2. Apply to Handle
        if not handle.is_valid(): 
            print("[SharedController] Invalid Handle!")
            return
        
        if not handle.status().has_metadata:
            print("[SharedController] No Metadata yet. Skipping priority sync.")
            # Cannot set piece priorities without metadata
            return

        num_pieces = handle.get_torrent_info().num_pieces()
        priorities = [0] * num_pieces
        
        for p in wanted:
            if p < num_pieces:
                priorities[p] = 7
        
        # Verify first few
        print(f"[SharedController] Applied Priorities (First 10): {priorities[:10]}")
        
        handle.prioritize_pieces(priorities)
        
        # Also set deadlines for wanted pieces to encourage speed
        # (Optional, might be aggressive)
        for p in wanted:
             if p < num_pieces:
                 handle.set_piece_deadline(p, 1000) 
 

    def get_piece_status(self, handle, piece_idx: int) -> bool:
        """Check if a piece is complete."""
        try:
            # Use status().pieces bitfield
            # Note: This is an expensive call if polled frequently in loop?
            # Better to use handle.statue() once and share?
            # For now, simple access.
            st = handle.status()
            return st.pieces[piece_idx]
        except:
            return False

    def get_stats(self, handle, pieces: List[int]):
        """Get aggregated stats for a list of pieces."""
        st = handle.status()
        
        total = len(pieces)
        done = 0
        
        # Optimize: get bitfield once
        bitfield = st.pieces
        
        # NEW: Track partial progress for incomplete pieces
        total_bytes = 0
        downloaded_bytes = 0
        
        info = handle.get_torrent_info()
        piece_length = info.piece_length()
        
        for p in pieces:
            piece_size = info.piece_size(p)
            total_bytes += piece_size
            
            if p < len(bitfield) and bitfield[p]:
                done += 1
                downloaded_bytes += piece_size
            else:
                # Check partial progress for this piece
                try:
                    partial = handle.piece_priority(p)
                    # If priority is 0, it's not being downloaded
                    if partial > 0:
                        # Try to get partial piece info (requires polling)
                        # For now, we assume 0 if not complete
                        # libtorrent doesn't expose per-piece byte progress easily
                        pass
                except:
                    pass
        
        # Calculate progress based on bytes, not just piece count
        progress = (downloaded_bytes / total_bytes * 100) if total_bytes > 0 else 0
        
        # DEBUG
        print(f"[SharedController] Stats - Pieces: {done}/{total}, Bytes: {downloaded_bytes}/{total_bytes}, Progress: {progress:.1f}%")
        
        if total > 0 and done == total:
             print(f"[SharedController] DEBUG: All pieces done? Total: {total}, Done: {done}. Pieces: {pieces[:5]}...")
             
        return {
            'total': total,
            'done': done,
            'progress': progress,
            'verified_bytes': downloaded_bytes,
            'total_bytes': total_bytes,
            'seeds': st.num_seeds,
            'peers': st.num_peers,
            'speed': st.download_rate # This is TOTAL speed, not per-task.
        }
