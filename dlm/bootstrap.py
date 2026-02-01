from pathlib import Path
from dlm.infra.persistence.sqlite import SqliteDownloadRepository
from dlm.infra.network.http import HttpNetworkAdapter
from dlm.app.media_service import MediaService
from dlm.app.services import DownloadService
from dlm.app.media_service import MediaService
from dlm.core.workspace import WorkspaceManager
import json
import os
from typing import Optional, Dict, Any

from dlm.app.commands import CommandBus, AddDownload, ListDownloads, StartDownload, PauseDownload, ResumeDownload, RemoveDownload, RetryDownload, SplitDownload, ImportDownload, VocalsCommand, BrowserCommand, PromoteBrowserDownload, RecaptureDownload, CreateFolder, MoveTask, DeleteFolder, RemoveBrowserDownload, RegisterExternalTask, UpdateExternalTask
from dlm.core.entities import DownloadState, Download

def get_project_root() -> Path:
    """Get the project root directory (where dlm package is located)."""
    # This file is dlm/bootstrap.py
    # Parent is dlm/
    # Parent.Parent is the root (Desktop/dlm)
    return Path(__file__).resolve().parent.parent

# Global index mapping (queue index -> UUID)
_index_to_uuid = {}
_browser_index_to_id = {}
_resolving_browser_ids = set()

def _rebuild_index_mapping(repo, brw=False, folder_id=None, include_workspace=False):
    """Rebuild the queue index to UUID/ID mapping."""
    global _index_to_uuid, _browser_index_to_id
    if brw:
        items = repo.get_browser_downloads_by_folder(folder_id)
        _browser_index_to_id = {i + 1: item['id'] for i, item in enumerate(items)}
    else:
        folders = repo.get_folders(folder_id)
        downloads = repo.get_all_by_folder(folder_id)
        
        _index_to_uuid = {}
        idx = 1
        
        # Handle Workspace Indexing (0 if included and at root)
        if include_workspace and folder_id is None:
            workspace_folder = repo.get_folder_by_name(WorkspaceManager.WORKSPACE_DIR_NAME, None)
            if workspace_folder:
                _index_to_uuid[0] = f"folder:{workspace_folder['id']}"

        for f in folders:
            if f['name'] == WorkspaceManager.WORKSPACE_DIR_NAME: continue # Skip if already at index 0 or filtered
            _index_to_uuid[idx] = f"folder:{f['id']}"
            idx += 1
        for d in downloads:
            _index_to_uuid[idx] = d.id
            idx += 1

def get_uuid_by_index(index: int, brw=False) -> str:
    """Get UUID by queue index."""
    if brw:
        if index in _browser_index_to_id:
            return str(_browser_index_to_id[index])
    else:
        if index in _index_to_uuid:
            return _index_to_uuid[index]
    raise ValueError(f"No download at index {index}")

