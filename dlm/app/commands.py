from dataclasses import dataclass
from typing import Protocol, Type, Dict, Any, TypeVar, Optional, List

# --- Commands ---
@dataclass
class Command:
    pass

@dataclass
class AddDownload(Command):
    url: str
    source: str = None
    media_type: str = None
    quality: str = None
    cut_range: str = None
    conversion_required: bool = False
    title: str = None
    duration: float = None
    audio_mode: str = None
    vocals_gpu: bool = False
    vocals_keep_all: bool = False
    output_template: str = None
    rename_template: str = None
    referer: str = None
    torrent_files: list = None
    torrent_file_offset: int = 0
    total_size: int = 0
    folder_id: Optional[int] = None
    ephemeral: bool = False

@dataclass
class ListDownloads(Command):
    brw: bool = False
    folder_id: Optional[int] = None
    recursive: bool = False
    include_workspace: bool = False
    include_ephemeral: bool = False

@dataclass
class StartDownload(Command):
    id: str
    brw: bool = False
    folder_id: Optional[int] = None
    recursive: bool = False

@dataclass
class PauseDownload(Command):
    id: str

@dataclass
class ResumeDownload(Command):
    id: str

@dataclass
class RemoveDownload(Command):
    id: str

@dataclass
class ExitApp(Command):
    pass

@dataclass
class RetryDownload(Command):
    id: str

@dataclass
class SplitDownload(Command):
    id: str
    parts: int
    users: list  # List of user names/IDs
    assignments: dict  # {user_index: [part_numbers]}
    workspace_name: Optional[str] = None

@dataclass
class ImportDownload(Command):
    manifest_path: str
    parts: list = None  # Specific parts to import (e.g., [1, 2, 3])
    separate: bool = False  # If True, add each part as a separate task
    folder_id: Optional[int] = None
    target_id: Optional[int] = None

@dataclass
class VocalsCommand(Command):
    path: str = None  # If None, trigger picker
    use_gpu: bool = False
    keep_all: bool = False  # Save all outputs (vocals + instrumental)

@dataclass
class BrowserCommand(Command):
    target_url: str = None

@dataclass
class PromoteBrowserDownload(Command):
    capture_id: int
    folder_id: Optional[int] = None
@dataclass
class RecaptureDownload(Command):
    id: str

@dataclass
class CreateFolder(Command):
    name: str
    parent_id: Optional[int] = None

@dataclass
class MoveTask(Command):
    source_id: str  # id or name
    target_folder_id: Optional[int]
    is_folder: bool = False

@dataclass
class DeleteFolder(Command):
    folder_id: int
    force: bool = False

@dataclass
class RemoveBrowserDownload(Command):
    id: int

@dataclass
class RegisterExternalTask(Command):
    filename: str
    total_size: int
    source: str = "upload" # 'upload' or 'download'
    state: str = "DOWNLOADING"

@dataclass
class UpdateExternalTask(Command):
    id: str
    downloaded_bytes: int
    speed: float = 0.0
    state: str = None # Optional state update

@dataclass
class ShareNotify(Command):
    message: str
    is_error: bool = False

@dataclass
class TakeoverRoom(Command):
    """Event for transitioning a participant to become the host."""
    room_id: str
    token: str
    files: List[Dict[str, Any]]
    devices: List[Dict[str, Any]]



# --- Bus ---
C = TypeVar("C", bound=Command)

class CommandHandler(Protocol[C]):
    def __call__(self, command: C) -> Any:
        ...

class CommandBus:
    def __init__(self):
        self._handlers: Dict[Type[Command], CommandHandler] = {}

    def register(self, command_type: Type[C], handler: CommandHandler[C]):
        self._handlers[command_type] = handler

    def handle(self, command: Command) -> Any:
        handler = self._handlers.get(type(command))
        if not handler:
            raise ValueError(f"No handler registered for {type(command)}")
        return handler(command)
