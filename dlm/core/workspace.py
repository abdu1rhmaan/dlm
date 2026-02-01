import os
import json
import shutil
from pathlib import Path
from typing import Optional, Dict, Any

class WorkspaceManager:
    WORKSPACE_DIR_NAME = ".workspace"
    MANIFEST_FILENAME = "manifest.json"
    DATA_FILENAME = "data.part"
    SEGMENTS_DIR_NAME = "segments"

    def __init__(self, root_path: str):
        """Initialize WorkspaceManager.
        
        Args:
            root_path: Project root path (for download directory reference)
        """
        self.root_path = Path(root_path)
        self.workspace_root = self._get_workspace_data_dir()
    
    def _get_workspace_data_dir(self) -> Path:
        return self.root_path / self.WORKSPACE_DIR_NAME

    def ensure_workspace_root(self):
        """Ensure the __workspace__ root directory exists and is hidden."""
        if not self.workspace_root.exists():
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            
            # Hide on Windows
            if os.name == 'nt':
                try:
                    import ctypes
                    # FILE_ATTRIBUTE_HIDDEN = 2
                    ctypes.windll.kernel32.SetFileAttributesW(str(self.workspace_root), 2)
                except:
                    pass

    def init_task_workspace(self, task_name: str, manifest_data: dict) -> Path:
        """Initialize a new task workspace with human-readable folder name.
        
        Args:
            task_name: Human-readable name (e.g., "cod-wwii.iso")
            manifest_data: Must contain 'task_id' for internal mapping
            
        Returns:
            Path to created task workspace folder
        """
        self.ensure_workspace_root()
        
        # Sanitize folder name for filesystem
        from dlm.app.services import sanitize_filename
        safe_name = sanitize_filename(task_name)
        
        # Handle collisions by appending (2), (3), etc.
        task_dir = self.workspace_root / safe_name
        counter = 2
        while task_dir.exists():
            task_dir = self.workspace_root / f"{safe_name} ({counter})"
            counter += 1
        
        # Create directory structure
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / self.SEGMENTS_DIR_NAME).mkdir()
        
        # Add folder name to manifest for reverse lookup
        manifest_data['_folder_name'] = task_dir.name
        
        # Save manifest
        manifest_path = task_dir / self.MANIFEST_FILENAME
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest_data, f, indent=2)
            
        return task_dir

    def get_task_workspace(self, task_name: str) -> Optional[Path]:
        path = self.workspace_root / task_name
        if path.exists() and path.is_dir():
            return path
        return None

    def is_inside_workspace(self, path_str: str) -> bool:
        """Check if the given path is inside the workspace root."""
        try:
            p = Path(path_str).resolve()
            w = self.workspace_root.resolve()
            if w == p: return True
            return w in p.parents
        except Exception:
            return False

    def is_workspace_root(self, folder_name: str) -> bool:
        return folder_name == self.WORKSPACE_DIR_NAME

    def load_manifest(self, task_folder: Path) -> dict:
        manifest_path = task_folder / self.MANIFEST_FILENAME
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found in {task_folder}")
        
        with open(manifest_path, 'r') as f:
            return json.load(f)

    def get_data_part_path(self, task_folder: Path) -> Path:
        return task_folder / self.DATA_FILENAME

    def get_segments_dir(self, task_folder: Path) -> Path:
        return task_folder / self.SEGMENTS_DIR_NAME

    def validate_workspace_integrity(self, task_folder: Path) -> bool:
        """Check if basic workspace structure exists."""
        return (task_folder.exists() and 
                (task_folder / self.MANIFEST_FILENAME).exists() and
                (task_folder / self.SEGMENTS_DIR_NAME).exists())

    def export_to_files(self, task_folder: Path, parts: list) -> Path:
        """
        Export specific parts from data.part to separate files in 'exported' folder.
        """
        manifest = self.load_manifest(task_folder)
        data_part = self.get_data_part_path(task_folder)
        
        if not data_part.exists():
            raise FileNotFoundError("data.part missing in workspace.")
            
        export_dir = task_folder / "exported"
        export_dir.mkdir(exist_ok=True)
        
        # Check integrity of requested parts (optional, but good)
        segments_dir = self.get_segments_dir(task_folder)
        missing = []
        for p in parts:
            if not (segments_dir / f"{p:03d}.done").exists():
                # Allow export if .done missing? 
                # Maybe incomplete export is allowed?
                # Spec says "Verifies required segments".
                # Let's warn but proceed or fail?
                # Fail seems safer.
                missing.append(p)
        
        if missing:
             raise ValueError(f"Cannot export parts {missing}: Segments not marked as done.")

        part_ranges = {p['part']: p for p in manifest['part_ranges']}
        
        with open(data_part, 'rb') as f:
            for p in parts:
                if p not in part_ranges:
                    continue
                info = part_ranges[p]
                start = info['start']
                end = info['end']
                # Youtube time-based split logic complexity:
                # If time-based, data.part might NOT be contiguous bytes corresponding to parts!
                # If it's time-based split, how is data.part structured?
                # It's usually one big file download.
                # If source is youtube, data.part is the FULL video?
                # If so, exporting "part 1" means cutting it? ffmpeg?
                # Or is `data.part` built by concatenated segments?
                # If source=youtube, we don't have byte ranges easily unless we calculated them.
                # Wait, my create_split logic for YT used `duration`.
                # But `import` creates `Download` tasks.
                # If I import YT range, `yt-dlp` download it.
                # `yt-dlp` download specific time range? `download_ranges` callback?
                # If so, `data.part` contains ... what?
                # If multiple tasks write to `data.part`, and they are time-ranges...
                # `yt-dlp` usually writes its own file.
                # We forced it to write to `data.part`?
                # For YT, concurrency on single file is hard.
                # Maybe v2 split for YT uses separate files and merges?
                # Spec says: "data.part # the real underlying file".
                # For YT, this implies we might need ffmpeg to split/merge if it's one file.
                # BUT if we downloaded PARTS, we downloaded BYTES (if format is simple) or SEPARATE FILES.
                # If `import` creates tasks, and tasks use `yt-dlp` with `download_ranges`.
                # `yt-dlp` appends to file?
                # This is complex.
                # Let's assume Byte-based for now (files).
                # If time-based, just assume bytes map 1:1? No.
                # If YT, usually we download SEPARATE tracks and merge.
                # But v2 says "Work on shared data.part".
                # Maybe for YT, we just export the whole thing or fail?
                # Let's implement byte-based extraction first.
                
                size = int(end - start + 1)
                f.seek(int(start))
                
                out_name = f"part_{p:03d}.mp4" # Extension? guess
                # extension from manifest filename?
                ext = Path(manifest['filename']).suffix or ".bin"
                out_name = f"part_{p:03d}{ext}"
                
                with open(export_dir / out_name, 'wb') as out:
                    # Copy chunk
                    # Use buffer
                    remaining = size
                    while remaining > 0:
                        chunk_size = min(64*1024, remaining)
                        data = f.read(chunk_size)
                        if not data: break
                        out.write(data)
                        remaining -= len(data)
                        
        return export_dir

    def finalize_workspace(self, task_folder: Path, output_dir: Path = None) -> Path:
        """
        Verify all parts, rename data.part to final name, move to output_dir (or parent), delete workspace.
        """
        manifest = self.load_manifest(task_folder)
        data_part = self.get_data_part_path(task_folder)
        segments_dir = self.get_segments_dir(task_folder)
        
        if not data_part.exists():
            raise FileNotFoundError("data.part missing.")

        # Verify all parts
        parts_count = manifest['parts']
        missing = []
        for i in range(1, parts_count + 1):
            if not (segments_dir / f"{i:03d}.done").exists():
                missing.append(i)
        
        if missing:
            raise ValueError(f"Cannot finalize: Parts {missing} contain missing segments.")
            
        # Finalize
        final_name = manifest['filename']
        if not output_dir:
            output_dir = self.root_path / "downloads"
            
        target_path = output_dir / final_name
        
        # Move file
        if target_path.exists():
            raise FileExistsError(f"Target file '{target_path}' already exists.")
            
        shutil.move(str(data_part), str(target_path))
        
        # Cleanup workspace (entire task folder)
        shutil.rmtree(task_folder)
        
        return target_path
    
    def get_task_folder_by_id(self, task_id: str) -> Optional[Path]:
        """Find workspace folder by task_id.
        
        Args:
            task_id: Internal task UUID
            
        Returns:
            Path to task workspace folder, or None if not found
        """
        if not self.workspace_root.exists():
            return None
        
        for task_folder in self.workspace_root.iterdir():
            if not task_folder.is_dir():
                continue
            
            manifest_path = task_folder / self.MANIFEST_FILENAME
            if manifest_path.exists():
                try:
                    with open(manifest_path, 'r', encoding='utf-8') as f:
                        manifest = json.load(f)
                    
                    if manifest.get('task_id') == task_id:
                        return task_folder
                except Exception:
                    continue
        
        return None
    
    def get_task_id_by_folder(self, folder_name: str) -> Optional[str]:
        """Get task_id from folder name.
        
        Args:
            folder_name: Folder name (e.g., "cod-wwii.iso")
            
        Returns:
            Task ID string, or None if not found
        """
        manifest_path = self.workspace_root / folder_name / self.MANIFEST_FILENAME
        if manifest_path.exists():
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    manifest = json.load(f)
                return manifest.get('task_id')
            except Exception:
                pass
        
        return None