def create_container() -> dict:
    # 1. Config - Use project root directory
    project_root = get_project_root()
    db_path = project_root / "dlm.db"
    dl_dir = project_root / "downloads"

    # Init Config (Secure)
    from dlm.core.config import SecureConfigRepository
    config_repo = SecureConfigRepository(project_root)

    # 2. Infra
    repo = SqliteDownloadRepository(db_path)
    http_network = HttpNetworkAdapter()
    
    from dlm.infra.network.torrent import TorrentNetworkAdapter
    torrent_network = TorrentNetworkAdapter()
    
    media_service = MediaService(config_repo=config_repo)
    service = DownloadService(repo, http_network, dl_dir, media_service=media_service, config_repo=config_repo)
    service.torrent_network = torrent_network # Inject torrent network

    # Initial index rebuild
    _rebuild_index_mapping(repo)

    # 4. Handlers
    def handle_add_download(cmd: AddDownload):
        result = service.add_download(
            cmd.url,
            source=cmd.source,
            media_type=cmd.media_type,
            quality=cmd.quality,
            cut_range=cmd.cut_range,
            conversion_required=cmd.conversion_required,
            title=cmd.title,
            duration=cmd.duration,
            audio_mode=cmd.audio_mode,
            vocals_gpu=cmd.vocals_gpu,
            vocals_keep_all=cmd.vocals_keep_all,
            referer=cmd.referer,
            torrent_files=cmd.torrent_files,
            torrent_file_offset=cmd.torrent_file_offset,
            total_size=cmd.total_size,
            folder_id=cmd.folder_id
        )
        _rebuild_index_mapping(repo, folder_id=cmd.folder_id)
        return result

    # ... list ...

    # ... start ...
    
    # ...
    


    project_root = get_project_root()
    
    def _list_workspace_tasks(workspace_folder_id):
        """Custom view for workspace root directory."""
        from dlm.core.workspace import WorkspaceManager
        wm = WorkspaceManager(project_root)
        
        results = []
        workspace_path = wm.workspace_root
        
        if not workspace_path.exists():
            return results
        
        # Get all subfolders in DB
        db_folders = {f['name']: f['id'] for f in repo.get_folders(workspace_folder_id)}
        
        display_idx = 1
        # List task folders
        for task_folder in sorted(workspace_path.iterdir()):
            if not task_folder.is_dir():
                continue
            
            # Load manifest to get task details
            manifest_path = task_folder / "manifest.json"
            if manifest_path.exists():
                try:
                    with open(manifest_path, 'r', encoding='utf-8') as f:
                        manifest = json.load(f)
                    
                    # Calculate completion status
                    segments_dir = task_folder / "segments"
                    done_count = 0
                    total_count = manifest.get('parts', 0)
                    if segments_dir.exists():
                        done_count = len(list(segments_dir.glob("*.done")))
                    
                    folder_id = db_folders.get(task_folder.name)
                    if not folder_id:
                        # [FIX] Auto-heal: Create missing DB folder for valid workspace task
                        # This ensures the folder is addressable by ID for commands like 'rm' and 'cd'
                        try:
                            folder_id = repo.create_folder(task_folder.name, workspace_folder_id)
                            db_folders[task_folder.name] = folder_id
                        except Exception:
                            pass # If creation fails, we still list it but it might be read-only

                    results.append({
                        'index': display_idx,
                        'id': f"{folder_id}" if folder_id else f"ws:{task_folder.name}",
                        'filename': task_folder.name,
                        'is_folder': True,
                        'state': "WS",  # Workspace tag
                        'progress': f"{done_count}/{total_count}" if total_count else "-",
                        'source': "internal",
                        'size': manifest.get('total_size', 0)
                    })
                    if folder_id:
                        _index_to_uuid[display_idx] = f"folder:{folder_id}"
                    
                    display_idx += 1
                except Exception:
                    continue
        
        return results

    def _list_task_workspace_contents(task_folder_path: Path, folder_id: int):
        """Custom view for inside a task workspace folder."""
        results = []
        
        # Load manifest to get task_id
        from dlm.core.workspace import WorkspaceManager
        wm = WorkspaceManager(project_root)
        try:
            manifest = wm.load_manifest(task_folder_path)
            task_uuid = manifest.get('task_id')
        except Exception:
            task_uuid = None

        # 1. Physical: data.part
        idx = 1
        data_part = task_folder_path / "data.part"
        if data_part.exists():
            size = data_part.stat().st_size
            
            # Check segments status
            segments_dir = task_folder_path / "segments"
            total_parts = 0
            done_count = 0
            if segments_dir.exists():
                done_count = len(list(segments_dir.glob("*.done")))
                missing_count = len(list(segments_dir.glob("*.missing")))
                # Also counting .part files as potential active/missing parts? 
                # Actually v2 structure relies on .done and .missing markers mostly.
                # If manifest available, use that for total.
                if manifest and 'assigned_parts' in manifest:
                    total_parts = len(manifest['assigned_parts'])
                else:
                    total_parts = done_count + missing_count
            
            is_complete = (total_parts > 0 and done_count >= total_parts)
            
            results.append({
                'index': idx,
                'id': f"file:{data_part.name}",
                'filename': 'data.part',
                'is_folder': False,
                'state': 'COMPLETED' if is_complete else 'WRS', 
                'progress': '100%' if is_complete else 'writing',
                'source': 'internal',
                'size': size
            })
            _index_to_uuid[idx] = f"ws_file:{data_part.name}"
            idx += 1
        
        # 2. Logic: Database Tasks linked to this workspace
        # [REMOVED] User requested strictly NO tasks shown in workspace.
        # Tasks are shown in their actual folders (Root or User Folders).

        # 3. Physical: segments/
        db_folders = {f['name']: f['id'] for f in repo.get_folders(folder_id)}
        segments_dir = task_folder_path / "segments"
        if segments_dir.exists():
            done_count = len(list(segments_dir.glob("*.done")))
            missing_count = len(list(segments_dir.glob("*.missing")))
            total = done_count + missing_count
            
            seg_folder_id = db_folders.get('segments')
            if not seg_folder_id:
                seg_folder_id = repo.create_folder('segments', folder_id)

            results.append({
                'index': idx,
                'id': f"{seg_folder_id}",
                'filename': 'segments',
                'is_folder': True,
                'state': 'SEG',
                'progress': f"{done_count} done",
                'source': 'internal',
                'size': total
            })
            _index_to_uuid[idx] = f"folder:{seg_folder_id}"
            idx += 1
        
        # 4. Physical: exported/
        exported_dir = task_folder_path / "exported"
        if exported_dir.exists():
            item_count = len(list(exported_dir.iterdir()))
            
            exp_folder_id = db_folders.get('exported')
            if not exp_folder_id:
                exp_folder_id = repo.create_folder('exported', folder_id)

            results.append({
                'index': idx,
                'id': f"{exp_folder_id}",
                'filename': 'exported',
                'is_folder': True,
                'state': 'EXP',
                'progress': f"{item_count} items",
                'source': 'internal',
                'size': 0
            })
            _index_to_uuid[idx] = f"folder:{exp_folder_id}"
            idx += 1
        
        return results

    def _list_segments_folder(segments_path: Path):
        """List segments with status indicators."""
        results = []
        
        # Collect all segment markers
        done_files = {int(f.stem): 'done' for f in segments_path.glob("*.done")}
        missing_files = {int(f.stem): 'missing' for f in segments_path.glob("*.missing")}
        
        # Combine and sort
        all_segments = set(done_files.keys()) | set(missing_files.keys())
        
        for idx, seg_num in enumerate(sorted(all_segments), start=1):
            status = done_files.get(seg_num, missing_files.get(seg_num, 'unknown'))
            
            results.append({
                'index': idx,
                'id': f"seg:{seg_num}",
                'filename': f"part_{seg_num:03d}",
                'is_folder': False,
                'state': 'DONE' if status == 'done' else 'MISS',
                'progress': status,
                'source': 'internal',
                'size': 0
            })
            _index_to_uuid[idx] = f"seg:{seg_num}"
        
        return results

    def _get_workspace_depth(repo, folder_id):
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
            folder = repo.get_folder(curr_id)
            if not folder: break
            if folder['name'] == WorkspaceManager.WORKSPACE_DIR_NAME:
                return depth
            curr_id = folder['parent_id']
            depth += 1
        return None

    def handle_list_downloads(cmd: ListDownloads):
        global _index_to_uuid, _browser_index_to_id
        # Rebuild index mapping to ensure correctness before listing
        _rebuild_index_mapping(repo, brw=cmd.brw, folder_id=cmd.folder_id, include_workspace=cmd.include_workspace)
        
        # Check for Workspace Context
        if not cmd.brw and cmd.folder_id is not None:
            depth = _get_workspace_depth(repo, cmd.folder_id)
            if depth is not None:
                if depth == 0:
                    return _list_workspace_tasks(cmd.folder_id)
                elif depth == 1:
                    # Inside a task workspace folder
                    folder = repo.get_folder(cmd.folder_id)
                    wm = WorkspaceManager(project_root)
                    # We need the filesystem path of the task folder
                    # The folder name in DB matches the folder name in AppData
                    task_folder_path = wm.workspace_root / folder['name']
                    return _list_task_workspace_contents(task_folder_path, cmd.folder_id)
                elif depth == 2:
                    # Inside segments/ or exported/
                    folder = repo.get_folder(cmd.folder_id)
                    parent = repo.get_folder(folder['parent_id'])
                    wm = WorkspaceManager(project_root)
                    task_folder_path = wm.workspace_root / parent['name']
                    
                    if folder['name'] == 'segments':
                        return _list_segments_folder(task_folder_path / 'segments')
                    # Could add exported/ view here if needed
        
        if cmd.brw:
            # Fetch from browser_downloads
            if hasattr(repo, 'get_browser_downloads_by_folder'):
                items = repo.get_browser_downloads_by_folder(cmd.folder_id)
                
                # Identify which browser items are already imported
                all_dls = repo.get_all()
                imported_ids = {dl.browser_capture_id for dl in all_dls if dl.browser_capture_id}
                
                # ... same silent size resolution ...
                # (re-indexing browser downloads for this view)
                _browser_index_to_id = {i + 1: item['id'] for i, item in enumerate(items)}

                return [
                    {
                        "index": i + 1,
                        "id": str(item['id']),
                        "filename": item['filename'] or "N/A",
                        "state": "QUEUED",
                        "progress": "imported" if item['id'] in imported_ids else "captured",
                        "source": "browser",
                        "url": item['url'],
                        "size": item['size'],
                        "timestamp": item['timestamp']
                    }
                    for i, item in enumerate(items)
                ]
            return []

        # Normal list: Folders first, then tasks
        if cmd.recursive:
            folders = []
            downloads = repo.get_all()
        else:
            folders = repo.get_folders(cmd.folder_id)
            downloads = repo.get_all_by_folder(cmd.folder_id)
        
        results = []
        idx = 1
        workspace_folder = None
        
        # Pre-filter workspace folder
        filtered_folders = []
        for f in folders:
            if f['name'] == WorkspaceManager.WORKSPACE_DIR_NAME:
                workspace_folder = f
            else:
                filtered_folders.append(f)
        
        folders = filtered_folders

        # Handle Workspace Indexing (0 if included)
        if cmd.include_workspace and workspace_folder:
            f_size = 0 # Workspace size might be huge, skipping calc for now or implementing specialized logic
            if hasattr(repo, 'get_folder_size'):
                 f_size = repo.get_folder_size(workspace_folder['id'])

            results.append({
                "index": 0,
                "id": str(workspace_folder['id']),
                "filename": workspace_folder['name'],
                "is_folder": True,
                "state": "WORKSPACE",
                "progress": "-",
                "source": "internal",
                "size": f_size
            })
            _index_to_uuid[0] = f"folder:{workspace_folder['id']}"

        for f in folders:
            # Calculate folder size
            f_size = 0
            if hasattr(repo, 'get_folder_size'):
                f_size = repo.get_folder_size(f['id'])
                
            results.append({
                "index": idx,
                "id": str(f['id']),
                "filename": f['name'],
                "is_folder": True,
                "state": "FOLDER",
                "progress": "-",
                "source": "internal",
                "size": f_size
            })
            # Mapping folders to their IDs in index mapping (using f_ prefix to distinguish)
            _index_to_uuid[idx] = f"folder:{f['id']}"
            idx += 1
            
        for d in downloads:
            results.append({
                "index": idx,
                "id": d.id,
                "filename": d.target_filename or "N/A",
                "is_folder": False,
                "state": d.state.name,
                "progress": "100.0%" if d.state == DownloadState.COMPLETED else (
                    f"{d.progress:.1f}%" if d.state in [DownloadState.DOWNLOADING, DownloadState.PAUSED, DownloadState.INITIALIZING] 
                    else (d.current_stage if (d.source == 'youtube' and d.current_stage) else d.state.name.lower())
                ),
                "source": d.source,
                "media_type": d.media_type,
                "audio_mode": d.audio_mode,
                "cut_range": d.cut_range,
                "conversion_required": d.conversion_required,
                "current_stage": getattr(d, 'current_stage', None),
                "duration": d.duration,
                "downloaded": d.get_downloaded_bytes(),
                "total": d.total_size or 0,
                "speed": getattr(d, 'speed_bps', 0.0),
                "error": d.error_message,
                "segments": [{"start": s.start_byte, "end": s.end_byte, "downloaded": s.downloaded_bytes} for s in d.segments] if d.segments else [],
                "in_scope": any(d.id == qid for qid in service._batch_queue)
            })
            _index_to_uuid[idx] = d.id
            idx += 1
            
        return results
    
    def handle_start_download(cmd: StartDownload):
        if not cmd.id:
            service.start_folder(cmd.folder_id, recursive=cmd.recursive, brw=cmd.brw)
        else:
            service.start_download(cmd.id, brw=cmd.brw)
    
    def handle_promote_browser(cmd: PromoteBrowserDownload):
        return service.promote_browser_capture(cmd.capture_id, folder_id=cmd.folder_id)

    def handle_pause_download(cmd: PauseDownload):
        service.pause_download(cmd.id)

    def handle_resume_download(cmd: ResumeDownload):
        service.resume_download(cmd.id)

    def handle_remove_download(cmd: RemoveDownload):
        download = repo.get(cmd.id)
        f_id = download.folder_id if download else None
        service.remove_download(cmd.id)
        # Ensure mapping is cleared immediately
        _rebuild_index_mapping(repo, folder_id=f_id)

    def handle_remove_browser_download(cmd: RemoveBrowserDownload):
        service.remove_browser_download(cmd.id)
        # Clear browser mapping? or just rebuild
        _rebuild_index_mapping(repo, brw=True)

    def handle_retry_download(cmd: RetryDownload):
        download = repo.get(cmd.id)
        f_id = download.folder_id if download else None
        service.retry_download(cmd.id)
        # Rebuild index mapping in case something shifted, though retry doesn't change IDs
        _rebuild_index_mapping(repo, folder_id=f_id)

    def handle_split_download(cmd: SplitDownload):
        return service.create_split_workspace(cmd.id, cmd.parts, cmd.users, cmd.assignments, cmd.workspace_name)

    def handle_import_download(cmd: ImportDownload):
        result = service.import_from_manifest(
            cmd.manifest_path, 
            parts_filter=cmd.parts, 
            as_separate_tasks=cmd.separate,
            folder_id=cmd.folder_id,
            target_id=cmd.target_id
        )
        _rebuild_index_mapping(repo, folder_id=cmd.folder_id)
        if cmd.target_id != cmd.folder_id:
            _rebuild_index_mapping(repo, folder_id=cmd.target_id)
        return result

    def handle_vocals_command(cmd: VocalsCommand):
        # Direct tool mode: does not use the queue
        return service.separate_vocals(Path(cmd.path) if cmd.path else None, use_gpu=cmd.use_gpu)

    def handle_browser_command(cmd: BrowserCommand):
        from dlm.app.browser_service import browser_command
        import asyncio
        return asyncio.run(browser_command(target_url=cmd.target_url))

    def handle_recapture_download(cmd: RecaptureDownload):
        # Implementation in service to open browser for a specific download
        service.trigger_renewal(cmd.id)

    def handle_create_folder(cmd: CreateFolder):
        return repo.create_folder(cmd.name, cmd.parent_id)
    
    def handle_move_task(cmd: MoveTask):
        if cmd.is_folder:
            return repo.update_folder_parent(int(cmd.source_id), cmd.target_folder_id)
        else:
            # Check active downloads first to avoid monitor thread overwrite
            if cmd.source_id in service._active_downloads:
                 target = service._active_downloads[cmd.source_id]
                 target.folder_id = cmd.target_folder_id
                 repo.save(target)
                 return True

            download = repo.get(cmd.source_id)
            if download:
                download.folder_id = cmd.target_folder_id
                repo.save(download)
                return True
            return False

    def handle_delete_folder(cmd: DeleteFolder):
        if not cmd.force:
            # Check if empty
            has_tasks = len(repo.get_all_by_folder(cmd.folder_id)) > 0
            has_subfolders = len(repo.get_folders(cmd.folder_id)) > 0
            if has_tasks or has_subfolders:
                raise ValueError("Folder not empty. Use --force to delete recursively.")
        
        # Recursive deletion logic in service would be better, but let's do a simple one here if force
        #Recursive deletion logic in service would be better, but let's do a simple one here if force
        if cmd.force:
            service.delete_folder_recursively(cmd.folder_id)
        else:
            repo.delete_folder(cmd.folder_id)

    def handle_register_external_task(cmd: RegisterExternalTask):
        import uuid
        import datetime
        
        # Determine state
        state_enum = DownloadState.DOWNLOADING
        if cmd.state == "INITIALIZING": state_enum = DownloadState.INITIALIZING
        elif cmd.state == "WAITING": state_enum = DownloadState.WAITING

        d = Download(
            url="external://transfer", 
            id=str(uuid.uuid4()),
            target_filename=cmd.filename, 
            total_size=cmd.total_size, 
            state=state_enum,
            source=cmd.source
        )
        repo.save(d)
        _rebuild_index_mapping(repo)
        return d.id

    def handle_update_external_task(cmd: UpdateExternalTask):
        d = repo.get(cmd.id)
        if d:
            # Update basic stats
            d._downloaded_bytes_override = cmd.downloaded_bytes
            d.speed_bps = cmd.speed
            if cmd.state:
                if cmd.state == "COMPLETED": d.state = DownloadState.COMPLETED
                elif cmd.state == "FAILED": d.state = DownloadState.FAILED
                # ... others as needed
            repo.save(d)


    # 5. Bus
    bus = CommandBus()
    bus.register(RegisterExternalTask, handle_register_external_task)
    bus.register(UpdateExternalTask, handle_update_external_task)
    bus.register(AddDownload, handle_add_download)
    bus.register(ListDownloads, handle_list_downloads)
    bus.register(StartDownload, handle_start_download)
    bus.register(PauseDownload, handle_pause_download)
    bus.register(ResumeDownload, handle_resume_download)
    bus.register(RemoveDownload, handle_remove_download)
    bus.register(RemoveBrowserDownload, handle_remove_browser_download)
    bus.register(RetryDownload, handle_retry_download)
    bus.register(SplitDownload, handle_split_download)
    bus.register(ImportDownload, handle_import_download)
    bus.register(VocalsCommand, handle_vocals_command)
    bus.register(BrowserCommand, handle_browser_command)
    bus.register(PromoteBrowserDownload, handle_promote_browser)
    bus.register(RecaptureDownload, handle_recapture_download)
    bus.register(CreateFolder, handle_create_folder)
    bus.register(MoveTask, handle_move_task)
    bus.register(DeleteFolder, handle_delete_folder)

    return {
        "bus": bus,
        "service": service,
        "media_service": media_service,
        "get_uuid_by_index": get_uuid_by_index
    }
