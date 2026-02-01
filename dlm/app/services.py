from pathlib import Path
from typing import Optional, List, Dict
from concurrent.futures import ThreadPoolExecutor, Future
import threading
import time
import json
import re
from datetime import datetime
from collections import deque

from dlm.core.entities import Download, DownloadState, Segment, ResumeState
from dlm.core.repositories import DownloadRepository
from dlm.core.interfaces import NetworkAdapter

import hashlib


def sanitize_folder_name(name: str) -> str:
    """Sanitize a string to be safe as a folder name."""
    # Remove extension for folder name
    if '.' in name:
        name = name.rsplit('.', 1)[0]
    
    # Remove hashtags and other problematic characters
    name = re.sub(r'[#@]', '', name)
    
    # Remove invalid Windows path characters
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    
    # Remove multiple spaces/underscores
    name = re.sub(r'[_\s]+', '_', name)
    
    # Trim leading/trailing underscores and DOTS (Windows folders cannot end with dots)
    name = name.strip('_').strip('.')
    
    # Limit length more aggressively (50 chars for safety)
    name = name[:50] if len(name) > 50 else name

    # Final strip in case truncation left a trailing dot/underscore
    # Final strip in case truncation left a trailing dot/underscore
    return name.strip('_').strip('.')

def sanitize_filename(name: str) -> str:
    """Sanitize a string to be a safe filename, preserving extension."""
    # Split extension if present
    stem = name
    ext = ""
    if '.' in name:
        parts = name.rsplit('.', 1)
        stem = parts[0]
        ext = "." + parts[1]
    
    # Sanitize stem using same logic as folder but no extension stripping
    stem = re.sub(r'[#@]', '', stem)
    stem = re.sub(r'[<>:"/\\|?*]', '_', stem)
    stem = re.sub(r'[_\s]+', '_', stem)
    stem = stem.strip('_').strip('.')
    stem = stem[:50] if len(stem) > 50 else stem
    
    # Reassemble
    return f"{stem}{ext}"

def resolve_target_path(output_template: str, rename_template: str, metadata: dict) -> tuple:
    """
    Resolve (folder, filename) from templates and metadata.
    
    metadata uses keys: 'title', 'index', 'source', 'id'.
    
    1. Resolve Output Path (Folder)
    2. Resolve Base Filename (No extension)
    """
    # Defaults
    base_folder = "."
    base_filename = "download"
    
    clean_meta = {
        'title': sanitize_folder_name(metadata.get('title') or "Untitled"),
        'index': str(metadata.get('index', 1)),
        'source': metadata.get('source', 'unknown'),
        'id': metadata.get('id', 'unknown')
    }
    
    # 1. Output Template -> Folder
    if output_template:
        folder_str = output_template.format(**clean_meta)
        # Sanitize each part? Users might want subfolders "Series/Season1"
        # We assume user input trust for structure, but sanitize components if they come from variables?
        # Ideally, we format first, then strict logic? 
        # But "Lectures/{title}" -> "Lectures/My_Title" is valid.
        target_folder = Path(folder_str)
    else:
        target_folder = Path(base_folder)

    # 2. Rename Template -> Filename
    if rename_template:
        filename_str = rename_template.format(**clean_meta)
        target_stem = sanitize_folder_name(filename_str)
    else:
        # Fallback to Title
        target_stem = clean_meta['title']
        if len(target_stem) > 60:
            # Avoid using '...' which can lead to trailing dots if unsanitized
            target_stem = target_stem[:30] + "_etc_" + target_stem[-20:]

    return target_folder, target_stem


from dlm.core.config import SecureConfigRepository

