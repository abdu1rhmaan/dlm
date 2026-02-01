import sqlite3
import json
import time
from typing import List, Optional
from pathlib import Path
from datetime import datetime as dt
from dlm.core.entities import Download, DownloadState, Segment, ResumeState, IntegrityState
from dlm.core.repositories import DownloadRepository
from dlm.core.interfaces import NetworkAdapter

class SqliteDownloadRepository(DownloadRepository):
    def __init__(self, db_path: Path):
        self.db_path = db_path.resolve()
        self._ensure_db_exists()

    def _ensure_db_exists(self):
        """Ensure database and table exist before any operation."""
        # Fix 1: Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Fix 2: Explicitly connect to absolute path
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            
            # Create table if not exists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS downloads (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    target_filename TEXT,
                    total_size INTEGER,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    error_message TEXT,
                    segments_json TEXT,
                    last_update TEXT,
                    speed_bps REAL,
                    resumable INTEGER,
                    resume_state TEXT,
                    max_connections INTEGER,
                    integrity_state TEXT,
                    partial INTEGER,
                    task_id TEXT,
                    assigned_parts_summary TEXT,
                    probed_via_stream INTEGER DEFAULT 0,
                    browser_probe_done INTEGER DEFAULT 0,
                    torrent_files_json TEXT
                )
            """)
            
            # NEW: folders table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    parent_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (parent_id) REFERENCES folders(id)
                )
            """)

            # NEW: browser_downloads table to record downloads captured from the browser
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS browser_downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    filename TEXT,
                    size INTEGER,
                    referrer TEXT,
                    storage_state TEXT,
                    timestamp TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    downloaded_bytes INTEGER DEFAULT 0,
                    progress REAL DEFAULT 0.0,
                    user_agent TEXT,
                    captured_method TEXT,
                    captured_headers_json TEXT,
                    captured_cookies_json TEXT,
                    source_url TEXT,
                    folder_id INTEGER,
                    FOREIGN KEY (folder_id) REFERENCES folders(id)
                )
            """)

            conn.commit()
            
            # Enable WAL mode for concurrency
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            conn.commit()
            
            # Check for missing columns and add them
            cursor.execute("PRAGMA table_info(downloads)")
            columns = [info[1] for info in cursor.fetchall()]

            cursor.execute("PRAGMA table_info(browser_downloads)")
            brw_columns = [info[1] for info in cursor.fetchall()]

            if "downloaded_bytes" not in brw_columns:
                cursor.execute("ALTER TABLE browser_downloads ADD COLUMN downloaded_bytes INTEGER DEFAULT 0")
                conn.commit()
            if "progress" not in brw_columns:
                cursor.execute("ALTER TABLE browser_downloads ADD COLUMN progress REAL DEFAULT 0.0")
                conn.commit()
            if "user_agent" not in brw_columns:
                cursor.execute("ALTER TABLE browser_downloads ADD COLUMN user_agent TEXT")
                conn.commit()
            if "captured_method" not in brw_columns:
                cursor.execute("ALTER TABLE browser_downloads ADD COLUMN captured_method TEXT")
                conn.commit()
            if "captured_headers_json" not in brw_columns:
                cursor.execute("ALTER TABLE browser_downloads ADD COLUMN captured_headers_json TEXT")
                conn.commit()
            if "captured_cookies_json" not in brw_columns:
                cursor.execute("ALTER TABLE browser_downloads ADD COLUMN captured_cookies_json TEXT")
                conn.commit()
            if "source_url" not in brw_columns:
                cursor.execute("ALTER TABLE browser_downloads ADD COLUMN source_url TEXT")
                conn.commit()
            
            if "last_update" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN last_update TEXT")
                conn.commit()
            
            if "speed_bps" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN speed_bps REAL")
                conn.commit()
            
            if "resumable" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN resumable INTEGER")
                conn.commit()
                
            if "resume_state" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN resume_state TEXT")
                conn.commit()

            if "max_connections" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN max_connections INTEGER")
                conn.commit()

            if "integrity_state" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN integrity_state TEXT")
                conn.commit()
            
            if "partial" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN partial INTEGER")
                conn.commit()
            
            if "task_id" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN task_id TEXT")
                conn.commit()

            if "assigned_parts_summary" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN assigned_parts_summary TEXT")
                conn.commit()

            if "source" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN source TEXT")
                conn.commit()

            if "media_type" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN media_type TEXT")
                conn.commit()

            if "cut_range" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN cut_range TEXT")
                conn.commit()

            if "conversion_required" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN conversion_required INTEGER DEFAULT 0")
                conn.commit()

            if "duration" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN duration REAL")
            if "audio_mode" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN audio_mode TEXT")
            if "vocals_gpu" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN vocals_gpu INTEGER DEFAULT 0")
            
            if "quality" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN quality TEXT")

            if "output_path" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN output_path TEXT")
            
            if "captured_headers_json" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN captured_headers_json TEXT")
            if "captured_cookies_json" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN captured_cookies_json TEXT")
            
            if "source_url" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN source_url TEXT")
            
            if "storage_state" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN storage_state TEXT")
            
            if "browser_capture_id" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN browser_capture_id INTEGER")
            
            if "user_agent" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN user_agent TEXT")
            
            if "probed_via_stream" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN probed_via_stream INTEGER DEFAULT 0")
            
            if "browser_probe_done" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN browser_probe_done INTEGER DEFAULT 0")
            
            if "torrent_files_json" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN torrent_files_json TEXT")
                conn.commit()
            
            if "folder_id" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN folder_id INTEGER")
                conn.commit()

            if "torrent_file_offset" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN torrent_file_offset INTEGER DEFAULT 0")
                conn.commit()

            if "manual_progress" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN manual_progress REAL")
                conn.commit()

            if "downloaded_bytes_override" not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN downloaded_bytes_override INTEGER")
                conn.commit()

            if "folder_id" not in brw_columns:
                cursor.execute("ALTER TABLE browser_downloads ADD COLUMN folder_id INTEGER")
                conn.commit()
            
            conn.commit()

        finally:
            conn.close()

    def _get_connection(self):
        return sqlite3.connect(str(self.db_path))

    def save(self, download: Download) -> None:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            segments_json = json.dumps([
                {
                    "start": s.start_byte, 
                    "end": s.end_byte, 
                    "downloaded": s.downloaded_bytes,
                    "checkpoint": s.last_checkpoint,
                    "start_hash": s.start_hash,
                    "end_hash": s.end_hash,
                    "part": s.part_number
                } for s in download.segments
            ])
            
            cursor.execute("""
                INSERT OR REPLACE INTO downloads (
                    id, url, target_filename, total_size, state, created_at, error_message, segments_json,
                    last_update, speed_bps, resumable, resume_state, max_connections, integrity_state, 
                    partial, task_id, assigned_parts_summary, source, media_type, cut_range, conversion_required, duration, audio_mode, vocals_gpu, quality, output_path,
                    captured_headers_json, captured_cookies_json, source_url, storage_state, browser_capture_id, user_agent, probed_via_stream, browser_probe_done, torrent_files_json,
                    folder_id, torrent_file_offset, manual_progress, downloaded_bytes_override
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                download.id,
                download.url,
                download.target_filename,
                download.total_size,
                download.state.name,
                download.created_at.isoformat(),
                download.error_message,
                segments_json,
                dt.now().isoformat(),
                getattr(download, 'speed_bps', 0.0),
                1 if getattr(download, 'resumable', True) else 0,
                download.resume_state.name if hasattr(download.resume_state, 'name') else str(download.resume_state),
                getattr(download, 'max_connections', 4),
                download.integrity_state.name if hasattr(download.integrity_state, 'name') else str(download.integrity_state),
                1 if download.partial else 0,
                getattr(download, 'task_id', None),
                getattr(download, 'assigned_parts_summary', None),
                download.source,
                download.media_type,
                download.cut_range,
                1 if download.conversion_required else 0,
                download.duration,
                download.audio_mode,
                download.vocals_gpu if isinstance(download.vocals_gpu, int) else (1 if download.vocals_gpu else 0),
                download.quality,
                download.output_path,
                json.dumps(getattr(download, 'captured_headers', {})),
                json.dumps(getattr(download, 'captured_cookies', {})),
                download.source_url,
                download.storage_state,
                download.browser_capture_id,
                download.user_agent,
                1 if download.probed_via_stream else 0,
                1 if download.browser_probe_done else 0,
                download.torrent_files_json if hasattr(download, 'torrent_files_json') else json.dumps(download.torrent_files),
                download.folder_id,
                download.torrent_file_offset,
                getattr(download, '_manual_progress', None),
                getattr(download, '_downloaded_bytes_override', None)
            ))
            
            # Sync to browser_downloads if linked
            if download.browser_capture_id:
                cursor.execute("""
                    UPDATE browser_downloads 
                    SET status = ?, downloaded_bytes = ?, progress = ?
                    WHERE id = ?
                """, (
                    download.state.name.lower(),
                    download.get_downloaded_bytes(),
                    download.progress,
                    download.browser_capture_id
                ))

            conn.commit()
        finally:
            conn.close()

    def get(self, download_id: str) -> Optional[Download]:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM downloads WHERE id = ?", (download_id,))
            row = cursor.fetchone()
            if not row:
                return None
            cols = [c[0] for c in cursor.description]
            return self._row_to_entity(row, cols)
        finally:
            conn.close()

    def get_all(self) -> List[Download]:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM downloads ORDER BY created_at ASC")
            rows = cursor.fetchall()
            if not rows: return []
            cols = [c[0] for c in cursor.description]
            return [self._row_to_entity(row, cols) for row in rows]
        finally:
            conn.close()

    def delete(self, download_id: str) -> None:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
            conn.commit()
        finally:
            conn.close()

    def get_browser_downloads(self) -> List[dict]:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM browser_downloads ORDER BY timestamp DESC")
            cols = [c[0] for c in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def add_browser_download(self, url: str, filename: str, size: int, referrer: str, storage_state: str, user_agent: str, 
                             method: str = "GET", headers_json: str = "{}", cookies_json: str = "[]", source_url: str = None) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO browser_downloads (url, filename, size, referrer, storage_state, timestamp, status, user_agent, 
                                               captured_method, captured_headers_json, captured_cookies_json, source_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (url, filename, size, referrer, storage_state, time.ctime(), 'pending', user_agent, method, headers_json, cookies_json, source_url))
            return cursor.lastrowid

    def get_browser_download(self, capture_id: int) -> Optional[dict]:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM browser_downloads WHERE id = ?", (capture_id,))
            row = cursor.fetchone()
            if not row:
                return None
            cols = [c[0] for c in cursor.description]
            return dict(zip(cols, row))
        finally:
            conn.close()

    def update_browser_download_size(self, capture_id: int, size: int) -> None:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE browser_downloads 
                SET size = ? 
                WHERE id = ?
            """, (size, capture_id))
            conn.commit()
        finally:
            conn.close()

    def create_folder(self, name: str, parent_id: Optional[int]) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO folders (name, parent_id, created_at) VALUES (?, ?, ?)",
                (name, parent_id, dt.now().isoformat())
            )
            return cursor.lastrowid

    def get_folder(self, folder_id: int) -> Optional[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM folders WHERE id = ?", (folder_id,))
            row = cursor.fetchone()
            if not row: return None
            cols = [c[0] for c in cursor.description]
            return dict(zip(cols, row))

    def get_folder_by_name(self, name: str, parent_id: Optional[int]) -> Optional[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if parent_id is None:
                cursor.execute("SELECT * FROM folders WHERE name = ? AND parent_id IS NULL", (name,))
            else:
                cursor.execute("SELECT * FROM folders WHERE name = ? AND parent_id = ?", (name, parent_id))
            row = cursor.fetchone()
            if not row: return None
            cols = [c[0] for c in cursor.description]
            return dict(zip(cols, row))

    def get_folders(self, parent_id: Optional[int]) -> List[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if parent_id is None:
                cursor.execute("SELECT * FROM folders WHERE parent_id IS NULL ORDER BY name ASC")
            else:
                cursor.execute("SELECT * FROM folders WHERE parent_id = ? ORDER BY name ASC", (parent_id,))
            cols = [c[0] for c in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def update_folder_parent(self, folder_id: int, new_parent_id: Optional[int]) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE folders SET parent_id = ? WHERE id = ?", (new_parent_id, folder_id))
            conn.commit()

    def delete_folder(self, folder_id: int) -> None:
        with self._get_connection() as conn:
            # Recursive deletion logic handled by app service usually, 
            # but here we just delete the entry. Foreign keys might restrict it.
            cursor = conn.cursor()
            cursor.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
            conn.commit()

    def delete_browser_download(self, id: int):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM browser_downloads WHERE id = ?", (id,))
            conn.commit()

    def get_folder_size(self, folder_id: int) -> int:
        """Calculate total size of all downloads in a folder recursively."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Recursive CTE to get all subfolder IDs including current
            cursor.execute("""
                WITH RECURSIVE subfolders(id) AS (
                    SELECT id FROM folders WHERE id = ?
                    UNION ALL
                    SELECT f.id FROM folders f
                    JOIN subfolders s ON f.parent_id = s.id
                )
                SELECT SUM(d.total_size)
                FROM downloads d
                WHERE d.folder_id IN (SELECT id FROM subfolders)
            """, (folder_id,))
            result = cursor.fetchone()[0]
            
            # Check for direct file size in current folder (if no subfolders logic used incorrectly)
            # The above query covers d.folder_id in list of ALL subfolders starting from root folder_id
            
            return result if result is not None else 0

    def get_all_by_folder(self, folder_id: Optional[int]) -> List[Download]:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            if folder_id is None:
                cursor.execute("SELECT * FROM downloads WHERE folder_id IS NULL ORDER BY created_at ASC")
            else:
                cursor.execute("SELECT * FROM downloads WHERE folder_id = ? ORDER BY created_at ASC", (folder_id,))
            rows = cursor.fetchall()
            if not rows: return []
            cols = [c[0] for c in cursor.description]
            return [self._row_to_entity(row, cols) for row in rows]
        finally:
            conn.close()

    def get_browser_downloads_by_folder(self, folder_id: Optional[int]) -> List[dict]:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            if folder_id is None:
                cursor.execute("SELECT * FROM browser_downloads WHERE folder_id IS NULL ORDER BY timestamp DESC")
            else:
                cursor.execute("SELECT * FROM browser_downloads WHERE folder_id = ? ORDER BY timestamp DESC", (folder_id,))
            cols = [c[0] for c in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def _row_to_entity(self, row, cols=None) -> Download:
        segments_data = json.loads(row[7]) if row[7] else []
        segments = [
            Segment(
                s["start"], 
                s["end"], 
                s["downloaded"],
                s.get("checkpoint", 0),
                s.get("start_hash"),
                s.get("end_hash"),
                s.get("part")
            )
            for s in segments_data
        ]
        
        last_update = dt.now()
        speed_bps = 0.0
        resumable = True
        resume_state = "STABLE"
        max_connections = 4
        integrity_state = "PENDING"
        partial = False
        task_id = None
        assigned_parts_summary = None
        source = None
        media_type = None
        cut_range = None
        conversion_required = False
        quality = None

        # Map by index since schema might evolve
        if cols is None:
            cursor = self._get_connection().cursor()
            cursor.execute("PRAGMA table_info(downloads)")
            cols = [c[1] for c in cursor.fetchall()]

        d = Download(url=row[cols.index("url")])
        d.id = row[cols.index("id")]
        d.target_filename = row[cols.index("target_filename")]
        d.total_size = row[cols.index("total_size")] or 0
        d.state = DownloadState[row[cols.index("state")]]
        d.created_at = dt.fromisoformat(row[cols.index("created_at")])
        d.error_message = row[cols.index("error_message")]
        d.segments = segments
        
        if "last_update" in cols: d.last_update = dt.fromisoformat(row[cols.index("last_update")]) if row[cols.index("last_update")] else dt.now()
        if "speed_bps" in cols: d.speed_bps = row[cols.index("speed_bps")] or 0.0
        if "resumable" in cols: d.resumable = bool(row[cols.index("resumable")])
        if "resume_state" in cols:
            val = row[cols.index("resume_state")]
            d.resume_state = ResumeState[val] if val else ResumeState.STABLE
        if "max_connections" in cols: d.max_connections = row[cols.index("max_connections")] or 4
        if "integrity_state" in cols:
            val = row[cols.index("integrity_state")]
            d.integrity_state = IntegrityState[val] if val else IntegrityState.PENDING
        if "partial" in cols: d.partial = bool(row[cols.index("partial")])
        if "task_id" in cols: d.task_id = row[cols.index("task_id")]
        if "assigned_parts_summary" in cols: d.assigned_parts_summary = row[cols.index("assigned_parts_summary")]
        if "source" in cols: d.source = row[cols.index("source")]
        if "media_type" in cols: d.media_type = row[cols.index("media_type")]
        if "cut_range" in cols: d.cut_range = row[cols.index("cut_range")]
        if "conversion_required" in cols: d.conversion_required = bool(row[cols.index("conversion_required")])
        if "duration" in cols: d.duration = row[cols.index("duration")]
        if "audio_mode" in cols: d.audio_mode = row[cols.index("audio_mode")]
        if "vocals_gpu" in cols: d.vocals_gpu = bool(row[cols.index("vocals_gpu")])
        if "quality" in cols: d.quality = row[cols.index("quality")]
        if "output_path" in cols: d.output_path = row[cols.index("output_path")]
        
        if "captured_headers_json" in cols:
            val = row[cols.index("captured_headers_json")]
            d.captured_headers = json.loads(val) if val else {}
        if "captured_cookies_json" in cols:
            val = row[cols.index("captured_cookies_json")]
            d.captured_cookies = json.loads(val) if val else {}
        
        if "source_url" in cols:
            d.source_url = row[cols.index("source_url")]
        
        if "browser_capture_id" in cols:
            d.browser_capture_id = row[cols.index("browser_capture_id")]
        
        if "user_agent" in cols:
            d.user_agent = row[cols.index("user_agent")]

        if "probed_via_stream" in cols:
            d.probed_via_stream = bool(row[cols.index("probed_via_stream")])

        if "browser_probe_done" in cols:
            d.browser_probe_done = bool(row[cols.index("browser_probe_done")])
        
        if "torrent_files_json" in cols:
            val = row[cols.index("torrent_files_json")]
            d.torrent_files = json.loads(val) if val else []

        if "folder_id" in cols:
            d.folder_id = row[cols.index("folder_id")]
        
        if "torrent_file_offset" in cols:
            d.torrent_file_offset = row[cols.index("torrent_file_offset")] or 0

        if "manual_progress" in cols:
            d._manual_progress = row[cols.index("manual_progress")]
        
        if "downloaded_bytes_override" in cols:
            d._downloaded_bytes_override = row[cols.index("downloaded_bytes_override")]

        return d