class DownloadService:
    def __init__(self, repository: DownloadRepository, network: NetworkAdapter, download_dir: Path, max_workers: int = 4, media_service=None, config_repo: SecureConfigRepository = None):
        self.repository = repository
        self.network = network
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.media_service = media_service
        self.config = config_repo # New Dependency

        
        # Track active downloads in memory
        self._active_downloads: Dict[str, Download] = {}
        self._ephemeral_memory: Dict[str, Download] = {} # Live tasks (Share), NO DB persistence
        self._cancel_events: Dict[str, threading.Event] = {}
        self._last_tiktok_profile_download: Dict[str, float] = {} # For rate-limit guard
        self._batch_queue: deque = deque() # Real ordered queue for batch tasks
        self._discovery_tasks: set = set() # track IDs in discovery phase
        self._lock = threading.RLock()
        self.torrent_network = None # Injected by bootstrap
        
        # Vocals / Post-Processing Queue
        self.vocals_queue = [] # List of {"id": str, "path": Path, "gpu": bool, "status": "queued"|"processing"|"done"|"failed", "progress": 0, "error": None}
        self.vocals_lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.vocals_worker_thread = threading.Thread(target=self._vocals_loop, daemon=True)
        self.vocals_worker_thread.start()

    
    def _torrent_worker(self, dl: Download, cancel_event: threading.Event):
        """Worker for Torrent downloads."""
        if not self.torrent_network:
             dl.fail("Torrent support not initialized (adapter missing)")
             return

        try:
            import libtorrent as lt
        except ImportError:
            msg = "libtorrent module not found. "
            # Check for Termux
            path = os.environ.get("PATH", "")
            is_termux = "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in path
            if is_termux:
                msg += "On Termux, please install it via: pkg install python-libtorrent"
            else:
                msg += "Please install it via: pip install dlm[torrent]"
            dl.fail(msg)
            return

        folder = self._get_download_folder(dl)
        folder.mkdir(parents=True, exist_ok=True)
        self._save_metadata(dl)

        try:
             client = self.torrent_network.client
             handle = client.add_torrent(dl.url, str(folder))
             
             # Prioritize selected files
             info = handle.get_torrent_info()
             priorities = [0] * info.num_files()
             for idx in dl.torrent_files:
                 if 0 <= idx < info.num_files():
                     priorities[idx] = 1 # Normal priority
             
             handle.prioritize_files(priorities)
             
             dl.state = DownloadState.DOWNLOADING
             dl.current_stage = "downloading"
             self.repository.save(dl)

             while not cancel_event.is_set():
                 s = handle.status()
                 
                 if s.state == lt.torrent_status.seeding:
                     dl.state = DownloadState.COMPLETED
                     dl._manual_progress = 100.0
                     break
                 
                 if s.state == lt.torrent_status.downloading:
                      dl._manual_progress = s.progress * 100.0
                      dl.speed_bps = s.download_rate
                      dl.current_stage = f"downloading ({s.num_peers} peers)"
                 
                 if s.has_metadata:
                      # Update total size once metadata is available
                      if getattr(dl, 'torrent_files', None):
                          # Sum size of selected files only
                          info = s.torrent_file
                          files = info.files()
                          total = 0
                          for idx in dl.torrent_files:
                              if 0 <= idx < files.num_files():
                                  total += files.file_size(idx)
                          dl.total_size = total
                      else:
                          dl.total_size = s.torrent_file.total_size()
                      
                      # Create segment if needed for progress tracking
                      if not dl.segments and dl.total_size > 0:
                          dl.segments = [Segment(0, dl.total_size - 1)]
                 
                 # Update downloaded bytes based on torrent progress
                 if dl.segments and dl.total_size > 0:
                     downloaded = int(s.progress * dl.total_size)
                     dl.segments[0].downloaded_bytes = downloaded

                 # Periodic save
                 self.repository.save(dl)
                 
                 if s.is_finished:
                      dl.state = DownloadState.COMPLETED
                      dl._manual_progress = 100.0
                      self.repository.save(dl)
                      self._cleanup_on_completion(dl)
                      break
                 
                 time.sleep(1)

             if cancel_event.is_set():
                 if getattr(dl, "deleted", False):
                     # Task was deleted, remove from session to release locks
                     try:
                         client.remove_torrent(str(handle.info_hash()))
                     except: pass
                     dl.state = DownloadState.CANCELLED
                 else:
                     handle.pause()
                     dl.state = DownloadState.PAUSED
                 self.repository.save(dl)

        except Exception as e:
             dl.fail(f"Torrent Error: {e}")
             self.repository.save(dl)
        finally:
             self._on_task_terminated(dl)
    
    @property
    def concurrency_limit(self) -> int:
        if self.config:
            # Default to 1 (Sequential) as requested by user
            return int(self.config.get("concurrency_limit", 1))
        return 1

    def get_download_by_capture_id(self, capture_id: int) -> Optional[Download]:
        """Find a download by its browser capture ID."""
        with self._lock:
            # Check memory first
            for dl in self._active_downloads.values():
                if dl.browser_capture_id == capture_id:
                    return dl
            # Check repository
            existing_dls = self.repository.get_all()
            return next((d for d in existing_dls if d.browser_capture_id == capture_id), None)

    def _get_active_count(self) -> int:
        with self._lock:
            # Only count tasks that are truly active (DOWNLOADING or INITIALIZING)
            # This ensures PAUSED tasks don't block the queue.
            active_tasks = [d for d in self._active_downloads.values() 
                            if d.state in [DownloadState.DOWNLOADING, DownloadState.INITIALIZING]]
            return len(active_tasks) + len(self._discovery_tasks)

    def _get_workspace_depth(self, folder_id: Optional[int]) -> Optional[int]:
        """Returns depth relative to workspace root:
        0: __workspace__
        1: task folder
        2: segments/ or exported/
        None: not in workspace
        """
        if folder_id is None: return None
        depth = 0
        curr_id = folder_id
        while curr_id is not None:
            folder = self.repository.get_folder(curr_id)
            if not folder: break
            if folder['name'] == '__workspace__':
                return depth
            curr_id = folder['parent_id']
            depth += 1
        return None

    def _process_queue(self):
        """
        [ENGINE] المحرك المسؤول عن إدارة طابور المهام.
        """
        with self._lock:
            while self._get_active_count() < self.concurrency_limit:
                download_id = None
                
                # 1. Try Batch Queue first
                if self._batch_queue:
                    download_id = self._batch_queue.popleft()
                
                # 2. If Batch Queue empty, check for WAITING tasks in DB
                if not download_id:
                    waiting_tasks = [d.id for d in self.repository.get_all() if d.state == DownloadState.WAITING]
                    if waiting_tasks:
                        download_id = waiting_tasks[0]
                
                if not download_id:
                    break
                
                if download_id in self._active_downloads:
                    continue
                
                try:
                    self.start_download(download_id, manual_trigger=False)
                except Exception as e:
                    print(f"[ENGINE] Error starting task {download_id}: {e}")

    def _on_task_terminated(self, dl: Download):
        """
        [ENGINE] المركز الموحد لإنهاء أي مهمة (نجاح أو فشل).
        يضمن تحرير المكان في الذاكرة وتشغيل المهمة التالية.
        """
        with self._lock:
            # 1. تنظيف التتبع (Active & Discovery)
            self._active_downloads.pop(dl.id, None)
            self._cancel_events.pop(dl.id, None)
            self._discovery_tasks.discard(dl.id)
            
            # 2. الحفظ النهائي
            try:
                if not getattr(dl, 'ephemeral', False):
                    self.repository.save(dl)
                    self._save_metadata(dl)
            except:
                pass
            
            # 3. استدعاء المحرك فوراً لتشغيل المهمة التالية
            # print(f"[ENGINE] Task {dl.id} terminated. Triggering queue.")
            self._process_queue()

    def start_folder(self, folder_id: Optional[int], recursive: bool = False, brw: bool = False):
        """Start all tasks in a folder, optionally recursively."""
        if brw:
            items = self.repository.get_browser_downloads_by_folder(folder_id)
            for item in items:
                self.start_download(str(item['id']), brw=True)
            
            if recursive:
                subfolders = self.repository.get_folders(folder_id)
                for sub in subfolders:
                    self.start_folder(sub['id'], recursive=True, brw=True)
        else:
            downloads = self.repository.get_all_by_folder(folder_id)
            for d in downloads:
                self.start_download(d.id, brw=False)
            
            if recursive:
                subfolders = self.repository.get_folders(folder_id)
                for sub in subfolders:
                    self.start_folder(sub['id'], recursive=True, brw=False)

    def delete_folder_recursively(self, folder_id: int):
        """Delete a folder, its subfolders, and all tasks within them."""
        folder = self.repository.get_folder(folder_id)
        if not folder: return

        # CRITICAL: If this folder is inside __workspace__, delete the physical task folder
        if folder['parent_id'] is not None:
            parent = self.repository.get_folder(folder['parent_id'])
            if parent and parent['name'] == '__workspace__':
                # It's a task workspace folder.
                from dlm.core.workspace import WorkspaceManager
                wm = WorkspaceManager(self.download_dir.parent)
                task_folder = wm.workspace_root / folder['name']
                if task_folder.exists():
                    import shutil
                    shutil.rmtree(task_folder)
                    print(f"[Workspace Clean] Deleted physical folder: {task_folder}")

        # Delete tasks
        downloads = self.repository.get_all_by_folder(folder_id)
        for d in downloads:
            self.remove_download(d.id)
        
        # Delete subfolders
        subfolders = self.repository.get_folders(folder_id)
        for sub in subfolders:
            self.delete_folder_recursively(sub['id'])
        
        # Delete the folder itself
        self.repository.delete_folder(folder_id)
    def _get_download_folder(self, dl: Download) -> Path:
        """Get the folder path for a active download segments/discovery."""
        # For torrets, we download directly to the target folder
        if dl.source == 'torrent' and dl.output_path:
            return Path(dl.output_path)

        # For everything else, use a hidden workspace near the DB (project root)
        from dlm.core.workspace import WorkspaceManager
        # Use DB path as stable anchor for project root
        root_path = Path(self.repository.db_path).parent if hasattr(self.repository, 'db_path') else self.download_dir.parent
        wm = WorkspaceManager(root_path)
        
        # v2 Collaborative tasks
        if dl.task_id:
            ws_folder = wm.get_task_folder_by_id(dl.task_id)
            if ws_folder: return ws_folder

        # Standard tasks workspace naming: MUST BE STABLE
        # We use 'dl_' + shortened ID. We avoid using 'stem' or 'size' here
        # because they can change during the download lifecycle (title discovery/extension change).
        ws_name = f"dld_{dl.id[:12]}"
        
        wm.ensure_workspace_root()
        task_dir = wm.workspace_root / ws_name
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

    def _get_metadata_path(self, dl: Download) -> Path:
        """Get the metadata file path for a download."""
        return self._get_download_folder(dl) / "dlm.meta"

    def _delete_metadata(self, dl: Download):
        """Delete dlm.meta file."""
        try:
            meta_path = self._get_metadata_path(dl)
            if meta_path.exists():
                meta_path.unlink()
        except:
            pass

    def _cleanup_on_completion(self, dl: Download):
        """
        Post-completion cleanup: Move files to final destination and remove workspace.
        """
        try:
            import shutil
            folder = self._get_download_folder(dl)
            
            # Determine Target Directory
            if dl.output_path:
                target_dir = Path(dl.output_path)
            else:
                # [NEW] Check for global default output in config
                cfg_output = self.config.get("default_output_dir") if self.config else None
                if cfg_output:
                    target_dir = Path(cfg_output)
                else:
                    target_dir = self.download_dir
            
            target_dir.mkdir(parents=True, exist_ok=True)

            # 1. Move Files
            if dl.partial:
                # Handle Split/Partial Downloads
                parts = list(folder.glob(f"{dl.target_filename}.part.*"))
                for p_file in parts:
                    target_p = target_dir / p_file.name
                    if p_file != target_p:
                        final_p = target_p
                        if final_p.exists():
                            stem, ext = final_p.stem, final_p.suffix
                            c = 1
                            while final_p.exists():
                                final_p = target_dir / f"{stem}_{c}{ext}"
                                c += 1
                        shutil.move(str(p_file), str(final_p))
            else:
                # Handle Normal Downloads
                # For torrents, files are in a subfolder (torrent name)
                # For HTTP, files are directly in workspace
                
                # Check if this is a torrent download
                if dl.source == 'torrent':
                    # Torrent files are in a subfolder - extract contents directly to downloads
                    if folder.exists():
                        subdirs = [d for d in folder.iterdir() if d.is_dir() and d.name != '__pycache__']
                        if subdirs:
                            # Move all contents from torrent subfolder directly to downloads
                            torrent_folder = subdirs[0]
                            for item in torrent_folder.iterdir():
                                target_item = target_dir / item.name
                                
                                # Handle duplicates
                                if target_item.exists():
                                    if item.is_file():
                                        stem, suffix = target_item.stem, target_item.suffix
                                        counter = 1
                                        while target_item.exists():
                                            target_item = target_dir / f"{stem}_{counter}{suffix}"
                                            counter += 1
                                    else:
                                        counter = 1
                                        base_name = item.name
                                        while target_item.exists():
                                            target_item = target_dir / f"{base_name}_{counter}"
                                            counter += 1
                                
                                shutil.move(str(item), str(target_item))
                else:
                    # HTTP/YouTube downloads - file is directly in workspace
                    filename = dl.target_filename
                    source_file = folder / filename if filename else None
                    
                    if source_file and source_file.exists():
                        target_file = target_dir / filename
                        
                        # Handle Duplicates
                        if target_file.exists() and target_file.resolve() != source_file.resolve():
                            stem, suffix = target_file.stem, target_file.suffix
                            counter = 1
                            while target_file.exists():
                                target_file = target_dir / f"{stem}_{counter}{suffix}"
                                counter += 1
                        
                        if source_file.resolve() != target_file.resolve():
                            shutil.move(str(source_file), str(target_file))
                        
                        # [NEW] Vocals Support: Trigger separation on the FINAL file path
                        if dl.audio_mode == 'vocals':
                            try:
                                self.queue_vocals(target_file, use_gpu=dl.vocals_gpu, keep_all=dl.vocals_keep_all)
                                print(f"[VOCALS] Added '{target_file.name}' to background queue.")
                            except Exception as v_err:
                                print(f"[VOCALS] Queue Error: {v_err}")
                    else:
                        # If not in workspace, maybe already moved? 
                        # yt-dlp sometimes does its own thing. 
                        pass

            # 2. Delete Metadata and Workspace
            self._delete_metadata(dl)
            
            if folder.exists() and folder.resolve() != self.download_dir.resolve() and folder.resolve() != target_dir.resolve():
                 # Retry loop for windows file locking
                 for _ in range(5):
                     try:
                         shutil.rmtree(folder)
                         break
                     except:
                         time.sleep(0.5)

            # 3. Finalize State & Queue
            self._on_task_terminated(dl)
            
            # 4. Auto-delete share downloads (ephemeral)
            if dl.source == 'share' or (dl.source == 'upload' and dl.url == 'external://transfer'):
                import threading
                def cleanup_share():
                    import time
                    time.sleep(1)  # Wait for TUI to show completion
                    self.repository.delete(dl.id)
                threading.Thread(target=cleanup_share, daemon=True).start()
                            
        except Exception as e:
             print(f"[CLEANUP] Error: {e}")
             self._on_task_terminated(dl) # Ensure queue proceeds even on cleanup error


    def _save_metadata(self, dl: Download):
        """Save download metadata to file."""
        # Fix Ghost Folders: Do not save (and thus recreate folder) if task is done.
        if dl.state in [DownloadState.COMPLETED, DownloadState.FAILED]:
            return

        folder = self._get_download_folder(dl)
        
        # Don't create folder just for metadata if it doesn't exist? 
        # Actually initializing tasks need it.
        folder.mkdir(parents=True, exist_ok=True)
        
        meta = {
            "id": dl.id,
            "url": dl.url,
            "filename": dl.target_filename,
            "total_size": dl.total_size,
            "created_at": dl.created_at.isoformat(),
            "resumable": dl.resumable,
            "resume_state": dl.resume_state.value,
            "source": dl.source,
            "media_type": dl.media_type,
            "progress_mode": dl.progress_mode,
            "current_stage": dl.current_stage,
            "quality": dl.quality,
            "quality": dl.quality,
            "cut_range": dl.cut_range,
            "duration": dl.duration,
            "referer": dl.referer,
            "probed_via_stream": getattr(dl, 'probed_via_stream', False),
            "segments": [
                {
                    "start": s.start_byte, 
                    "end": s.end_byte, 
                    "downloaded": s.downloaded_bytes,
                    "checkpoint": s.last_checkpoint,
                    "start_hash": s.start_hash,
                    "end_hash": s.end_hash
                }
                for s in dl.segments
            ]
        }
        
        meta_path = self._get_metadata_path(dl)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)

    def _load_metadata(self, folder_path: Path) -> Optional[dict]:
        """Load metadata from a folder."""
        meta_path = folder_path / "dlm.meta"
        if not meta_path.exists():
            return None
        
        try:
            with open(meta_path, 'r') as f:
                return json.load(f)
        except:
            return None

    def _initialize_segments(self, dl: Download):
        """Initialize download segments based on total size."""
        if not dl.total_size or dl.total_size <= 0:
            dl.total_size = 0
            dl.resumable = False
            dl.max_connections = 1
            dl.segments = []
            return

        num_segments = 1
        if dl.resumable:
            if dl.total_size < 20 * 1024 * 1024:
                num_segments = 1
            elif dl.total_size < 100 * 1024 * 1024:
                num_segments = 2
            elif dl.total_size < 1 * 1024 * 1024 * 1024:
                num_segments = 4
            else:
                num_segments = 8
        
        dl.max_connections = num_segments
        
        if num_segments > 1:
            chunk_size = dl.total_size // num_segments
            dl.segments = []
            for i in range(num_segments):
                start = i * chunk_size
                end = (start + chunk_size - 1) if i < num_segments - 1 else dl.total_size - 1
                dl.segments.append(Segment(start, end))
        else:
            dl.segments = [Segment(0, dl.total_size - 1)]

    def add_download(self, url: str, source: str=None, media_type: str=None, quality: str=None, 
                     cut_range: str=None, conversion_required: bool=False, title: str=None, duration: float=None,
                     audio_mode: str=None, vocals_gpu: bool=False, vocals_keep_all: bool=False, referer: str=None, storage_state: str=None, 
                     torrent_files: list=None, torrent_file_offset: int=0, total_size: int=0, folder_id: int=None, 
                     output_template: str = None, rename_template: str = None, ephemeral: bool = False, **kwargs) -> str:
        """Add a download to the queue without starting it."""
        
        # CRITICAL: Prevent task creation inside workspace
        if folder_id is not None:
            folder = self.repository.get_folder(folder_id)
            if folder and folder['name'] == '__workspace__':
                raise ValueError(
                    "Cannot create tasks inside __workspace__. "
                    "This is an internal area. Navigate to a user folder first."
                )
        
        dl = Download(url=url)
        dl.source = source
        dl.media_type = media_type
        dl.quality = quality
        dl.cut_range = cut_range
        dl.conversion_required = conversion_required
        dl.duration = duration
        dl.audio_mode = audio_mode
        dl.vocals_gpu = vocals_gpu
        dl.vocals_keep_all = vocals_keep_all
        dl.referer = referer
        dl.storage_state = storage_state
        dl.torrent_files = torrent_files or []
        dl.torrent_file_offset = torrent_file_offset
        dl.folder_id = folder_id

        # Step 0: Torrent branch
        if source == 'torrent':
            torrent_name = sanitize_folder_name(title or "torrent_download")
            dl.target_filename = sanitize_filename(title or "torrent_download")
            
            # Create subfolder for torrent to keep downloads organized
            target_folder = self.download_dir / torrent_name
            target_folder.mkdir(parents=True, exist_ok=True)
            dl.output_path = str(target_folder)
            
            # For torrents, we use provided size if available (e.g. from known metadata)
            if total_size > 0:
                dl.total_size = total_size
                
            dl.state = DownloadState.QUEUED
            self.repository.save(dl)
            return dl.id

        if source in ['youtube', 'spotify']:
            res_meta = {'title': title, 'index': kwargs.get('index', 1), 'source': source, 'id': dl.id}
            target_folder, target_stem = resolve_target_path(output_template or kwargs.get('output_template'), rename_template or kwargs.get('rename_template'), res_meta)
            ext = ".mp3" if media_type == 'audio' else ".mp4"
            dl.target_filename = f"{target_stem}{ext}"
            final_folder = target_folder if target_folder.is_absolute() else self.download_dir / target_folder
            final_folder.mkdir(parents=True, exist_ok=True)
            dl.output_path = str(final_folder) if (output_template or kwargs.get('output_template')) else None
            dl.state = DownloadState.QUEUED
            self.repository.save(dl)
            return dl.id
        elif source == 'tiktok':
            profile_match = re.search(r'tiktok\.com/(@[a-zA-Z0-9._]+)', url)
            username = profile_match.group(1) if profile_match else ""
            vid_id = re.search(r'/video/(\d+)', url).group(1) if re.search(r'/video/(\d+)', url) else dl.id[:8]
            tiktok_stem = f"{username}_{vid_id}" if username else vid_id
            res_meta = {'title': title or tiktok_stem, 'index': kwargs.get('index', 1), 'source': source, 'id': vid_id}
            target_folder, target_stem = resolve_target_path(output_template or kwargs.get('output_template'), rename_template or kwargs.get('rename_template'), res_meta)
            if not kwargs.get('rename_template'): target_stem = sanitize_folder_name(tiktok_stem)
            ext = ".mp3" if media_type == 'audio' else ".mp4"
            dl.target_filename = f"{target_stem}{ext}"
            final_folder = target_folder if target_folder.is_absolute() else self.download_dir / target_folder
            final_folder.mkdir(parents=True, exist_ok=True)
            dl.output_path = str(final_folder) if (output_template or kwargs.get('output_template')) else None
            dl.state = DownloadState.QUEUED
            self.repository.save(dl)
            return dl.id

            # Step 2: Standard HTTP
        try:
            if total_size > 0:
                 dl.total_size = total_size
                 dl.resumable = True # Assume resumable if we have explicit size (implied safe source)
            else:
                 try:
                     dl.total_size = self.network.get_content_length(url, referer=dl.referer)
                 except: 
                     dl.total_size = 0
            
            # Resolve Target Path/Filename similar to other sources
            res_meta = {'title': title, 'index': kwargs.get('index', 1), 'source': source or 'http', 'id': dl.id}
            target_folder, target_stem = resolve_target_path(output_template or kwargs.get('output_template'), rename_template or kwargs.get('rename_template'), res_meta)
            
            # Determine filename
            if title:
                dl.target_filename = sanitize_filename(title)
            elif kwargs.get('rename_template'):
                # If rename template used, append extension from URL if possible
                ext = ""
                if '.' in url.split("/")[-1]:
                    ext = "." + url.split("/")[-1].rsplit('.', 1)[1]
                dl.target_filename = f"{target_stem}{ext}"
            else:
                 dl.target_filename = url.split("/")[-1] or f"download_{dl.id}"

            # Set output path
            final_folder = target_folder if target_folder.is_absolute() else self.download_dir / target_folder
            final_folder.mkdir(parents=True, exist_ok=True)
            dl.output_path = str(final_folder) if (output_template or kwargs.get('output_template')) else None

            if not dl.resumable:
                dl.resumable = self.network.supports_ranges(url, referer=dl.referer)
            self._initialize_segments(dl)
            dl.state = DownloadState.QUEUED
            if ephemeral:
                dl.ephemeral = True
                self._ephemeral_memory[dl.id] = dl
            else:
                self.repository.save(dl)
                
            self._start_workers(dl)
            return dl.id
        except Exception:
            dl.total_size = 0
            dl.resumable = False
            dl.state = DownloadState.QUEUED
            if ephemeral:
                dl.ephemeral = True
                self._ephemeral_memory[dl.id] = dl
            else:
                self.repository.save(dl)
            return dl.id

    def promote_browser_capture(self, capture_id: int, folder_id: int = None) -> str:
        """Move a browser capture to the main download list without starting it."""
        capture = self.repository.get_browser_download(capture_id)
        if not capture:
            raise ValueError(f"Capture ID {capture_id} not found")
        
        existing_dls = self.repository.get_all()
        dl = next((d for d in existing_dls if d.browser_capture_id == capture['id']), None)
        
        if not dl:
            from dlm.core.entities import Download
            dl = Download(url=capture['url'])
            dl.target_filename = capture['filename']
            dl.source = "browser"
            dl.referer = capture.get('referrer')
            dl.storage_state = capture['storage_state']
            dl.browser_capture_id = capture['id']
            dl.total_size = capture.get('size', 0)
            dl.user_agent = capture.get('user_agent')
            dl.folder_id = folder_id
            
            # Protocol-Level Session Data (Indistinguishability Fix)
            dl.source_url = capture.get('source_url')
            if 'captured_headers_json' in capture:
                dl.captured_headers = json.loads(capture['captured_headers_json'])
            if 'captured_cookies_json' in capture:
                dl.captured_cookies = json.loads(capture['captured_cookies_json'])
            
            if dl.total_size > 0 and dl.source not in ['youtube', 'tiktok', 'spotify', 'torrent']:
                dl.resumable = True
                self._initialize_segments(dl)
            else:
                # Trigger background discovery if size is still 0
                self.executor.submit(self.resolve_browser_download_size, capture['id'])
            
            dl.state = DownloadState.QUEUED
            self.repository.save(dl)
            return dl.id
        else:
            # Already promoted, just ensure fields are synced if discovery finished recently
            if (not dl.total_size or dl.total_size == 0):
                if capture.get('size') and dl.source not in ['youtube', 'tiktok', 'spotify', 'torrent']:
                    dl.total_size = capture['size']
                    dl.resumable = True
                    self._initialize_segments(dl)
                    self.repository.save(dl)
                else:
                    # Still unknown, trigger again just in case
                    self.executor.submit(self.resolve_browser_download_size, capture['id'])
            return dl.id

    def start_download(self, download_id: str, manual_trigger: bool = True, brw: bool = False):
        """Start a queued download."""
        if brw:
            capture_id = int(download_id)
            self.promote_browser_capture(capture_id)
            existing_dls = self.repository.get_all()
            dl = next((d for d in existing_dls if d.browser_capture_id == capture_id), None)
            if not dl: raise ValueError("Failed to promote browser task")
            download_id = dl.id
        
        dl = self.get_download(download_id)
        if not dl: raise ValueError("Download not found")
        
        # [NEW] Add to batch queue if this is a fresh manual start request
        if manual_trigger:
            with self._lock:
                # Allow re-queueing if it's not already in batch and not currently DOWNLOADING
                is_active = False
                if download_id in self._active_downloads:
                    is_active = self._active_downloads[download_id].state == DownloadState.DOWNLOADING
                
                if download_id not in self._batch_queue and not is_active:
                    self._batch_queue.append(download_id)
        
        if dl.state in [DownloadState.COMPLETED, DownloadState.FAILED]: return
        if dl.state == DownloadState.DOWNLOADING: return

        with self._lock:
            # 1. Skip if already in discovery
            if download_id in self._discovery_tasks: return
            
            # 1.5 Concurrency Check
            # If we are already at capacity, let it stay in WAITING/QUEUED/DEQUE
            if self._get_active_count() >= self.concurrency_limit:
                if dl.state != DownloadState.WAITING:
        dl.state = DownloadState.WAITING
                    self.repository.save(dl)
                return

        # 2. Resource Discovery if size unknown
        if dl.total_size == 0 and dl.source in [None, 'browser']:
            with self._lock:
                self._discovery_tasks.add(download_id)
                dl.state = DownloadState.INITIALIZING
                self.repository.save(dl)
            
            def do_discovery():
                try:
                    # STRICT 10s timeout for discovery
                    new_size = self.network.get_content_length(dl.url, referer=dl.referer, headers=dl.captured_headers, cookies=dl.captured_cookies, user_agent=dl.user_agent, timeout=10)
                    if not new_size or new_size == 0:
                        # Try stream probe if HEAD didn't work and we haven't probed yet
                        if not getattr(dl, 'probed_via_stream', False):
                            dl.probed_via_stream = True
                            new_size = self.network.get_content_length(dl.url, referer=dl.referer, headers=dl.captured_headers, cookies=dl.captured_cookies, user_agent=dl.user_agent, timeout=10)

                    if new_size and new_size > 0:
                        dl.total_size = new_size
                        dl.resumable = self.network.supports_ranges(dl.url, referer=dl.referer, headers=dl.captured_headers, cookies=dl.captured_cookies, user_agent=dl.user_agent)
                        self._initialize_segments(dl)
                        dl.state = DownloadState.QUEUED
                        self.repository.save(dl)
                        
                        with self._lock: self._discovery_tasks.discard(download_id)
                        # Re-inject into batch queue to ensure it follows concurrency rules
                        if download_id not in self._batch_queue:
                            self._batch_queue.append(download_id)
                        self._process_queue()
                    else:
                        with self._lock: self._discovery_tasks.discard(download_id)
                        self._start_workers(dl)
                except Exception as e:
                    print(f"[DISCOVERY] Error for {download_id}: {e}")
                    with self._lock: self._discovery_tasks.discard(download_id)
                    self._start_workers(dl)
            
            self.executor.submit(do_discovery)
            return

        # [ARCH-FIX] Ensure segments are initialized for known-size downloads (e.g. Share)
        # This prevents them from falling back to _stream_worker which might have progress tracking issues for known sizes.
        if dl.total_size > 0 and not dl.segments and dl.source not in ['youtube', 'tiktok', 'spotify', 'torrent']:
             self._initialize_segments(dl)
             self.repository.save(dl)

        # 3. Final Checks ...
        if dl.source == "tiktok":
            # [Tiktok Rate Logic]
            profile_match = re.search(r'tiktok\.com/(@[a-zA-Z0-9._]+)', dl.url)
            if profile_match:
                handle = profile_match.group(1)
                with self._lock:
                    last = self._last_tiktok_profile_download.get(handle, 0)
                    now = time.time()
                    if now - last < 1.5: time.sleep(1.5 - (now - last))
                    self._last_tiktok_profile_download[handle] = time.time()

        if (dl.total_size or 0) > 0:
            import shutil
            _, _, free = shutil.disk_usage(self.download_dir)
            required = dl.total_size + (50 * 1024 * 1024)
            if required > free:
                def format_size(v):
                    if v >= 1024**3: return f"{v/1024**3:.1f}GB"
                    if v >= 1024**2: return f"{v/1024**2:.1f}MB"
                    if v >= 1024: return f"{v/1024:.0f}KB"
                    return f"{v}B"
                
                dl.fail(f"Insufficient disk space. Required: {format_size(required)}, Available: {format_size(free)}")
                self.repository.save(dl)
                self._process_queue()
                return

        self._save_metadata(dl)
        self._start_workers(dl)

    def resolve_browser_download_size(self, capture_id: int):
        """Silently resolve file size for a browser download."""
        try:
            # Check if linked task already has size or probe done
            dl = self.get_download_by_capture_id(capture_id)
            if dl and (dl.total_size > 0 or dl.browser_probe_done):
                return

            capture = self.repository.get_browser_download(capture_id)
            if not capture or (capture.get('size') and capture.get('size') > 0): return
            
            # Determine session data
            headers = json.loads(capture.get('captured_headers_json', '[]'))
            cookies = json.loads(capture.get('captured_cookies_json', '{}'))
            
            # Use a shorter timeout for proactive background resolution
            new_size = self.network.get_content_length(
                capture['url'], 
                referer=capture.get('referrer'), 
                headers=headers, 
                cookies=cookies, 
                user_agent=capture.get('user_agent'),
                timeout=5 # Faster timeout for background resolution
            )
            
            if new_size and new_size > 0:
                self.repository.update_browser_download_size(capture_id, new_size)
                # Sync existing task in DOWNLOADS table
                all_dls = self.repository.get_all()
                dl = next((d for d in all_dls if d.browser_capture_id == capture_id), None)
                if dl and (not dl.total_size or dl.total_size == 0):
                    dl.total_size = new_size
                    dl.resumable = True # Assume for now
                    self._initialize_segments(dl)
                    self.repository.save(dl)
        except Exception: pass

    def retry_download(self, download_id: str):
        """Reset a failed download and requeue it."""
        dl = self.get_download(download_id)
        if not dl:
            raise ValueError("Download not found")
        
        with self._lock:
            # Allow retrying failed, cancelled, or completed tasks
            if dl.state not in [DownloadState.FAILED, DownloadState.CANCELLED, DownloadState.COMPLETED]:
                return

            dl.state = DownloadState.QUEUED
            dl.total_size = 0  # Re-discover size on retry to fix 100% glitch
            dl.reset_progress()
            
            self.repository.save(dl)
            self._save_metadata(dl)

            # Re-inject into batch queue if not there
            if download_id not in self._batch_queue:
                self._batch_queue.append(download_id)

    def resume_download(self, download_id: str):
        """Resume a paused or failed download."""
        dl = self.get_download(download_id)
        if not dl:
            raise ValueError("Download not found")
        
        if dl.state == DownloadState.DOWNLOADING:
            return

        if download_id not in self._batch_queue:
            self._batch_queue.append(download_id)
        
        self._process_queue()

    def pause_download(self, download_id: str):
        """Pause a running download."""
        # 1. Signal cancellation FIRST (no lock needed for set())
        should_cleanup = False
        with self._lock:
            if download_id in self._cancel_events:
                self._cancel_events[download_id].set()
                should_cleanup = True
        
        # 2. Reset state logic
        # We don't join/wait here to avoid blocking REPL.
        # The monitor thread picks up the signal and transitions to PAUSED/Cleanup.
        
        # However, to be responsive, we might want to update DB state to 'PAUSED' 
        # BUT only if it was effectively stopped.
        # Current logic sets PAUSED immediately.
        # Let's ensure we don't hold lock while IO happens effectively.
        
        with self._lock:
            dl = self.repository.get(download_id) # Reload from DB/Memory
            if getattr(dl, 'id', None) == download_id: 
                 # Safety: Do NOT pause a completed/failed task
                 if dl.state in [DownloadState.COMPLETED, DownloadState.FAILED]:
                     return

                 # If in memory active list
                 if download_id in self._active_downloads:
                     self._active_downloads[download_id].state = DownloadState.PAUSED
                 
                 dl.state = DownloadState.PAUSED
                 self.repository.save(dl)
                 # Metadata save might block slightly, but acceptable compared to deadlocks
                 self._save_metadata(dl)

    def _async_cleanup(self, folder: Path, retries: int = 10):
        """Helper to delete folder with retries (background thread)."""
        import shutil
        for i in range(retries):
            try:
                if folder.exists():
                    shutil.rmtree(folder)
                return
            except Exception:
                time.sleep(1.0) # Wait for file handles to release

    def remove_download(self, download_id: str, delete_file: bool = False):
        """Remove a download and optionally delete files."""
        download_to_clean_manually = None
        
        with self._lock:
            # If active, signal it to stop. Monitor will handle cleanup.
            if download_id in self._active_downloads:
                dl = self._active_downloads[download_id]
                dl.deleted = True # Signal monitor
                if download_id in self._cancel_events:
                    self._cancel_events[download_id].set()
                
                # We remove from active list immediately so UI updates
                self._active_downloads.pop(download_id, None)
                self._cancel_events.pop(download_id, None)
            else:
                # Inactive, we must handle cleanup
                if delete_file:
                    dl = self.repository.get(download_id)
                    if dl:
                        download_to_clean_manually = dl
        
        # Remove from DB immediately
        self.repository.delete(download_id)
        
        # User Request: "remove command deletes db only"
        # Disabled file cleanup entirely.
        # if download_to_clean_manually:
        #     folder = self._get_download_folder(download_to_clean_manually)
        #     threading.Thread(target=self._async_cleanup, args=(folder,), daemon=True).start()

    def remove_browser_download(self, id: int):
        """Remove a captured browser download from the database."""
        self.repository.delete_browser_download(id)

    def resume_from_folder(self, url: str, folder_path: Path) -> str:
        # Load metadata
        meta = self._load_metadata(folder_path)
        if not meta:
            raise ValueError(f"No metadata found in {folder_path}")
        
        # Validate with HEAD request
        new_size = self.network.get_content_length(url)
        if new_size != meta.get("total_size"):
            raise ValueError(f"Size mismatch: expected {meta.get('total_size')}, got {new_size}")
        
        # Create download from metadata
        dl = Download(url=url)
        dl.id = meta["id"]
        dl.target_filename = meta["filename"]
        dl.total_size = meta["total_size"]
        dl.created_at = datetime.fromisoformat(meta["created_at"])
        dl.resumable = meta.get("resumable", True)
        dl.resume_state = ResumeState(meta.get("resume_state", "STABLE"))
        dl.referer = meta.get("referer")
        dl.segments = [
            Segment(
                s["start"], 
                s["end"], 
                s["downloaded"],
                s.get("checkpoint", 0),
                s.get("start_hash"),
                s.get("end_hash")
            )
            for s in meta.get("segments", [])
        ]
        
        # Safety Core: Validate & Rollback
        self._validate_and_rollback(dl)
        
        dl.state = DownloadState.QUEUED
        self.repository.save(dl)
        
        return dl.id

    def _validate_and_rollback(self, dl: Download):
        """Resume Safety Core: Validate state and rollback corrupted segments."""
        folder = self._get_download_folder(dl)
        
        # Determine the physical file being checked
        if dl.task_id:
            part_file = folder / "data.part"
            use_shared_structure = True
        elif not dl.partial:
            part_file = folder / f"{dl.target_filename}.part"
            use_shared_structure = True
        else:
            # LEGACY: Old separate part files.
            # We still need to handle these for existing tasks, 
            # but new tasks will use task_id + data.part.
            use_shared_structure = False
            part_file = None 

        if use_shared_structure and part_file:
            if part_file.exists():
                file_size = part_file.stat().st_size
                
                # Check for unstable state
                if dl.total_size and file_size != dl.total_size and not dl.task_id:
                    # For non-workspace tasks, mismatch means unstable.
                    # For workspace tasks, data.part might be smaller if we are importing.
                    dl.resume_state = ResumeState.UNSTABLE
                
                for seg in dl.segments:
                    # Range safety
                    if seg.downloaded_bytes > seg.last_checkpoint:
                        seg.downloaded_bytes = seg.last_checkpoint
                        dl.resume_state = ResumeState.UNSTABLE
            else:
                # File missing - reset
                for seg in dl.segments:
                    seg.downloaded_bytes = 0
                    seg.last_checkpoint = 0
        
        elif dl.partial and not dl.task_id:
            # Legacy Separate Part Files Rollback
            for i, seg in enumerate(dl.segments):
                p_num = seg.part_number if seg.part_number else (i + 1)
                p_file = folder / f"{dl.target_filename}.part.{p_num}"
                
                if p_file.exists():
                    file_size = p_file.stat().st_size
                    if file_size < seg.last_checkpoint:
                        seg.downloaded_bytes = file_size
                        seg.last_checkpoint = file_size
                        dl.resume_state = ResumeState.UNSTABLE
                    elif file_size > seg.last_checkpoint:
                        with open(p_file, "r+b") as f:
                            f.truncate(seg.last_checkpoint)
                        seg.downloaded_bytes = seg.last_checkpoint
                else:
                    seg.downloaded_bytes = 0
                    seg.last_checkpoint = 0
                
                # Integrity check for completed segments
                if seg.is_complete and part_file.exists():
                    try:
                        current_start, current_end = self._compute_segment_hashes(part_file)
                        
                        if seg.start_hash and current_start != seg.start_hash:
                            dl.resume_state = ResumeState.UNSTABLE
                            with open(part_file, "wb") as f:
                                pass
                            seg.downloaded_bytes = 0
                            seg.last_checkpoint = 0
                        elif seg.end_hash and current_end != seg.end_hash:
                            dl.resume_state = ResumeState.UNSTABLE
                            with open(part_file, "wb") as f:
                                pass
                            seg.downloaded_bytes = 0
                            seg.last_checkpoint = 0
                    except Exception:
                        dl.resume_state = ResumeState.UNSTABLE

    def _compute_segment_hashes(self, path: Path):
        """Compute SHA256 of first and last 512KB."""
        size = path.stat().st_size
        chunk = 512 * 1024
        
        with open(path, "rb") as f:
            # Start Hash
            start_data = f.read(chunk)
            start_hash = hashlib.sha256(start_data).hexdigest()
            
            # End Hash
            if size > chunk:
                f.seek(-chunk, 2) # Seek from end
                end_data = f.read(chunk)
            else:
                end_data = start_data # Overlapping/same
            end_hash = hashlib.sha256(end_data).hexdigest()
            
        return start_hash, end_hash

    # ... start_download, resume_download call _start_workers ...

    def _start_workers(self, dl: Download):
        """Start download workers for the given download."""
        # [FIX] Auto-detect Torrent Source if missing (for existing broken tasks)
        # print(f"[DEBUG] _start_workers for {dl.id}. Source: {dl.source}, Segments: {len(dl.segments) if dl.segments else 0}")
        if not dl.source and dl.url:
            lower_url = dl.url.lower()
            if lower_url.endswith('.torrent') or lower_url.startswith('magnet:'):
                # print(f"[DEBUG] Auto-detected torrent source for {dl.target_filename}")
                dl.source = 'torrent'
                # Remove from discovery if it was there
                with self._lock: self._discovery_tasks.discard(dl.id)
                self.repository.save(dl)
                self._save_metadata(dl)


            
        # 0. Branch for Cut/Trim Tasks (PRIME PRIORITY)
        if dl.cut_range:
            with self._lock:
                dl.state = DownloadState.DOWNLOADING
                dl.error_message = None
                self.repository.save(dl)
                self._active_downloads[dl.id] = dl
                cancel_event = threading.Event()
                self._cancel_events[dl.id] = cancel_event
                dl._futures = []
            
            self._start_cut_pipeline(dl, cancel_event)
            return

        # 1. Branch for Standard Torrent Execution (fallback)
        if dl.source == 'torrent':
            with self._lock:
                dl.state = DownloadState.DOWNLOADING
                dl.error_message = None
                self.repository.save(dl)
                self._active_downloads[dl.id] = dl
                cancel_event = threading.Event()
                self._cancel_events[dl.id] = cancel_event
                dl._futures = []

            f = self.executor.submit(self._torrent_worker, dl, cancel_event)
            dl._futures.append(f)
            
            # Start monitor (Torrents use manual progress updates)
            threading.Thread(target=self._monitor_download, args=(dl, cancel_event), daemon=True).start()
            return

        # 1. Branch for YouTube Atomic Execution
        if dl.source in ['youtube', 'tiktok', 'spotify']:
            with self._lock:
                dl.state = DownloadState.DOWNLOADING
                dl.current_stage = "downloading"
                dl.error_message = None
                self.repository.save(dl)
                self._save_metadata(dl) # FORCE META CREATION
                self._active_downloads[dl.id] = dl
                cancel_event = threading.Event()
                self._cancel_events[dl.id] = cancel_event
                dl._futures = []

            if dl.source == 'spotify':
                f = self.executor.submit(self._spotify_worker, dl, cancel_event)
            else:
                f = self.executor.submit(self._youtube_atomic_worker, dl, cancel_event)
            dl._futures.append(f)
            
            # Start monitor (YouTube uses a simplified monitor or we reuse)
            threading.Thread(target=self._monitor_download, args=(dl, cancel_event), daemon=True).start()
            return

        # 2. Standard HTTP Pipeline
        self._validate_and_rollback(dl)
        
        folder = self._get_download_folder(dl)
        folder.mkdir(parents=True, exist_ok=True)
        self._save_metadata(dl)
        
        # Create shared .part file for Standard downloads ONLY
        # Partial downloads use separate .part.num files created in segment_worker
        # V2 Workspace tasks use shared data.part file
        if (dl.segments and dl.total_size and not dl.partial) or dl.task_id:
            filename = "data.part" if dl.task_id else f"{dl.target_filename}.part"
            part_file = folder / filename
            if not part_file.exists():
                # Create and preallocate full size for shared file
                with open(part_file, 'wb') as f:
                    f.seek(dl.total_size - 1)
                    f.write(b'\0')
        
        with self._lock:
            dl.state = DownloadState.DOWNLOADING
            dl.error_message = None
            dl.last_update = datetime.now()
            self.repository.save(dl)
            
            self._active_downloads[dl.id] = dl
            cancel_event = threading.Event()
            self._cancel_events[dl.id] = cancel_event
            dl._futures = []
        
        # Start workers
        if dl.cut_range:
            self._start_cut_pipeline(dl, cancel_event)
        elif dl.segments:
            for i, seg in enumerate(dl.segments):
                if not seg.is_complete:
                    f = self.executor.submit(self._segment_worker, dl, i, cancel_event)
                    dl._futures.append(f)
        else:
            # No segments - unknown size, use stream worker
            f = self.executor.submit(self._stream_worker, dl, cancel_event)
            dl._futures.append(f)
        
        threading.Thread(target=self._monitor_download, args=(dl, cancel_event), daemon=True).start()


    def _youtube_atomic_worker(self, dl: Download, cancel_event: threading.Event):
        """Dedicated worker for Atomic Downloads using yt-dlp library."""
        import yt_dlp
        import shutil
        
        folder = self._get_download_folder(dl)
        folder.mkdir(parents=True, exist_ok=True)
        dl.current_stage = "analyzing"
        self.repository.save(dl)

        def progress_hook(d):
            if cancel_event.is_set():
                raise Exception("Download cancelled by user")
            
            if d['status'] == 'downloading':
                try:
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    downloaded = d.get('downloaded_bytes', 0)
                    speed = d.get('speed', 0)
                    
                    if total > 0: dl.total_size = total
                    dl.speed_bps = speed
                    dl._downloaded_bytes_override = downloaded
                    
                    percent_str = d.get('_percent_str', '--%')
                    dl.current_stage = f"downloading {percent_str}"
                    
                    # Update monitor progress
                    if '%' in percent_str:
                         dl._manual_progress = float(percent_str.replace('%', ''))
                    else:
                         dl._manual_progress = 0.0

                    import time
                    now = time.time()
                    if not hasattr(progress_hook, 'last_save'): progress_hook.last_save = 0
                    if now - progress_hook.last_save > 0.5: 
                        self.repository.save(dl)
                        progress_hook.last_save = now
                except Exception:
                    pass
            elif d['status'] == 'finished':
                dl.current_stage = "processing"
                self.repository.save(dl)

        try:
            # Common Anti-blocking options
            common_opts = {
                'quiet': True, 'no_warnings': True,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                },
                'extractor_retries': 3, 'retries': 3, 
                'sleep_interval': 1, 'max_sleep_interval': 5,
                'geo_bypass': True,
            }

            # 1. EXTRACT METADATA & FIX EXTENSION
            final_ext = Path(dl.target_filename).suffix
            
            with yt_dlp.YoutubeDL(common_opts) as ydl:
                try:
                    info = ydl.extract_info(dl.url, download=False)
                    
                    # TikTok Title Fix
                    if dl.source == 'tiktok' and info.get('title'):
                        clean_title = sanitize_filename(info['title'])
                        
                        # Preserve existing extension concept or default to mp4
                        if not final_ext: final_ext = ".mp4"
                        
                        new_name = f"{clean_title}{final_ext}"
                        if dl.target_filename != new_name:
                            dl.target_filename = new_name
                            self.repository.save(dl)
                            self._save_metadata(dl)

                    # Determine EXTENSION (Single Source of Truth)
                    # For audio, we FORCE mp3 via postprocessor, so ext is .mp3
                    if dl.media_type == 'audio':
                         # If we don't have .mp3 in target, append/replace it
                         if not final_ext or final_ext != '.mp3':
                             dl.target_filename = Path(dl.target_filename).stem + ".mp3"
                             self.repository.save(dl)
                             self._save_metadata(dl)

                except Exception:
                     pass

            
            # 2. CONFIGURE DOWNLOAD
            target_path = folder / dl.target_filename
            
            # Format/Extension Logic
            
            selector = "best"
            if dl.source == 'youtube':
                if dl.media_type == 'audio':
                    selector = "bestaudio/best"
                else:
                    # STRICT QUALITY ENFORCEMENT
                    if dl.quality:
                        # Parse "720p" -> 720
                        try:
                            target_height = int(re.search(r'\d+', str(dl.quality)).group())
                            # STRICT SELECTOR: 
                            # 1. Native MP4 at exact height (Fastest, no re-encode)
                            # 2. Any format at exact height (Will be converted)
                            selector = (f"bestvideo[height={target_height}][ext=mp4]+bestaudio[ext=m4a]/"
                                      f"bestvideo[height={target_height}]+bestaudio/"
                                      f"best[height={target_height}]")
                        except (ValueError, AttributeError):
                            # Fallback only if parsing fails
                            selector = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
                    else:
                        # Default Behavior (Best Available)
                        selector = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            
            ydl_opts = common_opts.copy()
            ydl_opts.update({
                'format': selector,
                'outtmpl': str(folder / f"{Path(dl.target_filename).stem}.%(ext)s"), # Stem based template
                'noprogress': True, # CRITICAL FIX for UI artifact
                'progress_hooks': [progress_hook],
            })

            # Post-Processors (Audio -> MP3)
            if dl.media_type == 'audio':
                ydl_opts['postprocessors'] = [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                    {'key': 'FFmpegMetadata'},
                    {'key': 'EmbedThumbnail'}
                ]

            elif dl.media_type == 'video':
                 ydl_opts['postprocessors'] = [
                     {'key': 'EmbedThumbnail'},
                     {'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}
                 ]
            
            # 3. EXECUTE
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([dl.url])
                
            # 4. RECONCILE FILENAME
            # Find the file yt-dlp created (it might differ from target_filename if ext differs)
            found_file = None
            stem = Path(dl.target_filename).stem
            for f in folder.iterdir():
                if f.stem == stem and f.suffix:
                    found_file = f
                    break
            
            if not found_file:
                 raise Exception("Download finished but file not found in workspace.")
            
            # Update target_filename to match reality (extension wise)
            if found_file.name != dl.target_filename:
                dl.target_filename = found_file.name
                self.repository.save(dl)
                self._save_metadata(dl)

            # 5. FINALIZE
            if not cancel_event.is_set():
                
                dl.current_stage = "done"
                dl.complete()
                self.repository.save(dl)
                self._cleanup_on_completion(dl)

        except Exception as e:
            if not cancel_event.is_set():
                dl.fail(str(e))
                self.repository.save(dl)
                self._cleanup_on_completion(dl)

    def _vocals_loop(self):
        """Background worker for monitoring and processing vocals queue."""
        import time
        while not self.shutdown_event.is_set():
            task_to_process = None
            
            with self.vocals_lock:
                # Find first queued task
                for task in self.vocals_queue:
                    if task['status'] == 'queued':
                         task_to_process = task
                         task['status'] = 'processing'
                         task['progress'] = 0
                         break
            
            if task_to_process:
                try:
                    self.separate_vocals(
                         task_to_process['path'], 
                         use_gpu=task_to_process['gpu'], 
                         queue_task_ref=task_to_process,
                         keep_all=task_to_process.get('keep_all', False)
                    )
                    with self.vocals_lock:
                        task_to_process['status'] = 'done'
                        task_to_process['progress'] = 100
                except Exception as e:
                    # print(f"Vocals Task Failed: {e}")
                    with self.vocals_lock:
                         task_to_process['status'] = 'failed'
                         task_to_process['error'] = str(e)
            else:
                time.sleep(1.0)

    def queue_vocals(self, path: Path, use_gpu: bool = False, keep_all: bool = False) -> str:
        """Add a file to the vocals processing queue."""
        import uuid
        task_id = str(uuid.uuid4())[:8]
        with self.vocals_lock:
            self.vocals_queue.append({
                "id": task_id,
                "path": path,
                "gpu": use_gpu,
                "keep_all": keep_all,
                "status": "queued",
                "progress": 0,
                "error": None,
                "filename": path.name
            })
        return task_id

    def get_vocals_queue(self):
        """Return a copy of the current vocals queue."""
        with self.vocals_lock:
            return list(self.vocals_queue)

    def separate_vocals(self, file_path: Path, use_gpu: bool = False, cancel_event: threading.Event = None, queue_task_ref=None, keep_all: bool = False):
        """Stand-alone vocal separation for local files (Direct Mode)."""
        import subprocess
        import time
        import shutil
        
        cancel_event = cancel_event or threading.Event()
        
        # 0. Check for ffmpeg and demucs (already checked in worker, but safe to keep)
        if not shutil.which("ffmpeg") or not shutil.which("demucs"):
             if queue_task_ref:
                  queue_task_ref['error'] = "Missing dependencies (ffmpeg/demucs)"
             else:
                  print("Error: clean separation requires ffmpeg and demucs.")
             return

        # 1. Detect type
        is_video = file_path.suffix.lower() in ['.mp4', '.mkv', '.avi', '.mov']
        work_dir = file_path.parent
        temp_audio = None
        
        if not queue_task_ref:
             print("[vocals] processing")
        else:
             queue_task_ref['status'] = 'extracting audio...'
        
        try:
            # 2. Extract audio if video
            if is_video:
                temp_audio = work_dir / f"temp_audio_{int(time.time())}.mp3"
                subprocess.run([
                    "ffmpeg", "-y", "-i", str(file_path),
                    "-vn", "-ab", "128k", str(temp_audio)
                ], capture_output=True, check=True)
                input_file = temp_audio
            else:
                input_file = file_path
                
            # 3. Run Demucs (Direct CLI - No Library Dependencies)
            import os
            import sys
            device = "cuda" if use_gpu else "cpu"
            # Direct subprocess call to demucs CLI
            demucs_cmd = [sys.executable, "-m", "demucs", "--two-stems", "vocals", "-d", device]
            demucs_cmd.extend(["-o", ".", str(input_file.name)])
            
            if not queue_task_ref:
                print(f"[separate] vocals")
            
            self._run_demucs_with_progress(demucs_cmd, str(work_dir), task_ref=queue_task_ref)
            
            # 4. Find outputs
            stem = input_file.stem
            model_dir = work_dir / "htdemucs" / stem
            v_wav = model_dir / "vocals.wav"
            nv_wav = model_dir / "no_vocals.wav"
            
            if not v_wav.exists():
                raise Exception("Demucs finished but output files not found.")
                
            # 5. Finalize based on file type and keep_all flag
            if is_video:
                # Video: Always merge vocals back into video
                clean_video = work_dir / f"{file_path.stem}_clean{file_path.suffix}"
                subprocess.run([
                    "ffmpeg", "-y", "-i", str(file_path), "-i", str(v_wav),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", str(clean_video)
                ], capture_output=True, check=True)
                
                # If keep_all, also save separate audio files
                if keep_all:
                    final_v = work_dir / f"{file_path.stem}_vocals.wav"
                    final_nv = work_dir / f"{file_path.stem}_no_vocals.wav"
                    subprocess.run(["ffmpeg", "-y", "-i", str(v_wav), str(final_v)], capture_output=True, check=True)
                    subprocess.run(["ffmpeg", "-y", "-i", str(nv_wav), str(final_nv)], capture_output=True, check=True)
            else:
                # Audio: Always create output files if processed from download context or requested
                final_v = work_dir / f"{file_path.stem}_vocals.mp3"
                final_nv = work_dir / f"{file_path.stem}_no_music.mp3"
                
                # We always save at least vocals.wav if it's audio
                subprocess.run(["ffmpeg", "-y", "-i", str(v_wav), str(final_v)], capture_output=True, check=True)
                
                if keep_all and nv_wav.exists():
                    subprocess.run(["ffmpeg", "-y", "-i", str(nv_wav), str(final_nv)], capture_output=True, check=True)
                # else: vocals processed internally only, no output files
                
            if not queue_task_ref:
                print("[done]")
            else:
                queue_task_ref['status'] = 'done'


            
        finally:
            if temp_audio and temp_audio.exists(): temp_audio.unlink()
            shutil.rmtree(work_dir / "htdemucs", ignore_errors=True)

    def _process_vocals(self, dl: Download, cancel_event: threading.Event):
        """Worker-integrated vocal separation for YouTube tasks."""
        import subprocess
        import shutil
        import os
        
        folder = self._get_download_folder(dl)
        downloaded_file = folder / dl.target_filename
        
        # 0. Check for ffmpeg
        import shutil
        if not shutil.which("ffmpeg"):
            msg = "ffmpeg not found. "
            path = os.environ.get("PATH", "")
            if "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in path:
                msg += "On Termux, please install it via: pkg install ffmpeg"
            else:
                msg += "Please install ffmpeg to use vocals features."
            dl.fail(msg)
            return

        # 0.1 Check for demucs
        if not shutil.which("demucs"):
            dl.fail("'demucs' not found or not installed. Please install it via: pip install demucs")
            return
        
        if not downloaded_file.exists():
            raise Exception(f"Base file for vocals not found: {downloaded_file}")
            
        # 1. ROBUST MEDIA DETECTION (Handle .bin / no extension)
        # If media_type is unknown or extension is suspicious, probe it.
        ext = downloaded_file.suffix.lower()
        is_suspicious = ext in ['', '.bin', '.part', '.tmp']
        
        dl.current_stage = "analyzing media"
        self.repository.save(dl)
        
        # Check if it's actually a video regardless of media_type field
        actual_is_video = False
        try:
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                "-of", "csv=p=0", str(downloaded_file)
            ], capture_output=True, text=True).stdout
            if 'video' in probe:
                actual_is_video = True
        except:
            # Fallback to field if probe fails
            actual_is_video = dl.media_type == 'video'

        is_video = actual_is_video
        temp_audio = None
        
        # 2. NORMALIZATION
        # Demucs can be picky about extensions. We normalize to a temp .wav/mp3 for reliability.
        if is_video:
            dl.current_stage = "extracting audio"
            self.repository.save(dl)
            temp_audio = folder / f"vocals_norm_{dl.id}.wav"
            subprocess.run([
                "ffmpeg", "-y", "-i", str(downloaded_file),
                "-vn", "-ac", "2", "-ar", "44100", str(temp_audio)
            ], capture_output=True, check=True)
            input_file = temp_audio
        elif is_suspicious:
            # Normalize .bin or unknown audio to wav
            dl.current_stage = "normalizing audio"
            self.repository.save(dl)
            temp_audio = folder / f"vocals_norm_{dl.id}.wav"
            subprocess.run([
                "ffmpeg", "-y", "-i", str(downloaded_file),
                "-ac", "2", "-ar", "44100", str(temp_audio)
            ], capture_output=True, check=True)
            input_file = temp_audio
        else:
            input_file = downloaded_file
            
        dl.current_stage = "separating vocals"
        self.repository.save(dl)
        
        # Run Demucs (Direct CLI - No Library Dependencies)
        import sys
        device = "cuda" if dl.vocals_gpu else "cpu"
        # Direct subprocess call to demucs CLI
        demucs_cmd = [sys.executable, "-m", "demucs", "--two-stems", "vocals", "-d", device, "-o", ".", str(input_file.name)]
            
        # Run Demucs with Live Progress
        self._run_demucs_with_progress(demucs_cmd, str(folder), dl=dl)
        
        dl.current_stage = "finalizing vocals"
        self.repository.save(dl)

        # Find outputs - Robust recursive search
        v_wav = None
        for root, dirs, files in os.walk(str(folder)):
            if "vocals.wav" in files:
                v_wav = Path(root) / "vocals.wav"
                break
        
        if not v_wav or not v_wav.exists():
            raise Exception(f"Demucs finished but vocals.wav not found in {folder}. Files: {os.listdir(str(folder))}")
        
        nv_wav = v_wav.parent / "no_vocals.wav"
        
        if is_video:
            dl.current_stage = "merging clean audio"
            self.repository.save(dl)
            
            # Smart extension handling
            base_stem = downloaded_file.stem
            orig_ext = downloaded_file.suffix if downloaded_file.suffix and downloaded_file.suffix != '.bin' else '.mp4'
            clean_name = f"{base_stem}_clean{orig_ext}"
            clean_video = folder / clean_name
            subprocess.run([
                "ffmpeg", "-y", "-i", str(downloaded_file), "-i", str(v_wav),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", str(clean_video)
            ], capture_output=True, check=True)
            downloaded_file.unlink()
            clean_video.rename(downloaded_file)
        else:
            # Audio downloads: Always create output files (download context)
            # Note: This differs from separate_vocals which respects keep_all flag
            final_v = folder / (downloaded_file.stem + "_vocals.mp3")
            final_nv = folder / (downloaded_file.stem + "_no_music.mp3")
            
            subprocess.run(["ffmpeg", "-y", "-i", str(v_wav), str(final_v)], capture_output=True, check=True)
            if nv_wav.exists():
                subprocess.run(["ffmpeg", "-y", "-i", str(nv_wav), str(final_nv)], capture_output=True, check=True)
            
        if temp_audio and temp_audio.exists(): temp_audio.unlink()
        if (folder / "htdemucs").exists():
            shutil.rmtree(folder / "htdemucs", ignore_errors=True)

    def _run_demucs_with_progress(self, cmd: list, cwd: str, dl: Optional[Download] = None, task_ref: dict = None):
        """Execute demucs and parse progress from stderr."""
        import subprocess
        import os
        import re
        import sys
        
        # 0. Validate environment before starting
        from dlm.core.env_validator import validate_vocals_environment
        errors, warnings = validate_vocals_environment()
        if errors:
            error_msg = "Vocals environment validation failed:\n" + "\n".join(errors)
            if dl: dl.error = error_msg
            if task_ref: task_ref['error'] = error_msg
            raise Exception(error_msg)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["TORCHAUDIO_BACKEND"] = "soundfile"  # Force soundfile to avoid torchcodec DLL errors
        
        # 1. Isolate process from console signals (Windows)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        # Start process
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # Merge for reading
            text=True,
            encoding='utf-8',
            errors='replace',
            cwd=cwd,
            env=env,
            bufsize=1,
            universal_newlines=True,
            creationflags=creationflags
        )

        progress_re = re.compile(r"(\d+)%")
        last_percent = -1
        
        full_output = []
        try:
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                
                if line:
                    full_output.append(line.strip())
                    if len(full_output) > 50: full_output.pop(0) # Keep last 50 lines
                    
                    match = progress_re.search(line)
                    if match:
                        percent = int(match.group(1))
                        if percent != last_percent:
                            if dl:
                                dl.progress = float(percent)
                                if percent % 5 == 0: # Throttle DB writes
                                    self.repository.save(dl)
                            elif task_ref:
                                task_ref['progress'] = percent
                                # No DB save needed for queue task
                            else:
                                filled = percent // 5
                                bar = "█" * filled + "░" * (20 - filled)
                                sys.stdout.write(f"\r[separate] vocals [{bar}] {percent}%")
                                sys.stdout.flush()
                            last_percent = percent

            
            process.wait()
            if not dl: print() # New line after \r
            
            if process.returncode != 0:
                error_context = "\n".join(full_output[-10:])
                raise Exception(f"Demucs process failed with code {process.returncode}.\nLast output:\n{error_context}")
                
        except Exception as e:
            if process.poll() is None:
                process.kill()
            raise e
        



    def _spotify_worker(self, dl: Download, cancel_event: threading.Event):
        """Worker for Spotify downloads."""
        import yt_dlp
        from dlm.extractors.spotify.extractor import SpotifyExtractor
        from dlm.app.services import sanitize_filename 

        folder = self._get_download_folder(dl)
        
        try:
            # 1. Re-fetch Metadata
            extractor = SpotifyExtractor(self.config)
            result = extractor.extract(dl.url)
            if not result or not result.metadata:
                raise Exception("Failed to retrieve Spotify metadata")
            
            meta = result.metadata
            query = f"{meta.artist} {meta.title}".strip()
            dl.current_stage = f"Searching: {query}"
            self.repository.save(dl)
            
            # 2. Search YouTube
            search_query = f"ytsearch10:{query}"
            match_url = None
            
            # Silence Search
            with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'noprogress': True}) as ydl:
                info = ydl.extract_info(search_query, download=False)
                if info and 'entries' in info:
                    target_duration = meta.duration / 1000.0
                    best_diff = float('inf')
                    for e in info['entries']:
                        if not e: continue
                        dur = e.get('duration', 0)
                        diff = abs(dur - target_duration)
                        if diff < 15: # 15s tolerance
                             if diff < best_diff:
                                 best_diff = diff
                                 match_url = e.get('webpage_url')
            
            if not match_url:
                 if info['entries']: match_url = info['entries'][0]['webpage_url']
                 else: raise Exception("No match found")

            # 3. Filename Setup
            # User Preference: Just Title, no Artist prefix
            final_name = f"{meta.title}.mp3"
            final_name = sanitize_filename(final_name)
            
            dl.target_filename = final_name
            self.repository.save(dl)
            
            # 4. Download
            dl.current_stage = "Downloading Audio"
            self.repository.save(dl)

            def progress_hook(d):
                if cancel_event.is_set(): raise Exception("Cancelled")
                if d['status'] == 'downloading':
                     try:
                         # Progress
                         total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                         downloaded = d.get('downloaded_bytes', 0)
                         speed = d.get('speed', 0)
                         
                         if total > 0: dl.total_size = total
                         dl.speed_bps = speed
                         dl._downloaded_bytes_override = downloaded
                         
                         percent = d.get('_percent_str', '--%')
                         dl.current_stage = f"downloading {percent}"
                         if '%' in percent: 
                             dl._manual_progress = float(percent.replace('%', ''))
                         
                         import time
                         now = time.time()
                         if not hasattr(progress_hook, 'last_save'): progress_hook.last_save = 0
                         if now - progress_hook.last_save > 0.5:
                             self.repository.save(dl)
                             progress_hook.last_save = now
                     except: pass

            target_path = folder / final_name
            
            # Spotify always audio -> mp3
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': str(folder / f"{Path(final_name).stem}.%(ext)s"), # Use stem only, let yt-dlp add ext
                'quiet': True,
                'no_warnings': True,
                'noprogress': True,  # CRITICAL FIX for UI artifact
                'progress_hooks': [progress_hook],
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([match_url])
            
            # 5. Fix Filename
            # We used stem in template, so yt-dlp outputted stem.mp3 (after conversion)
            # This matches final_name which is stem.mp3
            # So file should exist at target_path
            
            if not target_path.exists():
                 # Fallback: find by glob if something weird happened
                 found = list(folder.glob(f"{Path(final_name).stem}*.mp3"))
                 if found:
                     found[0].rename(target_path)
                 else:
                     raise Exception(f"Download missing at {target_path}")

            # 6. Finalize
            dl.current_stage = "done"
            dl.complete()
            self.repository.save(dl)
            self._cleanup_on_completion(dl)

        except Exception as e:
            if not cancel_event.is_set():
                dl.fail(str(e))
                self.repository.save(dl)
            self._cleanup_on_completion(dl)

        except Exception as e:
            dl.state = DownloadState.FAILED
            dl.error_message = str(e)
            self.repository.save(dl)
            print(f"Spotify Worker Error: {e}")

    def _start_cut_pipeline(self, dl: Download, cancel_event: threading.Event):
        """Dedicated pipeline for Cut/Trim tasks."""
        # 1. Update State
        with self._lock:
            dl.state = DownloadState.DOWNLOADING
            self.repository.save(dl)
            
        # 2. Submit Worker
        # We use a specific worker that handles the full lifecycle for cuts
        f = self.executor.submit(self._cut_worker, dl, cancel_event)
        dl._futures.append(f)

    def _cut_worker(self, dl: Download, cancel_event: threading.Event):
        """Worker that performs Download Range -> Convert -> Finalize."""
        if dl.source == 'youtube':
             pass # Atomic YouTube worker is compatible


        folder = self._get_download_folder(dl)
        temp_file = folder / "temp_cut.part"
        
        try:
             # Parse Range (Simple implementation: expect '00:00:00-00:00:00' or similar)
            start_str, end_str = dl.cut_range.split('-')
            
            # 1. Resolve Direct Stream URL
            # Standard download tools fail on YouTube Page URLs. We MUST get the direct stream.
            stream_url = dl.url
            if self.media_service:
                try:
                    resolved = self.media_service.resolve_stream_url(dl.url, dl.media_type)
                    if resolved:
                        stream_url = resolved
                except Exception as e:
                    pass # Silent warning
            
            # 2. Build FFMPEG Command with Stream URL
            # We use output seeking (-ss after -i) for maximum accuracy on streams,
            # but input seeking (-ss before -i) for speed. 
            # Combining both is usually best: -ss before -i to get close, then -ss after -i to be precise.
            
            # Calculate target duration for progress
            def to_seconds(ts):
                parts = ts.strip().split(':')
                return sum(float(x) * 60**i for i, x in enumerate(reversed(parts)))
            
            target_duration = to_seconds(end_str) - to_seconds(start_str)
            if target_duration <= 0: target_duration = 1 # Avoid div by zero
            
            # Rebuilding command for accuracy and progress
            # -progress - tells ffmpeg to output progress info to stdout/stderr
            cmd = [
                "ffmpeg", "-y",
                "-ss", start_str.strip(),
                "-to", end_str.strip(),
                "-i", stream_url,
                "-progress", "pipe:1"
            ]
            
            if dl.media_type == 'audio':
                 cmd += [
                    "-vn",
                    "-acodec", "libmp3lame",
                    "-q:a", "2",
                    str(temp_file.with_suffix(".mp3"))
                 ]
                 temp_file = temp_file.with_suffix(".mp3")
            else:
                 cmd += [
                    "-c", "copy",
                    str(temp_file.with_suffix(".mp4"))
                 ]
                 temp_file = temp_file.with_suffix(".mp4")
            
            # Ensure folder exists
            folder.mkdir(parents=True, exist_ok=True)
            
            # Run FFMPEG with progress parsing
            import subprocess
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            
            for line in process.stdout:
                if cancel_event.is_set():
                    process.terminate()
                    return
                
                # Parse: out_time_ms=12345678
                if "out_time_ms=" in line:
                    try:
                        ms = int(line.split('=')[1].strip())
                        current_sec = ms / 1000000.0
                        percent = min(100.0, (current_sec / target_duration) * 100.0)
                        dl._manual_progress = percent
                        dl.current_stage = f"cutting {percent:.1f}%"
                        
                        # Fake speed/size for monitor
                        dl.speed_bps = 500000 # Just to show activity
                        if not dl.segments: dl.segments = [Segment(0, 100)]
                        dl.segments[0].downloaded_bytes = int(percent)
                        
                        # Save periodically
                        import time
                        if not hasattr(_cut_worker, 'last_save'): _cut_worker.last_save = 0
                        if time.time() - _cut_worker.last_save > 1.0:
                            self.repository.save(dl)
                            _cut_worker.last_save = time.time()
                    except: pass

            process.wait()
            
            if process.returncode != 0:
                raise Exception(f"FFmpeg failed with code {process.returncode}")
            
            # 3. Finalize
            if not cancel_event.is_set():
                # Strict Validation
                if not temp_file.exists():
                    raise Exception("Output file missing")
                
                if temp_file.stat().st_size == 0:
                    # Clean up
                    try: temp_file.unlink()
                    except: pass
                    raise Exception("Output file is empty (0 bytes). Steam might be invalid.")

                final_name = dl.target_filename or (f"cut_{dl.id}.mp3" if dl.media_type == 'audio' else f"cut_{dl.id}.mp4")
                final_path = folder / final_name
                
                # Move temp to final
                import shutil
                shutil.move(str(temp_file), str(final_path))
                
                # [NEW] Check for Vocals Separation
                if dl.audio_mode == 'vocals':
                    # [MOVE] Use background queue instead of synchronous processing
                    final_file = folder / final_name
                    if final_file.exists():
                        self.queue_vocals(final_file, use_gpu=dl.vocals_gpu, keep_all=dl.vocals_keep_all)
                        print(f"[VOCALS] Cut output '{final_name}' added to background queue.")

                # Mark Complete
                dl.complete()
                self.repository.save(dl)
                self._cleanup_on_completion(dl)
                
        except Exception as e:
            if not cancel_event.is_set():
                if dl.state == DownloadState.DOWNLOADING: # Only fail if still active
                     dl.fail(str(e))
                     self.repository.save(dl)
                # print(f"Cut failed: {e}") # Removed print call

    def _try_rebalance(self, dl: Download, cancel_event: threading.Event):
        """Phase B: Smart Segment Rebalancing."""
        with self._lock:
            # Check rebalance conditions
            if dl.state != DownloadState.DOWNLOADING or not dl.resumable or dl.resume_state == ResumeState.UNSTABLE:
                return

            active_segments = [s for s in dl.segments if not s.is_complete]
            # If we have reached or exceeded max connections, or no segments left, no rebalance
            if len(active_segments) >= dl.max_connections or len(active_segments) == 0:
                # If no active segments, check for completion (Fix for 100% hang)
                if len(active_segments) == 0 and all(s.is_complete for s in dl.segments):
                     # Gate: Ensure we don't finalize twice (Monitor vs Worker)
                     if dl.current_stage != "finalizing" and dl.state == DownloadState.DOWNLOADING:
                         dl.current_stage = "finalizing"
                         self.repository.save(dl)
                         self.executor.submit(self._finalize_download, dl)
                return

            # Find slowest segment (approx by largest remaining bytes)
            candidate = None
            max_remaining = 0
            
            for seg in active_segments:
                remaining = seg.end_byte - (seg.start_byte + seg.downloaded_bytes)
                if remaining > max_remaining:
                    max_remaining = remaining
                    candidate = seg
            
            # Threshold: 8 MB
            if not candidate or max_remaining < 8 * 1024 * 1024:
                return

            # Split
            # Validate safe split point
            current_offset = candidate.start_byte + candidate.downloaded_bytes
            mid_point = current_offset + (max_remaining // 2)
            
            # Create new segment
            original_end = candidate.end_byte
            candidate.end_byte = mid_point
            
            new_seg = Segment(mid_point + 1, original_end)
            new_seg.last_checkpoint = 0 # New segment starts fresh
            
            dl.segments.append(new_seg)
            new_index = len(dl.segments) - 1
            
            # Persist
            self.repository.save(dl)
            self._save_metadata(dl)
            
            # Spawn worker for new segment
            if not cancel_event.is_set():
                 f = self.executor.submit(self._segment_worker, dl, new_index, cancel_event)
                 if hasattr(dl, '_futures'):
                     dl._futures.append(f)

    def _segment_worker(self, dl: Download, segment_index: int, cancel_event: threading.Event):
        # print("❌ تم التنفيذ عن طريق الخطأ عبر مسار HTTP")
        seg = dl.segments[segment_index]
        offset = seg.start_byte + seg.downloaded_bytes
        
        if offset > seg.end_byte:
            # Segment finished (or empty), try to rebalance others
            self._try_rebalance(dl, cancel_event)
            return

        folder = self._get_download_folder(dl)
        
        # CRITICAL: Workspace-linked tasks (v2) MUST use data.part and shared file mode
        if dl.task_id:
            part_file = folder / "data.part"
            use_shared_file = True
        elif dl.partial:
            # Legacy partial downloads: separate file for each assigned part
            part_num = seg.part_number or (segment_index + 1)
            part_file = folder / f"{dl.target_filename}.part.{part_num}"
            use_shared_file = False
        else:
            # Standard segmented download: single shared file
            part_file = folder / f"{dl.target_filename}.part"
            use_shared_file = True
        
        checkpoint_interval = 4 * 1024 * 1024 # 4MB
        bytes_since_checkpoint = 0
        
        # Retry Policy
        max_retries = 3
        retry_delay = 1
        
        try:
            for attempt in range(max_retries + 1):
                
                # Strict Byte Enforcement (Dynamic Check for Race Condition)
                # ... [Code preserved] ...

                try:
                    # Request only exactly what is needed
                    current_offset = seg.start_byte + seg.downloaded_bytes
                    if current_offset > seg.end_byte:
                         # Rebalance call handled in finally
                         return

                    # Note: We request up to the CURRENT end_byte. 
                    # If it shrinks, we receive more than needed, but we catch it below.
                    if dl.resumable:
                        iter_data = self.network.download_range(
                            dl.url, 
                            current_offset, 
                            seg.end_byte, 
                            referer=dl.referer,
                            headers=dl.captured_headers,
                            cookies=dl.captured_cookies,
                            user_agent=dl.user_agent
                        )
                    else:
                        # Fallback for Strict CDNs (Single Connection, No Range)
                        # We must stream from start.
                        if seg.start_byte != 0:
                             raise Exception("Cannot segment non-resumable download")
                        
                        # Reset file pointer to 0 for fresh stream
                        current_offset = 0
                        seg.downloaded_bytes = 0
                        
                        iter_data = self.network.download_stream(
                            dl.url, 
                            referer=dl.referer,
                            headers=dl.captured_headers,
                            cookies=dl.captured_cookies,
                            user_agent=dl.user_agent
                        )
                    
                    # File opening logic based on shared vs separate files
                    if use_shared_file:
                        # Shared file: use r+b mode and seek to position
                        mode = "r+b"
                        file_offset = seg.start_byte + seg.downloaded_bytes
                    else:
                        # Separate file: append or write mode
                        mode = "ab" if seg.downloaded_bytes > 0 else "wb"
                        file_offset = None
                    
                    with open(part_file, mode) as f:
                        # Seek to correct position for shared file
                        if use_shared_file and file_offset is not None:
                            f.seek(file_offset)
                        
                        for chunk in iter_data:
                            if cancel_event.is_set():
                                return
                            
                            # RACE CONDITION FIX:
                            # Always read the fresh authoritative end_byte
                            current_end = seg.end_byte
                            current_expected_size = current_end - seg.start_byte + 1
                            current_remaining = current_expected_size - seg.downloaded_bytes
                            
                            if current_remaining <= 0:
                                 # Segment was shrunk and we are now done/over.
                                 # If we are OVER (negative), we MUST truncate the file to avoid corruption.
                                 if current_remaining < 0:
                                     f.flush()
                                     f.truncate(current_expected_size)
                                     seg.downloaded_bytes = current_expected_size
                                 break
                            
                            # Strict Safety: Truncate overflow
                            if len(chunk) > current_remaining:
                                chunk = chunk[:current_remaining]
                            
                            if not chunk:
                                 break

                            f.write(chunk)
                            f.flush() # Ensure flush to disk
                            
                            seg.downloaded_bytes += len(chunk)
                            bytes_since_checkpoint += len(chunk)
                            
                            if bytes_since_checkpoint >= checkpoint_interval:
                                seg.last_checkpoint = seg.downloaded_bytes
                                bytes_since_checkpoint = 0
                            
                            if seg.downloaded_bytes >= (seg.end_byte - seg.start_byte + 1):
                                break # STRICT BREAK: Exact size reached
                    
                    # Check completion AFTER loop
                    if seg.downloaded_bytes == (seg.end_byte - seg.start_byte + 1):
                        # Completion Checkpoint & Hash
                        seg.last_checkpoint = seg.downloaded_bytes 
                        start_h, end_h = self._compute_segment_hashes(part_file)
                        seg.start_hash = start_h
                        seg.end_hash = end_h
                        
                        return # Success (finally will trigger rebalance)
                    else:
                        # Stream ended but not enough bytes (Premature Close)
                         if attempt < max_retries:
                            time.sleep(retry_delay)
                            retry_delay *= 2
                            continue
                         else:
                            print(f"Segment {segment_index} Failed: Stream ended prematurely ({seg.downloaded_bytes}/{(seg.end_byte - seg.start_byte + 1)})")
                            return

                except Exception as e:
                    # Import exceptions locally
                    from dlm.infra.network.http import NetworkError, ServerError
                    
                    if cancel_event.is_set():
                        return
                    
                    if isinstance(e, ServerError):
                        if "403" in str(e) or "401" in str(e) or "410" in str(e):
                            # SMART RENEW TRIGGER
                            print(f"[RENEW] 403 Error detected for {dl.target_filename}. Triggering Smart Renew.")
                            self.trigger_renewal(dl.id)
                            return
                        if attempt < 2: 
                            time.sleep(retry_delay)
                            retry_delay *= 2
                            continue
                        else:
                            return 
                    
                    elif isinstance(e, NetworkError) or isinstance(e, Exception): 
                        if attempt < max_retries:
                            time.sleep(retry_delay)
                            retry_delay *= 2
                            continue
                        else:
                            return
                            
        finally:
            if not cancel_event.is_set():
                # Guaranteed check for completion or re-spawn
                self._try_rebalance(dl, cancel_event)

    # ... _stream_worker ...


    def _stream_worker(self, dl: Download, cancel_event: threading.Event):
        folder = self._get_download_folder(dl)
        # Use data.part for workspace tasks, otherwise {filename}.part
        part_file = folder / ("data.part" if dl.task_id else f"{dl.target_filename}.part")
        try:
            iter_data = self.network.download_stream(
                dl.url, 
                referer=dl.referer,
                headers=dl.captured_headers,
                cookies=dl.captured_cookies,
                user_agent=dl.user_agent
            )
            
            downloaded_count = 0
            with open(part_file, "wb") as f:
                for chunk in iter_data:
                    if cancel_event.is_set():
                        return
                    f.write(chunk)
                    if not dl.segments:
                        dl.segments.append(Segment(0, 0))
                    dl.segments[0].downloaded_bytes += len(chunk)
                    downloaded_count += len(chunk)
            
            # [FIX] Enforce Strict Completion if Size Known
            if dl.total_size and dl.total_size > 0:
                if downloaded_count != dl.total_size:
                     raise Exception(f"Incomplete transfer: Expected {dl.total_size}, got {downloaded_count}")
            
            # Post-stream safety check:
            # If we downloaded very small amount (e.g. < 200KB) and total size is still 0, 
            # it's likely a failure that wasn't caught (e.g. site says 200 OK but serves HTML).
            if downloaded_count < 1024 * 200 and (dl.total_size or 0) == 0:
                if part_file.exists() and part_file.stat().st_size > 0:
                    with open(part_file, "rb") as bf:
                        snippet = bf.read(1024).lower()
                        if b"<!doctype html" in snippet or b"<html" in snippet or b"<head" in snippet:
                            raise Exception("Server returned an HTML error page. Your session may be expired or the site is blocking direct download.")

        except Exception as e:
            if not cancel_event.is_set():
                if "403" in str(e) or "401" in str(e) or "410" in str(e):
                    # SMART RENEW TRIGGER for streams
                    print(f"[RENEW] 403 Error detected for {dl.target_filename}. Triggering Smart Renew.")
                    self.trigger_renewal(dl.id)
                else:
                    dl.fail(f"Stream error: {e}")
                    if not getattr(dl, 'ephemeral', False):
                        self.repository.save(dl)

    def _monitor_download(self, dl: Download, cancel_event: threading.Event):
        last_bytes = dl.get_downloaded_bytes()
        last_time = time.time()
        
        # Adaptive Logic State
        last_scaling_time = time.time()
        last_speed = 0.0
        
        while not cancel_event.is_set():
            time.sleep(1)
            
            # Stop if deleted
            if getattr(dl, "deleted", False):
                self._wait_and_cleanup(dl)
                return

            # Stop if failed (Worker reported error)
            if dl.state == DownloadState.FAILED:
                self._on_task_terminated(dl)
                return

            # Calculate speed
            current_bytes = dl.get_downloaded_bytes()
            current_time = time.time()
            elapsed = current_time - last_time
            
            if elapsed > 0:
                bytes_diff = current_bytes - last_bytes
                dl.speed_bps = bytes_diff / elapsed
                last_bytes = current_bytes
                last_time = current_time
            
            # Feature C: Adaptive Connections
            # Only adapt if resumable and downloading
            if dl.resumable and dl.state == DownloadState.DOWNLOADING:
                now = time.time()
                if now - last_scaling_time > 30: # Check every 30s
                    # If speed increased significantly, we are good.
                    # If speed is low, maybe we are throttled?
                    # Simple heuristic: If we have many segments but low speed per segment, maybe too many.
                    
                    # Probing: Try increasing if < 8
                    if dl.max_connections < 8:
                         dl.max_connections += 1
                         # The rebalancer will pick this up
                    
                    last_scaling_time = now
            
            dl.last_update = datetime.now()
            
            # Persist Progress
            if not getattr(dl, 'ephemeral', False):
                self.repository.save(dl)
                self._save_metadata(dl)
            
            # Check Completion
            if dl.segments and all(s.is_complete for s in dl.segments):
                if dl.partial:
                    # PARTIAL downloads: Do NOT assemble/finalize.
                    dl.complete()
                    dl.speed_bps = 0.0
                    self._on_task_terminated(dl)
                    return

                # Race Guard: Check if worker thread already picked it up
                if dl.current_stage == "finalizing" or dl.source in ['youtube', 'tiktok', 'spotify', 'torrent']:
                     return

                dl.current_stage = "finalizing"
                if not getattr(dl, 'ephemeral', False):
                    self.repository.save(dl)
                self._finalize_download(dl)
                return
        
        # Stop if deleted (double check)
        if getattr(dl, "deleted", False):
            self._wait_and_cleanup(dl)
            return

        # Cancelled - save final state
        dl.last_update = datetime.now()
        if not getattr(dl, 'ephemeral', False):
            self.repository.save(dl)
            self._save_metadata(dl)

    def _wait_and_cleanup(self, dl: Download):
        """Wait for workers to finish and clean up files."""
        # Wait for threads to stop writing
        if hasattr(dl, '_futures'):
            from concurrent.futures import wait
            try:
                wait(dl._futures, timeout=10) # 10s max wait
            except: pass
        
        # Clean up files
        folder = self._get_download_folder(dl)
        self._async_cleanup(folder) # Reuse logic, but run here

    def _finalize_download(self, dl: Download):
        """Assembles segments if necessary and triggers cleanup."""
        try:
            # [CRITICAL] Wait for all worker threads to release file handles (Windows Locking)
            if hasattr(dl, '_futures') and dl._futures:
                from concurrent.futures import wait
                # Filter out done futures? wait() handles it.
                # But if WE are running inside a future (e.g. via submit), we shouldn't wait for ourselves?
                # _finalize_download is called by monitor (thread) or _try_rebalance (worker).
                # If called by worker, it might be in dl._futures.
                # However, usually _stream_worker finishes then we finalize.
                # Monitor calls it. Monitor is NOT in _futures.
                try:
                    wait(dl._futures, timeout=30)
                except Exception:
                    pass

            # --- V2 Workspace Logic ---
            if dl.task_id:
                # This is a workspace part task.
                # Do NOT merge or rename data.part. It is shared.
                # Just mark segments as done.
                self._mark_workspace_segments_done(dl)
                dl.complete()
                dl.speed_bps = 0.0
                dl.progress = 100.0
                self.repository.save(dl)
                return
            # --------------------------

            folder = self._get_download_folder(dl)
            target_filename = dl.target_filename or f"download_{dl.id}"
            workspace_file = folder / target_filename
            

            # 2. Handle Single .part file
            # Consolidate: use 'data.part' for workspace tasks, '{filename}.part' for others
            part_filename = "data.part" if dl.task_id else f"{target_filename}.part"
            part_file = folder / part_filename
            
            if part_file.exists() and not workspace_file.exists():
                part_file.rename(workspace_file)

            # 3. Validation
            if not workspace_file.exists() and not dl.partial:
                # If it's not partial and no file exists, something failed.
                # yt-dlp might have finished but we missed the filename?
                # Let's check for any file in workspace that might be it.
                if folder.exists():
                    files = [f for f in folder.iterdir() if f.is_file() and f.name != "dlm.meta"]
                    if files:
                        workspace_file = files[0]
                        dl.target_filename = workspace_file.name
                    else:
                        dl.fail("Finalization failed: Output file missing in workspace.")
                        self._on_task_terminated(dl)
                        return
                else:
                    dl.fail("Finalization failed: Workspace directory missing.")
                    self._on_task_terminated(dl)
                    return

            if workspace_file.exists() and workspace_file.stat().st_size == 0 and not dl.partial:
                 dl.fail("Finalization failed: Output file is empty.")
                 self._on_task_terminated(dl)
                 return

            # 4. Success State
            from dlm.core.entities import IntegrityState
            dl.integrity_state = IntegrityState.VERIFIED
            
            # [NEW] Vocals Support for HTTP/Browser downloads
            if dl.audio_mode == 'vocals':
                try:
                    dl.current_stage = "processing"
                    self.repository.save(dl)
                    self._process_vocals(dl, cancel_event)
                except Exception as v_err:
                    print(f"[VOCALS] Error: {v_err}")

            dl.complete()
            dl.speed_bps = 0.0
            self.repository.save(dl)

            # 5. Trigger Cleanup
            # This will move the file from workspace to target_dir and delete workspace
            self._cleanup_on_completion(dl)
            
        except Exception as e:
            dl.fail(f"Finalization error: {e}")
            self._on_task_terminated(dl)

    def _mark_workspace_segments_done(self, dl: Download):
        """Mark corresponding segments as done in the workspace."""
        if not dl.task_id or not dl.assigned_parts_summary:
            return
            
        # assigned_parts_summary should store list of parts, e.g. "1,2,3"
        # We need to parse it. 
        # Wait, Download entity has assigned_parts_summary logic?
        # Assuming it's a comma-separated string of part numbers.
        
        from dlm.core.workspace import WorkspaceManager
        # We need to find the workspace path.
        # Ideally stored in dl or we search.
        # But we know structure: __workspace__/task_{id}
        # dl.task_id IS the UUID.
        
        # We can construct path since we know root.
        wm = WorkspaceManager(self.download_dir.parent)
        task_folder = wm.get_task_folder_by_id(dl.task_id)
        if not task_folder:
            return 
            
        segments_dir = wm.get_segments_dir(task_folder)
        
        parts = [int(p) for p in dl.assigned_parts_summary.split(',') if p.strip().isdigit()]
        for p in parts:
             done_file = segments_dir / f"{p:03d}.done"
             done_file.touch()
             
             # Clean up missing file
             missing_file = segments_dir / f"{p:03d}.missing"
             if missing_file.exists():
                 try:
                     missing_file.unlink()
                 except: pass


    def get_download(self, download_id: str) -> Optional[Download]:
        # Return in-memory state if active, otherwise from DB
        with self._lock:
            # Check ephemeral first
            if download_id in self._ephemeral_memory:
                return self._ephemeral_memory[download_id]
            if download_id in self._active_downloads:
                return self._active_downloads[download_id]

    def get_all_downloads(self, include_ephemeral: bool = False):
        """Get all downloads, optionally including in-memory ephemeral ones."""
        dls = self.repository.get_all()
        if include_ephemeral:
            # Merge ephemeral tasks
            ephemeral_list = list(self._ephemeral_memory.values())
            dls.extend(ephemeral_list)
        return dls
        return self.repository.get(download_id)

    def import_from_manifest(self, manifest_path: str, parts_filter: list = None, as_separate_tasks: bool = False, folder_id: int = None, target_id: int = None) -> str:
        """Import a partial download task from a manifest file."""
        # MANDATORY: Import MUST be executed ONLY inside a task workspace folder (depth >= 1)
        depth = self._get_workspace_depth(folder_id)
        if depth is None or depth < 1:
            raise ValueError(
                "Import rule violation: You MUST navigate into a task workspace folder first.\n"
                "Example: cd /__workspace__/my_task.bin\n"
            )

        # Logic: Tasks are created in target_id. 
        # If target_id is None, we default to Root (None) if we are inside workspace,
        # or to folder_id if we are outside (though validation above blocks outside, so default is Root).
        creation_id = target_id
        if creation_id is None and depth is None:
             # Should be unreachable due to validation, but good for robust fallback
             creation_id = folder_id

        # Load manifest
        m_path = Path(manifest_path)
        if not m_path.exists():
            raise ValueError(f"Manifest file not found: {manifest_path}")
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except Exception as e:
            raise ValueError(f"Failed to load manifest: {e}")
        
        # Validate manifest type
        manifest_type = manifest.get('manifest_type')
        if manifest_type in ['v2', 'dlm.task.v2', 'youtube.split.v2']:
            return self._import_v2(manifest, parts_filter, as_separate_tasks, creation_id, manifest_path)
        elif manifest_type not in ['dlm.parts.v1', 'youtube.user.parts.v1']:
            raise ValueError(f"Unsupported manifest type: {manifest_type}")
        
        # ... V1 Logic ...
        # Validate required fields
        required_fields = ['task_id', 'url', 'filename']
        if manifest_type == 'youtube.user.parts.v1':
             required_fields += ['media_type']
        
        for field in required_fields:
            if field not in manifest:
                raise ValueError(f"Missing required field: {field}")
        
        if 'assigned_parts' not in manifest:
            raise ValueError("Manifest must contain 'assigned_parts'")
        
        # Extract fields
        task_id = manifest['task_id']
        url = manifest['url']
        filename = manifest['filename']
        assigned_parts = manifest.get('assigned_parts', [])
        
        # APPLY FILTER
        if parts_filter:
            assigned_parts = [p for p in assigned_parts if p['part'] in parts_filter]
            if not assigned_parts:
                raise ValueError(f"No parts matching filter: {parts_filter}")

        # Prepare summary fields
        actual_creation_id = creation_id
        folder_name = "root"
        if creation_id:
            f = self.repository.get_folder(creation_id)
            if f: folder_name = f['name']

        if as_separate_tasks:
            # Create a virtual folder for this file
            target_folder_name = Path(filename).stem
            try:
                actual_creation_id = self.repository.create_folder(target_folder_name, creation_id)
                folder_name = target_folder_name
                print(f"[Import] Created folder '{target_folder_name}' at destination")
            except Exception:
                existing = self.repository.get_folder_by_name(target_folder_name, creation_id)
                if existing:
                    actual_creation_id = existing['id']
                    folder_name = target_folder_name
                else:
                    raise

        import uuid
        def create_download_from_parts(parts_for_task, custom_name=None):
            dl = Download(url=url)
            dl.id = str(uuid.uuid4())
            dl.target_filename = custom_name or filename
            dl.partial = True
            dl.task_id = task_id
            dl.source = 'youtube' if manifest_type == 'youtube.user.parts.v1' else None
            dl.media_type = manifest.get('media_type')
            dl.quality = manifest.get('quality')
            dl.state = DownloadState.QUEUED
            dl.resumable = True
            dl.referer = manifest.get('referer')
            dl.folder_id = actual_creation_id
            
            # Calculate assigned_parts_summary
            parts_indices = sorted([p['part'] for p in parts_for_task])
            ranges = []
            if parts_indices:
                start = parts_indices[0]
                end = parts_indices[0]
                for i in range(1, len(parts_indices)):
                    if parts_indices[i] == end + 1:
                        end = parts_indices[i]
                    else:
                        ranges.append(str(start) if start == end else f"{start}..{end}")
                        start = parts_indices[i]
                        end = parts_indices[i]
                ranges.append(str(start) if start == end else f"{start}..{end}")
            dl.assigned_parts_summary = ",".join(ranges)
            
            dl.segments = []
            for part_info in parts_for_task:
                seg = Segment(
                    start_byte=int(part_info['start']),
                    end_byte=int(part_info['end']),
                    downloaded_bytes=0,
                    part_number=part_info['part']
                )
                dl.segments.append(seg)
            
            # For byte-based multi-part tasks, we need total_size for proper seek/write
            if manifest_type == 'dlm.parts.v1':
                 dl.total_size = sum(p.get('size', 0) for p in parts_for_task)
            else:
                 dl.total_size = manifest.get('total_size', 0)
            
            # CRITICAL: Save to repository
            self.repository.save(dl)

            # Verify save
            saved = self.repository.get(dl.id)
            if not saved:
                raise RuntimeError(f"Internal Error: Failed to save task {dl.id} to database.")

            return dl.id

        # Create tasks
        created_ids = []
        if as_separate_tasks:
            for p in assigned_parts:
                part_num = p['part']
                part_name = f"part_{part_num:03d}"
                tid = create_download_from_parts([p], custom_name=part_name)
                created_ids.append(tid)
        else:
            tid = create_download_from_parts(assigned_parts)
            created_ids.append(tid)

        if not created_ids:
            raise RuntimeError("Import produced no tasks - this is a bug.")

        print(f"✅ Added {len(created_ids)} task(s) to folder '{folder_name}'")
        return created_ids[0]



    def _start_torrent_split_download(self, dl: Download):
        """Start split torrent download using SharedTorrentController."""
        try:
            from dlm.infra.network.shared_torrent import SharedTorrentController
            
            # 1. Get Controller
            controller = SharedTorrentController()
            
            # 2. Resolve Save Path (Workspace/Folder)
            # CRITICAL: All split tasks for the same torrent MUST use the SAME save_path
            # Otherwise libtorrent will create duplicate file structures
            
            # Get the workspace root (parent folder containing all tasks)
            from dlm.core.workspace import WorkspaceManager
            wm = WorkspaceManager(self.download_dir.parent)
            
            # Use the workspace root as save_path
            # This ensures all tasks write to the same torrent file structure
            workspace_root = self.download_dir.parent / "__workspace__"
            workspace_root.mkdir(parents=True, exist_ok=True)
            
            save_path = str(workspace_root)
            
            # 3. Get/Add Handle
            handle = controller.get_handle(dl.url, save_path)
            if not handle:
                dl.fail("Failed to get torrent handle")
                self._on_task_terminated(dl)
                return

            # 4. Wait for Metadata (if needed to map pieces)
            if not handle.status().has_metadata:
                for _ in range(30):
                    if handle.status().has_metadata: break
                    time.sleep(1)
            
            if not handle.status().has_metadata:
                 dl.fail("Metadata timeout")
                 self._on_task_terminated(dl)
                 return

            # 5. Map Segments to Pieces
            info = handle.get_torrent_info()
            piece_length = info.piece_length()
            total_pieces = info.num_pieces()
            
            my_pieces = set()
            for seg in dl.segments:
                start_p = int((seg.start_byte + getattr(dl, 'torrent_file_offset', 0)) // piece_length)
                end_p = int((seg.end_byte + getattr(dl, 'torrent_file_offset', 0)) // piece_length)
                end_p = min(end_p, total_pieces - 1)
                for p in range(start_p, end_p + 1):
                    my_pieces.add(p)
            
            # 6. Register Interest
            controller.register_interest(handle, dl.id, list(my_pieces))
            
            # 7. Start Monitoring
            with self._lock:
                dl.state = DownloadState.DOWNLOADING
                dl.current_stage = "split-downloading"
                dl.error_message = None
                self.repository.save(dl)
                self._active_downloads[dl.id] = dl
                
                cancel_event = threading.Event()
                self._cancel_events[dl.id] = cancel_event

            # Monitor Loop
            threading.Thread(
                target=self._monitor_shared_torrent,
                args=(dl, controller, handle, list(my_pieces), cancel_event),
                daemon=True
            ).start()
            
        except Exception as e:
            print(f"[SplitTorrent] Start Error: {e}")
            dl.fail(f"Split Start Failed: {e}")
            self._on_task_terminated(dl)

    def _monitor_shared_torrent(self, dl: Download, controller, handle, pieces: list, cancel_event: threading.Event):
        """Monitor progress of specific pieces."""
        try:
            # Setup segments folder for marker files
            folder = self._get_download_folder(dl)
            segments_dir = folder / "segments"
            segments_dir.mkdir(parents=True, exist_ok=True)
            
            # Track which segments we've marked as done
            marked_done = set()
            
            while not cancel_event.is_set():
                if dl.state != DownloadState.DOWNLOADING: break
                
                # Get Stats for OUR pieces only
                stats = controller.get_stats(handle, pieces)
                
                # Update Progress
                dl._manual_progress = stats['progress']
                
                # UPDATE: Reflect verified bytes in downloaded_bytes for TUI accuracy
                if stats.get('verified_bytes'):
                    v_bytes = stats['verified_bytes']
                    for seg in dl.segments:
                        seg_size = seg.end_byte - seg.start_byte + 1
                        seg.downloaded_bytes = min(v_bytes, seg_size)
                        v_bytes -= seg.downloaded_bytes
                
                if not dl.total_size and stats.get('total_bytes'):
                    dl.total_size = stats['total_bytes']

                dl.current_stage = f"downloading ({stats['peers']} peers)"
                dl.speed_bps = stats['speed'] 
                
                self.repository.save(dl)
                
                # Update marker files for each segment
                for i, seg in enumerate(dl.segments):
                    part_num = seg.part_number or (i + 1)
                    done_file = segments_dir / f"{part_num:03d}.done"
                    missing_file = segments_dir / f"{part_num:03d}.missing"
                    
                    # Check if this segment's pieces are all complete
                    # Map segment to pieces (same logic as in _start_torrent_split_download)
                    info = handle.get_torrent_info()
                    piece_length = info.piece_length()
                    
                    start_p = int((seg.start_byte + getattr(dl, 'torrent_file_offset', 0)) // piece_length)
                    end_p = int((seg.end_byte + getattr(dl, 'torrent_file_offset', 0)) // piece_length)
                    seg_pieces = list(range(start_p, end_p + 1))
                    
                    # Check if all pieces for this segment are done
                    all_done = all(controller.get_piece_status(handle, p) for p in seg_pieces)
                    
                    if all_done and part_num not in marked_done:
                        # Mark as done
                        if missing_file.exists():
                            missing_file.unlink()
                        done_file.touch()
                        marked_done.add(part_num)
                        # print(f"[Monitor] Marked segment {part_num} as DONE")
                    elif not all_done and not missing_file.exists() and not done_file.exists():
                        # Mark as missing
                        missing_file.touch()
                
                if stats['done'] >= stats['total']:
                    dl.complete()
                    self.repository.save(dl)
                    break
                
                # DEBUG: Print progress update
                # print(f"[Monitor] Task {dl.id[:8]} - Progress: {stats['progress']:.1f}%, Speed: {stats['speed']/1024:.1f} KB/s")
                
                time.sleep(5.0)  # Update every 5 seconds as requested
        except Exception as e:
            print(f"[Monitor] Error: {e}")
            import traceback
            traceback.print_exc()
            dl.fail(str(e))
        finally:
            # Deregister interest on stop/complete
            controller.deregister_interest(handle, dl.id)
            self._on_task_terminated(dl)

    def _import_v2(self, manifest, parts_filter, separate, creation_id, manifest_path):
        """Handle V2 workspace imports with context detection.
        
        Two modes:
        1. Segment import (inside workspace) - Updates data.part, marks segments done
        2. Task creation (outside workspace) - Creates tasks in current folder
        """
        from dlm.core.workspace import WorkspaceManager
        wm = WorkspaceManager(self.download_dir.parent)
        
        # Note: We rely on import_from_manifest validation. 
        # But V2 might be called recursively? No.
        
        # We assume if we are here, we are creating tasks in creation_id.
        return self._import_v2_tasks(manifest, parts_filter, separate, creation_id, manifest_path, wm)
    
    def _import_v2_segments(self, manifest, parts_filter, manifest_path, wm):
        """Import mode for segment upload inside workspace.
        
        Updates data.part with actual segment data and marks segments as done.
        Does NOT create tasks.
        """
        # TODO: Implement actual segment upload logic
        # For now, just mark segments as done
        task_name = Path(manifest_path).parent.name
        
        assigned_parts = manifest.get('assigned_parts', [])
        if parts_filter:
            assigned_parts = [p for p in assigned_parts if p['part'] in parts_filter]
        
        # Mark segments as done
        for part in assigned_parts:
            part_num = part['part']
            wm.mark_segment_done(task_name, part_num)
        
        print(f"[Segment Import] Marked {len(assigned_parts)} segments as done in workspace '{task_name}'")
        return None  # No task IDs created
    
    def _import_v2_tasks(self, manifest, parts_filter, separate, folder_id, manifest_path, wm):
        """Import mode for task creation outside workspace.
        
        Creates download tasks that reference workspace data.part.
        Tasks are added to folder_id (current user directory), NEVER inside workspace.
        """
        from dlm.core.entities import Download, Segment, DownloadState
        import uuid
        
        # CRITICAL: Prevent task creation inside workspace
        if folder_id is not None:
            folder = self.repository.get_folder(folder_id)
            if folder and folder['name'] == '__workspace__':
                raise ValueError(
                    "Cannot create tasks inside __workspace__. "
                    "This is an internal area. Navigate to a user folder first."
                )
        
        # Determine real physical workspace path using task_id from manifest
        task_id = manifest.get('task_id')
        if not task_id:
             raise ValueError("Manifest is missing 'task_id'.")
             
        task_path = wm.get_task_folder_by_id(task_id)
        if not task_path or not task_path.exists():
             # Last resort: try relative to manifest_path (backward compatibility)
             task_path = Path(manifest_path).parent
             # We don't strictly NEED task.manifest.json to exist if we have the data in 'manifest'
             
        # Use data DIRECTLY from the manifest we just loaded
        total_size = manifest.get('total_size', 0)
        original_filename = manifest.get('filename', 'unknown.bin')
        target_filename = original_filename
        
        # Get parts to import
        assigned_parts = manifest.get('assigned_parts', manifest.get('part_ranges', []))
        if not assigned_parts:
             raise ValueError("Manifest contains no valid parts for import.")

        # --- Intelligent Part Selection Logic ---
        segments_dir = task_path / "segments"
        if segments_dir.exists() and any(segments_dir.glob("*.done")):
            # Subsequent import: Filter out parts already marked as 'done'
            done_parts = {int(f.stem) for f in segments_dir.glob("*.done")}
            
            initial_count = len(assigned_parts)
            assigned_parts = [p for p in assigned_parts if p['part'] not in done_parts]
            
            if len(assigned_parts) < initial_count:
                print(f"[Import] Filtered out {initial_count - len(assigned_parts)} already completed parts.")
        
        # Apply user filter if provided
        if parts_filter:
            assigned_parts = [p for p in assigned_parts if p['part'] in parts_filter]
            if not assigned_parts:
                print("All selected parts are already completed.")
                return None
        
        # Prepare summary fields
        actual_folder_id = folder_id
        folder_name = "root"
        if folder_id is not None:
            folder = self.repository.get_folder(folder_id)
            if folder:
                folder_name = folder['name']

        if separate:
            # Create a virtual folder for this file
            target_folder_name = Path(original_filename).stem
            try:
                actual_folder_id = self.repository.create_folder(target_folder_name, folder_id)
                folder_name = target_folder_name
                print(f"[Import] Created folder '{target_folder_name}' at current location")
            except Exception:
                existing = self.repository.get_folder_by_name(target_folder_name, folder_id)
                if existing:
                    actual_folder_id = existing['id']
                    folder_name = target_folder_name
                else:
                    raise

        # Helper to create one task
        def create_v2_task(parts, custom_name=None):
            if not manifest.get('url'):
                raise ValueError("Manifest is missing 'url' - cannot create task.")
                
            dl = Download(url=manifest['url'])
            dl.id = str(uuid.uuid4())
            dl.target_filename = custom_name or target_filename
            
            # For separate parts, the total size of the task is the sum of its parts
            if len(parts) < len(assigned_parts) or custom_name:
                dl.total_size = sum(int(p.get('size', 0)) for p in parts)
            else:
                dl.total_size = total_size
                
            dl.task_id = manifest['task_id']
            dl.partial = True
            dl.folder_id = actual_folder_id
            dl.state = DownloadState.QUEUED
            dl.resumable = True
            
            # Import metadata
            dl.source = manifest.get('source')
            # [FIX] Auto-detect source during import if missing
            if not dl.source:
                 if dl.url.lower().endswith('.torrent') or dl.url.startswith('magnet:'):
                     dl.source = 'torrent'

            dl.media_type = manifest.get('media_type')
            dl.quality = manifest.get('quality')
            dl.referer = manifest.get('referer')
            dl.torrent_file_offset = manifest.get('torrent_file_offset', 0)
            
            # Segments
            dl.segments = []
            for p in parts:
                dl.segments.append(Segment(
                    start_byte=int(p['start']),
                    end_byte=int(p['end']),
                    part_number=p['part'],
                    downloaded_bytes=0
                ))
            
            # Summary
            parts_nums = sorted([p['part'] for p in parts])
            dl.assigned_parts_summary = ",".join(map(str, parts_nums))
            
            # CRITICAL: Save to repository
            self.repository.save(dl)
            
            # Verify save
            saved = self.repository.get(dl.id)
            if not saved:
                raise RuntimeError(f"Internal Error: Failed to save task {dl.id} to database.")
            
            # Create .missing files in workspace
            segments_dir = task_path / "segments"
            if segments_dir.exists():
                for num in parts_nums:
                    try:
                        (segments_dir / f"{num:03d}.missing").touch(exist_ok=True)
                    except Exception:
                        pass
            
            return dl.id

        # Create tasks
        created_ids = []
        if separate:
            for p in assigned_parts:
                part_num = p['part']
                part_name = f"part_{part_num:03d}"
                tid = create_v2_task([p], custom_name=part_name)
                created_ids.append(tid)
        else:
            tid = create_v2_task(assigned_parts)
            created_ids.append(tid)

        if not created_ids:
            raise RuntimeError("Import produced no tasks - this is a bug.")

        print(f"✅ Added {len(created_ids)} task(s) to folder '{folder_name}'")
        return created_ids[0]

    def create_split_workspace(self, download_id: str, parts: int, users: list, assignments: dict, workspace_name: str = None) -> Path:
        """
        Create a new split workspace for a download (v2).
        Does NOT create database tasks or segments.
        Generates manifest.json and user manifests within __workspace__ context.
        """
        dl = self.get_download(download_id)
        if not dl:
            raise ValueError("Download not found")
        
        if dl.source == 'torrent':
            raise ValueError("Torrent splitting is not supported.")
        
        # 1. Validation & Calculation
        if dl.source == 'youtube':
            if not dl.duration:
                 raise ValueError(f"Cannot split YouTube download without known duration (found: {dl.duration})")
            part_duration = dl.duration / parts
            split_type = "time"
        else:
            if not dl.total_size or dl.total_size <= 0:
                raise ValueError("Cannot split download: Unknown file size.")
            part_size = dl.total_size // parts
            split_type = "bytes"


        # 2. Init Workspace
        # Use filename as workspace folder name (human-readable)
        import uuid
        from pathlib import Path as PathLib
        task_uuid = str(uuid.uuid4())
        
        # Use passed name or extract from filename
        if not workspace_name:
            stem = PathLib(dl.target_filename).stem
            size_part = ""
            if dl.total_size and dl.total_size > 0:
                if dl.total_size >= 1024**3: size_part = f"_{dl.total_size/1024**3:.1f}G"
                elif dl.total_size >= 1024: size_part = f"_{dl.total_size/1024:.0f}K"
                else: size_part = f"_{dl.total_size}B"
            workspace_name = f"{stem}{size_part}"
        
        from dlm.core.workspace import WorkspaceManager
        wm = WorkspaceManager(self.download_dir.parent)
        
        # 3. Generate Manifest Data
        task_manifest = {
            "manifest_type": "youtube.split.v2" if dl.source == 'youtube' else "dlm.task.v2",
            "task_id": task_uuid,
            "original_download_id": str(dl.id),
            "url": dl.url,
            "filename": dl.target_filename,
            "parts": parts,
            "users": users,
            "torrent_file_offset": getattr(dl, 'torrent_file_offset', 0),
            "part_ranges": []
        }
        
        if dl.source == 'youtube':
            task_manifest["duration"] = dl.duration
        else:
            task_manifest["total_size"] = dl.total_size
            task_manifest["part_size"] = part_size

        for i in range(1, parts + 1):
            if dl.source == 'youtube':
                start = (i - 1) * part_duration
                end = i * part_duration if i < parts else dl.duration
                task_manifest["part_ranges"].append({
                    "part": i, "start": start, "end": end
                })
            else:
                start = (i - 1) * part_size
                end = start + part_size - 1 if i < parts else dl.total_size - 1
                task_manifest["part_ranges"].append({
                    "part": i, "start": start, "end": end, "size": end - start + 1
                })
        
        # 4. Create structure via Manager
        task_folder = wm.init_task_workspace(workspace_name, task_manifest)
        
        # --- Export Manifest to Downloads for visibility ---
        try:
            export_manifest_path = self.download_dir / f"{dl.target_filename}.manifest.json"
            with open(export_manifest_path, 'w', encoding='utf-8') as f:
                json.dump(task_manifest, f, indent=2)
            print(f"✅ Manifest exported to: {export_manifest_path}")
        except Exception as e:
            print(f"⚠️ Warning: Failed to export manifest to downloads folder: {e}")
        
        # --- Fix: Create DB Folder for Navigation ---
        # We need this so users can 'cd' into the task workspace.
        # Find __workspace__ parent ID
        ws_folder = self.repository.get_folder_by_name(WorkspaceManager.WORKSPACE_DIR_NAME, None)
        if ws_folder:
            ws_id = ws_folder['id']
        else:
            # Create if missing
            ws_id = self.repository.create_folder(WorkspaceManager.WORKSPACE_DIR_NAME, None)
            
        if ws_id:
             # Create task folder in DB (use actual folder name from workspace)
             # ENSURE it is parented under ws_id
             self.repository.create_folder(task_folder.name, ws_id)
        # --------------------------------------------
        
        # 5. Generate User Manifests (for distribution)
        # These are generated inside the workspace for export later
        for user_idx, part_list in assignments.items():
            user_manifest = {
                "manifest_type": "v2",
                "task_id": task_uuid,
                "user": users[user_idx - 1],
                "url": dl.url,
                "filename": dl.target_filename,
                "torrent_file_offset": getattr(dl, 'torrent_file_offset', 0),
                "assigned_parts": []
            }
            if dl.source != 'youtube':
                user_manifest['media_type'] = dl.media_type
                
            for part_num in sorted(part_list):
                part_range = task_manifest["part_ranges"][part_num - 1]
                user_manifest["assigned_parts"].append(part_range)
            
            user_manifest_path = task_folder / f"user_{user_idx}.manifest.json"
            with open(user_manifest_path, 'w') as f:
                json.dump(user_manifest, f, indent=2)
                
        return task_folder

    def shutdown_all(self):
        """Gracefully stop all active and waiting downloads for application exit."""
        all_dls = self.repository.get_all()
        
        with self._lock:
            active_ids = list(self._active_downloads.keys())
            discovery_ids = list(self._discovery_tasks)
        
        # 1. Handle Active Threads (Cancel them)
        for pid in active_ids:
            if pid in self._cancel_events:
                self._cancel_events[pid].set()
        
        # 2. Update States for persistence
        for dl in all_dls:
            changed = False
            # Active downloads go to PAUSED
            if dl.id in active_ids:
                if dl.state not in [DownloadState.COMPLETED, DownloadState.FAILED]:
                    dl.state = DownloadState.PAUSED
                    changed = True
            # Discovery tasks or Explicitly WAITING tasks go to QUEUED
            elif dl.id in discovery_ids or dl.state == DownloadState.WAITING or dl.state == DownloadState.INITIALIZING:
                dl.state = DownloadState.QUEUED
                changed = True
            
            if changed:
                try:
                    self.repository.save(dl)
                    self._save_metadata(dl)
                except Exception:
                    pass
        
        # 3. Wait for workers to finish
        self.executor.shutdown(wait=True)

    def trigger_renewal(self, download_id: str):
        """Trigger a browser-based renewal for a session-bound link."""
        dl = self.get_download(download_id)
        if not dl: return
        
        # 1. Pause the download if it's currently running
        self.pause_download(download_id)
        
        # 2. Get the source URL
        source_url = dl.source_url or dl.referer or dl.url
        
        # 3. Open Chromium (Visible) at source_url with overlay
        from dlm.app.commands import BrowserCommand
        # We assume the CommandBus is accessible or we use a more direct method.
        # In this architecture, service doesn't have the bus, but we can resolve it from bootstrap if needed.
        # However, we can just run the browser_command function directly in a thread.
        from dlm.app.browser_service import browser_command
        import asyncio

        def run_browser():
            asyncio.run(browser_command(target_url=source_url))
        
        thread = threading.Thread(target=run_browser, daemon=True)
        thread.start()
        
        print(f"[RENEW] Browser opened for {dl.target_filename} at {source_url}")

    def _parse_storage_state(self, state_json: str) -> tuple:
        """Parse Playwright storage state JSON into (cookies_dict, headers_dict)."""
        import json
        if not state_json:
            return {}, {}
        try:
            state = json.loads(state_json)
            cookies = {c['name']: c['value'] for c in state.get('cookies', [])}
            return cookies, {}
        except Exception as e:
            print(f"[Browser Session] Warning: Failed to parse storage state: {e}")
            return {}, {}
