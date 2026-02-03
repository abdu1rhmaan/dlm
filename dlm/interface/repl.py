import cmd
from typing import Optional, List, Dict, Set, Tuple, Any
import shlex
import sys
import os
import threading
import time
from pathlib import Path
from dlm.app.commands import CommandBus, AddDownload, ListDownloads, StartDownload, PauseDownload, ResumeDownload, RemoveDownload, RetryDownload, SplitDownload, ImportDownload, VocalsCommand, BrowserCommand
from dlm.core.workspace import WorkspaceManager
from dlm.bootstrap import get_project_root
from dlm.interface.tui import TUI

def parse_index_selector(selector: str, get_uuid_by_index, max_index: int) -> list:
    """
    Parse an index selector expression into a sorted list of (index, uuid) tuples.
    
    Supported formats:
    - Single: 2
    - Multiple: 1 2 3
    - Range: 1..6 or 1-6
    - Mixed: 1 3 5..8 10
    - All: *
    - Negation: * !5 (all except 5) or 1..10 !3..5
    """
    if not selector.strip():
        return []
    
    indices = set()
    parts = selector.replace(',', ' ').strip().split()
    
    for part in parts:
        part = part.strip()
        if not part: continue
        
        exclude = False
        if part.startswith('!'):
            exclude = True
            part = part[1:]
        
        current_set = set()
        
        if part == '*':
            for i in range(1, max_index + 1):
                current_set.add(i)
        elif '..' in part or ('-' in part and any(c.isdigit() for c in part)):
            # Range: start..end or start-end
            try:
                sep = '..' if '..' in part else '-'
                s_str, e_str = part.split(sep, 1)
                start = int(s_str) if s_str else 1
                end = int(e_str) if e_str else max_index
                for i in range(start, end + 1):
                    current_set.add(i)
            except ValueError:
                continue
        else:
            # Single index
            try:
                idx = int(part)
                current_set.add(idx)
            except ValueError:
                continue
        
        if exclude:
            indices -= current_set
        else:
            indices.update(current_set)
    
    # Sort, filter bounds and map to UUIDs
    result = []
    for idx in sorted(indices):
        if 0 <= idx <= max_index:
            try:
                uuid = get_uuid_by_index(idx)
                result.append((idx, uuid))
            except Exception:
                continue
    
    return result


def normalize_cut_range(range_str: str) -> str:
    """Normalize H:M:S-H:M:S to HH:MM:SS-HH:MM:SS."""
    try:
        if '-' not in range_str:
            return range_str
        start, end = range_str.split('-')
        
        def norm(ts):
            parts = ts.split(':')
            if len(parts) == 3:
                h, m, s = [int(p) for p in parts]
                return f"{h:02d}:{m:02d}:{s:02d}"
            return ts
            
        return f"{norm(start.strip())}-{norm(end.strip())}"
    except:
        return range_str

def fix_text_display(text: str) -> str:
    """Reverse Arabic text for correct display in LTR terminals."""
    if not text: return text
    # Check for Arabic Unicode range
    is_arabic = any('\u0600' <= c <= '\u06FF' for c in text)
    if is_arabic:
        return text[::-1]
    return text

def truncate_middle(text: str, max_width: int) -> str:
    """Truncate text from the middle, preserving extension."""
    if len(text) <= max_width:
        return text
    
    # Use 3 dots for ASCII safe ellipsis
    ellipsis = "..."
    if max_width <= len(ellipsis):
        return text[:max_width]
    
    # Find extension
    if '.' in text:
        last_dot = text.rfind('.')
        ext = text[last_dot:]  # e.g. ".mp4"
    else:
        ext = ""
        last_dot = len(text)
    
    # Calculate available space for characters (excluding ellipsis)
    available = max_width - len(ellipsis)
    ext_len = len(ext)
    
    if ext_len >= available:
        # If extension is too long, just truncate from end and add ellipsis
        return text[:max_width-len(ellipsis)] + ellipsis
    
    # Split remaining space between start and end (before extension)
    remaining = available - ext_len
    start_len = remaining // 2 + remaining % 2
    end_len = remaining // 2
    
    base = text[:last_dot]
    
    start = base[:start_len]
    end = base[len(base)-end_len:] if end_len > 0 else ""
    
    return start + ellipsis + end + ext


def is_mobile_env() -> bool:
    """Detect if running in a mobile environment (e.g. Termux)."""
    # Check for Termux specifically
    if "TERMUX_VERSION" in os.environ:
        return True
    # Check for Android root or prefix
    if "ANDROID_ROOT" in os.environ or "ANDROID_DATA" in os.environ:
        return True
    # Check for common Termux prefixes in PATH
    path = os.environ.get("PATH", "")
    if "/data/data/com.termux" in path:
        return True
    
    return False

def check_binary_exists(name: str) -> bool:
    """Check if a system binary exists in PATH."""
    import shutil
    return shutil.which(name) is not None

def try_folder_picker():
    """
    Try to open a GUI folder picker.
    Returns (gui_available: bool, path: str).
    """
    try:
        # Check environment
        if os.name == 'posix' and not os.environ.get('DISPLAY'):
             return False, ""

        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()
        # Bring to front
        root.attributes('-topmost', True)
        
        folder = filedialog.askdirectory(title="Select download folder to resume")
        root.destroy()
        return True, folder or ""
    except Exception:
        return False, ""


def try_file_picker(title="Select file", filetypes=None):
    """
    Try to open a GUI file picker.
    Returns (gui_available: bool, path: str).
    """
    if filetypes is None:
        filetypes = [
            ("Media Files", "*.mp3 *.wav *.mp4 *.mkv *.avi *.mov"),
            ("Audio Files", "*.mp3 *.wav *.flac"),
            ("Video Files", "*.mp4 *.mkv *.avi *.mov"),
            ("All Files", "*.*")
        ]
    try:
        # Check environment
        if os.name == 'posix' and not os.environ.get('DISPLAY'):
             return False, ""

        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        
        file_path = filedialog.askopenfilename(
            title=title,
            filetypes=filetypes
        )
        root.destroy()
        return True, file_path or ""
    except Exception:
        return False, ""





def clear_lines(n: int):
    """Clear the last n lines from terminal using ANSI escape codes."""
    import sys
    if n <= 0: return
    # Move cursor up n lines
    sys.stdout.write(f'\033[{n}A')
    # Clear from cursor to end of screen
    sys.stdout.write('\033[J')
    sys.stdout.flush()

def clear_section_after_delay(lines: int, delay: float = 0.3):
    """Clear a section of output after a short delay."""
    import time
    time.sleep(delay)
    # Just clear lines upwards (standard delete), not whole screen wipe
    # We use explicit line deletion loop here for sections to avoid nuking everything below
    import sys
    for _ in range(lines):
        sys.stdout.write('\033[F\033[K')
    sys.stdout.flush()


# ... (SharedLineCounter, LineCountingStream unchanged)

# Removed complex LineCounting logic in favor of robust CLS.

class DLMShell(cmd.Cmd):
    intro = 'Welcome to DLM. Type help or ? to list commands.\n'
    
    @property
    def prompt(self) -> str:
        path = self.get_current_path()
        return f'dlm:{path}> '
    
    def do_eof(self, arg):
        """Exit the shell."""
        print() # Newline
        return True # Stop processing
        
    def do_EOF(self, arg):
        """Exit the shell."""
        print()
        return True

    def get_current_path(self) -> str:
        """Returns the current path string (e.g., /folder1/sub)."""
        if self.current_folder_id is None:
            return "/"
        
        path_parts = []
        curr_id = self.current_folder_id
        while curr_id is not None:
            folder = self.service.repository.get_folder(curr_id)
            if not folder: break
            path_parts.append(folder['name'])
            curr_id = folder['parent_id']
        
        return "/" + "/".join(reversed(path_parts)).replace(WorkspaceManager.WORKSPACE_DIR_NAME, "__workspace__")

    def __init__(self, bus: CommandBus, get_uuid_by_index, service, media_service):
        
        
        # Init colorama for Windows ANSI support
        import colorama
        colorama.init()
            
        super().__init__()

        self.bus = bus
        self.get_uuid_by_index = get_uuid_by_index
        self.service = service
        self.media_service = media_service

        self.workspace_manager = WorkspaceManager(get_project_root())
        self.show_workspace = False # Hidden by default
        
        # Ensure __workspace__ exists in DB at startup (Optional, or handled lazily)
        self._ensure_db_workspace_folder()

        self.current_folder_id = None # root
        self.copied_items = set() # clipboard: set of (uuid_str, is_folder)
        self.last_command = None

    def _ensure_db_workspace_folder(self):
        """Ensure the physical __workspace__ folder has a corresponding DB folder."""
        # This allows cd into it.
        # We need to find if it exists, if not create it.
        # But we must ensure specific ID or just name?
        # Name "__workspace__" at root level.
        # Name "__workspace__" at root level.
        from dlm.core.workspace import WorkspaceManager
        ws_name = WorkspaceManager.WORKSPACE_DIR_NAME
        existing = self.service.repository.get_folder_by_name(ws_name, None)
        if not existing:
             # Create it directly via repo or bus
             # Using bus to ensure proper flow? No, internal init.
             # But CreateFolder command might be safer.
             from dlm.app.commands import CreateFolder
             try:
                 self.bus.handle(CreateFolder(name=ws_name, parent_id=None))
             except Exception:
                 pass

    def _get_workspace_folder_id(self) -> Optional[int]:
        from dlm.core.workspace import WorkspaceManager
        ws_name = WorkspaceManager.WORKSPACE_DIR_NAME
        folder = self.service.repository.get_folder_by_name(ws_name, None)
        return folder['id'] if folder else None

    def _is_inside_workspace_context(self) -> bool:
        """Check if current_folder_id is inside __workspace__ hierarchy."""
        ws_id = self._get_workspace_folder_id()
        if not ws_id: return False
        
        # Traverse up
        curr = self.current_folder_id
        while curr:
            if curr == ws_id: return True
            f = self.service.repository.get_folder(curr)
            if not f: break
            curr = f['parent_id']
        return False

    def precmd(self, line):
        """Called before executing a command - handle '?' suffix, aliases and clear screen."""
        stripped = line.strip()
        if not stripped:
            return line
            
        if stripped == '?':
            return 'help'

        from dlm.interface.aliases import COMMAND_ALIASES
        
        # 1. Handle Aliases (Must be before '?' check to support 'ls?')
        parts = stripped.split()
        cmd_candidate = parts[0].lower()
        original_args = " ".join(parts[1:]) if len(parts) > 1 else ""
        
        # Check if cmd_candidate is an alias
        if cmd_candidate in COMMAND_ALIASES:
            cmd_part = COMMAND_ALIASES[cmd_candidate]
            line = f"{cmd_part} {original_args}".strip()
            stripped = line
        else:
            cmd_part = cmd_candidate

        # --- WORKSPACE PROTECTION ---
        # Block forbidden commands if inside workspace
        # Note: 'rm' is handled by smart logic in do_rm() - allows deletion at workspace root
        forbidden = ['mv', 'rename', 'mkdir', 'cp', 'ucp', 'paste', 'mk']
        
        if cmd_part in forbidden:
             if self._is_inside_workspace_context():
                 print(f"Error: Command '{cmd_part}' is FORBIDDEN inside the workspace.")
                 return "" # Suppress
        # ----------------------------

        # 2. Check if command exists or is a valid alias

        # 2. Check if command exists or is a valid alias
        # We need to get a list of available 'do_' methods
        available_cmds = [name[3:] for name in dir(self) if name.startswith('do_')]
        if cmd_part not in available_cmds and cmd_part not in COMMAND_ALIASES.values():
            if cmd_part not in ['?', 'help', 'exit', 'quit']:
                # Unknown command logic
                alias_list = ", ".join([f"{k}->{v}" for k, v in COMMAND_ALIASES.items()])
                print(f"Error: Unknown command '{cmd_part}'")
            
                return "" # Suppress execution

        # 3. Handle Help Syntax (e.g., "add ?" or "add?")
        # Logic: Only trigger if '?' is at the end of the first word (command) 
        # or is a standalone argument following the command.
        first_word = stripped.split()[0]
        is_help_request = False
        if first_word.endswith('?'):
            is_help_request = True
        elif len(parts) > 1 and parts[1] == '?':
            is_help_request = True

        if is_help_request:
            from dlm.interface.help_manager import get_detailed_help
            
            # CLEAR SCREEN FIRST (as requested for all commands)
            os.system('cls' if os.name == 'nt' else 'clear')
            print(f"{self.prompt}{line}")
            
            print(get_detailed_help(cmd_part))
            return "" # Suppress execution

        # 2. Force Full Clear for normal commands
        os.system('cls' if os.name == 'nt' else 'clear')
        
        # Reprint the prompt/command line so user sees context
        print(f"{self.prompt}{line}")

        return line

    def postcmd(self, stop, line):
        """Called after executing a command."""
        return stop

    def emptyline(self):
        """Do nothing on empty line (prevents repeating last command)."""
        pass

    def cmdloop(self, intro=None):
        """Override cmdloop to handle Ctrl+C as a hard exit."""
        try:
            super().cmdloop(intro)
        except KeyboardInterrupt:
            print("\nBye!")
            # Use sys.exit to ensure we break out of any parent loops in main.py
            import sys
            sys.exit(0)

    def do_retry(self, arg):
        """Retry a failed or cancelled download: retry <index_selector> (e.g., retry 1, retry 1..5)"""
        if not arg:
            print("Usage: retry <index_selector>")
            return
        
        try:
            selections = self._parse_selector(arg)
            if not selections:
                print(f"No valid downloads found for selector: {arg}")
                return
            
            for idx, uuid in selections:
                self.bus.handle(RetryDownload(id=uuid))
                print(f"Retrying task #{idx}...")
            
            # Show list after retrying
            print("")
            self.do_ls("")
        except Exception as e:
            print(f"Error: {e}")

    def do_error(self, arg):
        """Show the error message for a failed task: error <index>"""
        if not arg:
            print("Usage: error <index>")
            return
        
        try:
            # Use selector to ensure folder context and fresh index mapping
            selected = self._parse_selector(arg)
            if not selected:
                print(f"Task #{arg} not found in current view.")
                return
                
            idx, uuid = selected[0]
            # Fetch status to show error
            downloads = self.bus.handle(ListDownloads(folder_id=self.current_folder_id))
            task = next((d for d in downloads if d['id'] == uuid), None)
            
            if task and task.get('error'):
                print(f"\nTask #{idx} Error:")
                print("-" * 20)
                print(task['error'])
                print("-" * 20)
            elif task:
                print(f"Task #{idx} has no recorded error.")
            else:
                print(f"Task #{idx} not found.")
        except Exception as e:
            print(f"Error: {e}")

    def _format_size(self, size_bytes: int) -> str:
        """Format bytes to human readable string."""
        if not size_bytes: return "0 B"
        import math
        if size_bytes == 0: return "0 B"
        size_name = ("B", "KB", "MB", "GB", "TB")
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_name[i]}"

    def _calculate_folder_size(self, folder_id: Optional[int], recursive: bool = False, brw: bool = False) -> Tuple[int, int]:
        """
        Calculate total size and task count for a folder.
        Returns (total_size, task_count).
        """
        total_size = 0
        task_count = 0
        
        # 1. Sum tasks in this folder
        if brw:
            items = self.service.repository.get_browser_downloads_by_folder(folder_id)
            for item in items:
                s = item.get('size', 0) or 0
                total_size += int(s)
                task_count += 1
        else:
            downloads = self.service.repository.get_all_by_folder(folder_id)
            for d in downloads:
                s = d.total_size or 0
                total_size += int(s)
                task_count += 1
        
        # 2. Recurse if requested
        if recursive:
            subfolders = self.service.repository.get_folders(folder_id)
            for sub in subfolders:
                s, c = self._calculate_folder_size(sub['id'], recursive=True, brw=brw)
                total_size += s
                task_count += c
                
        return total_size, task_count

    def do_size(self, arg):
        """
        Show size of tasks or folders.
        usage: 
          size [ids...] [folder_names...]
          size                -> Sum all in current folder (recursive)
          size -brw           -> Switch context to browser downloads
          
        Examples:
          size 1 2 3          -> Sum items 1, 2, and 3 (files or folders)
          size MyFolder       -> Sum folder "MyFolder"
          size 1 MyFolder     -> Sum item 1 and folder "MyFolder"
        """
        args = shlex.split(arg)
        brw_driver = False
        potential_selectors = []
        potential_names = []
        
        # 1. Parse Flags & Args
        i = 0
        while i < len(args):
            val = args[i]
            if val == '-brw':
                brw_driver = True
            elif val == '*':
                potential_selectors.append('*')
            else:
                # Heuristic: if it looks like a selector part (digit, range), treat as selector
                # otherwise treat as name
                is_selector = True
                # Check for range chars
                if '..' in val or (len(val.split('-')) == 2 and val.split('-')[0].isdigit()):
                     pass
                elif val.startswith('!'):
                     pass
                elif val.isdigit():
                     pass
                else:
                     is_selector = False
                
                if is_selector:
                    potential_selectors.append(val)
                else:
                    potential_names.append(val)
            i += 1

        try:
            total_size = 0
            task_count = 0
            
            # Case A: No arguments -> Current Folder (Recursive)
            if not potential_selectors and not potential_names:
                s, c = self._calculate_folder_size(self.current_folder_id, recursive=True, brw=brw_driver)
                
                folder_name = "Current Folder"
                if self.current_folder_id:
                     f = self.service.repository.get_folder(self.current_folder_id)
                     if f: folder_name = f"Folder '{f['name']}'"
                elif self.current_folder_id is None:
                     folder_name = "Root"
                     
                print(f"{folder_name}: {self._format_size(s)} ({c} tasks)")
                return

            # Case B: Selectors (Indices)
            if potential_selectors:
                selector_str = " ".join(potential_selectors)
                selected = self._parse_selector(selector_str, brw=brw_driver)
                
                for idx, uuid in selected:
                    if uuid.startswith("folder:"):
                        # It's a folder
                        fid = int(uuid.split(":")[1])
                        s, c = self._calculate_folder_size(fid, recursive=True, brw=brw_driver)
                        total_size += s
                        task_count += c
                    else:
                        # It's a task
                        if brw_driver:
                             item = self.service.repository.get_browser_download(int(uuid))
                             if item:
                                 total_size += (item.get('size', 0) or 0)
                                 task_count += 1
                        else:
                             dl = self.service.repository.get(uuid)
                             if dl:
                                 total_size += (dl.total_size or 0)
                                 task_count += 1

            # Case C: Names (Folders)
            for name in potential_names:
                # Try to resolve as folder in current directory
                sub = self.service.repository.get_folder_by_name(name, self.current_folder_id)
                if sub:
                    s, c = self._calculate_folder_size(sub['id'], recursive=True, brw=brw_driver)
                    total_size += s
                    task_count += c
                else:
                    print(f"Warning: Item or Folder '{name}' not found.")

            print(f"Selected: {self._format_size(total_size)} ({task_count} tasks)")

        except Exception as e:
            print(f"Error: {e}")

    def _print_tree_recursive(self, folder_id, prefix, brw):
        """Recursive tree printer."""
        subfolders = self.service.repository.get_folders(folder_id)
        
        if brw:
            files = self.service.repository.get_browser_downloads_by_folder(folder_id)
        else:
            files = self.service.repository.get_all_by_folder(folder_id)
            
        items = []
        for f in subfolders: items.append({'type': 'folder', 'name': f['name'], 'id': f['id']})
        for f in files: 
            name = (f.get('filename') if brw else f.target_filename) or "Untitled"
            size = (f.get('size', 0) if brw else f.total_size) or 0
            items.append({'type': 'file', 'name': name, 'size': size})
            
        items.sort(key=lambda x: x['name'].lower())
        
        total = len(items)
        for i, item in enumerate(items):
            is_last = (i == total - 1)
            connector = "└── " if is_last else "├── "
            
            if item['type'] == 'folder':
                print(f"{prefix}{connector}{item['name']}")
                new_prefix = prefix + ("    " if is_last else "│   ")
                self._print_tree_recursive(item['id'], new_prefix, brw)
            else:
                sz = self._format_size(item['size'])
                print(f"{prefix}{connector}{item['name']} ({sz})")

    def do_tree(self, arg):
        """
        Show directory tree.
        Usage: tree [-brw]
        """
        brw = '-brw' in arg
        print(".")
        self._print_tree_recursive(self.current_folder_id, "", brw)

    def do_launcher(self, arg):
        """
        Open the DLM Feature Manager (Launcher).
        Manage modules like YouTube, Browser, etc.
        """
        try:
            from dlm.features.tui import run_feature_manager
            run_feature_manager()
        except Exception as e:
            print(f"Error launching feature manager: {e}")

    def do_share(self, arg):
        """
        Open the DLM Share interface for local network file sharing.
        Usage: share [--room ROOM_NAME] [--add-file PATH] [--add-folder PATH]
        """
        import shlex
        args = shlex.split(arg) if arg else []
        
        # Parse arguments
        room_name = "default"
        add_file = None
        add_folder = None
        
        i = 0
        while i < len(args):
            if args[i] in ['-r', '--room'] and i + 1 < len(args):
                room_name = args[i + 1]
                i += 2
            elif args[i] == '--add-file' and i + 1 < len(args):
                add_file = args[i + 1]
                i += 2
            elif args[i] == '--add-folder' and i + 1 < len(args):
                add_folder = args[i + 1]
                i += 2
            else:
                i += 1
        
        try:
            from dlm.share.cli import main as share_main
            share_main(room_name=room_name, add_file=add_file, add_folder=add_folder)
        except Exception as e:
            print(f"Error launching share: {e}")
            import traceback
            traceback.print_exc()
            


    def _get_uuid_by_index(self, index_arg):
        try:
            idx = int(index_arg)
            from dlm.bootstrap import get_uuid_by_index
            return get_uuid_by_index(idx)
        except Exception:
            raise ValueError("Invalid index")

    def _parse_selector(self, arg: str, brw: bool = False) -> list:
        """Parse index selector and return list of (index, uuid) tuples."""
        # Refresh indexing from database based on current folder
        from dlm.app.commands import ListDownloads
        self.bus.handle(ListDownloads(brw=brw, folder_id=self.current_folder_id, include_workspace=self.show_workspace))
        
        max_idx = self._get_max_index(brw=brw)
        from dlm.bootstrap import get_uuid_by_index
        return parse_index_selector(arg, lambda idx: get_uuid_by_index(idx, brw=brw), max_idx)

    def _get_max_index(self, brw: bool = False) -> int:
        """Get the current max queue index for the current view."""
        downloads = self.bus.handle(ListDownloads(brw=brw, folder_id=self.current_folder_id))
        return len(downloads)

    def _parse_playlist_selector(self, selector: str, max_index: int) -> set:
        """Parse 1..N selector for playlist items."""
        indices = set()
        parts = selector.replace(',', ' ').strip().split()
        for part in parts:
            if not part: continue
            
            # Exclusion (e.g. !5)
            exclude = False
            if part.startswith('!'):
                exclude = True
                part = part[1:]
                
            current_set = set()
            
            # Determine separator
            is_range = False
            start_str, end_str = None, None
            
            if '..' in part:
                 is_range = True
                 s_str, e_str = part.split('..', 1)
                 start_str, end_str = s_str, e_str
            elif '-' in part and len(part.split('-')) == 2:
                 # Check if it looks like a range (digits on both sides) 
                 # and not a negative number (though indices are usually positive)
                 # Simple heuristic: if split returns 2 parts and both are likely ints
                 p1, p2 = part.split('-', 1)
                 if p1.isdigit() and p2.isdigit():
                     is_range = True
                     start_str, end_str = p1, p2

            if is_range:
                 try:
                     start = int(start_str) if start_str else 1
                     end = int(end_str) if end_str else max_index
                     for i in range(start, end+1): current_set.add(i)
                 except: continue
            else:
                 try:
                     current_set.add(int(part))
                 except: continue

            if exclude:
                indices -= current_set
            else:
                indices.update(current_set)
                
        # Filter bounds
        return {i for i in indices if 1 <= i <= max_index}

    def _parse_item_overrides(self, args: list) -> tuple:
        """
        Parse --item flags.
        Returns: (overrides_dict, remaining_args)
        overrides = { index: { 'variants': {'audio': {}, 'video': {}} } }
        """
        overrides = {}
        clean_args = []
        
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == '-item':
                if i + 1 >= len(args):
                    print("Error: -item requires <selection>:<options>")
                    return {}, []
                
                val = args[i+1]
                i += 2
                
                if ':' not in val:
                    print(f"Error: Invalid --item syntax '{val}'. Expected <selection>:<options>")
                    continue
                    
                sel_str, opts_str = val.split(':', 1)
                
                # We can't validate indices yet (don't know playlist size), 
                # so store raw checks or validated later?
                # Actually, we parse selectors later when we know size? 
                # No, we need to structure it now.
                # Let's store raw selectors and resolve later, OR assume a large max and filter later.
                # Spec says "Validate selection indices against playlist size".
                # This must happen AFTER extraction.
                # So we store struct: { 'selector': sel_str, 'options': opts_str }
                # But we need to merge multiple --item flags?
                # Let's store a list of raw override rules.
                
                # Wait, priority rules say we need to merge.
                # We'll parse options now.
                
                # Parse Options
                opts = opts_str.split(',')
                variants = set()
                config = {'video': {}, 'audio': {}}
                
                for opt in opts:
                    opt = opt.strip()
                    if opt in ('audio', 'video'):
                        variants.add(opt)
                    elif '=' in opt:
                        k, v = opt.split('=', 1)
                        if k == 'cut':
                            config['video']['cut'] = v
                            config['audio']['cut'] = v
                        elif k == 'quality':
                            config['video']['quality'] = v
                        elif k.startswith('video.'):
                            key = k.split('.')[1]
                            config['video'][key] = v
                        elif k.startswith('audio.'):
                            key = k.split('.')[1]
                            config['audio'][key] = v
                        elif k == 'output':
                             # Item-specific output
                             config['video']['output'] = v.strip('"').strip("'")
                             config['audio']['output'] = v.strip('"').strip("'")
                        elif k == 'rename':
                             # Item-specific rename
                             config['video']['rename'] = v.strip('"').strip("'")
                             config['audio']['rename'] = v.strip('"').strip("'")
                        else:
                            print(f"Warning: Unknown option '{k}' in --item")
                    else:
                        print(f"Warning: Invalid option '{opt}'")
                        
                # If no variants specified, it implies "apply to whatever default mode is"
                # But if we have specific configs, we might need to know checking variants.
                # We'll store this rule to apply to indices later.
                
                if 'raw_rules' not in overrides: overrides['raw_rules'] = []
                overrides['raw_rules'].append({
                    'selector': sel_str,
                    'variants': variants,
                    'config': config
                })
                
            else:
                clean_args.append(arg)
                i += 1
                
        return overrides, clean_args

        return overrides, clean_args

    def _interactive_item_selection(self, total: int) -> list:
        """
        Step 1: Ask user for playlist items to include.
        Returns: list of selected indices (integers).
        """
        print(f"\nSelect playlist items (1..{total})")
        print("Examples: 1 3..5 !2 or * (default: *)")
        
        while True:
            choice = input("Selection [*]: ").strip()
            
            if not choice or choice == '*':
                return list(range(1, total + 1))
            
            # Use existing parser logic
            try:
                indices = self._parse_playlist_selector(choice, total)
                if indices:
                    return sorted(list(indices))
                print("No valid indices selected. Try again.")
            except Exception as e:
                print(f"Invalid selection: {e}")

    def _resolve_interactive_config(self, current_flags: dict, defaults: dict=None, prompt_prefix="", video_url=None, platform=None) -> dict:
        """
        Step 3: Resolve configuration interactively IF flags are missing.
        current_flags: dict of flags present (e.g. {'video': True, 'quality': None})
        defaults: fallback values
        video_url: Optional URL for extracting actual available qualities
        platform: Optional platform string (e.g. 'tiktok')
        Returns: resolved dict {'audio': bool, 'video': bool, 'quality': str, 'cut': str}
        """
        if defaults is None: defaults = {}
        
        # 1. Mode Resolution
        is_audio = current_flags.get('audio')
        is_video = current_flags.get('video')
        quality = current_flags.get('quality')
        
        # Implicit: If quality is set, it MUST be video
        if quality:
            is_video = True

        # If neither flag is set, ask (unless default mode implies one)
        if not is_audio and not is_video:
            if platform == 'spotify':
                 is_audio = True
            else:
                print(f"\n{prompt_prefix}Select Download Mode:")
                print("1. Video (Default)")
                print("2. Audio Only")
            
                choice = input("Choice [1]: ").strip()

                if choice == '2':
                    is_audio = True
                else:
                    is_video = True

                # Clear mode selection (4 lines: header + 2 options + input)
                clear_section_after_delay(5)

                
        # 2. Quality Resolution (Only if Video)
        quality = current_flags.get('quality')
        
        # TikTok Phase 1: Skip quality selection (single stream, auto-best)
        is_tiktok = (platform == 'tiktok') or (video_url and ("tiktok.com" in video_url.lower() or "vm.tiktok.com" in video_url.lower()))
        
        if is_video and not quality and not is_tiktok:
            # Extract metadata to get actual max quality if URL provided
            available_qualities = None
            if video_url:
                try:
                    import yt_dlp
                    ydl_opts = {
                        'quiet': True, 
                        'no_warnings': True, 
                        'extract_flat': False,
                        'http_headers': {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                        }
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(video_url, download=False)
                        if info and 'formats' in info:
                            heights = [f.get('height') for f in info['formats'] if f.get('height')]
                            if heights:
                                original_max = max(heights)
                                # Quality ladder
                                ladder = [2160, 1440, 1080, 720, 480, 360, 240, 144]
                                available_qualities = [h for h in ladder if h <= original_max]
                except Exception:
                    pass  # Fallback to showing all options
            
            print(f"\n{prompt_prefix}Select Max Quality:")
            
            if available_qualities:
                # Show only available qualities (no "Best" option)
                q_map = {}
                option_num = 1
                for h in available_qualities:
                    print(f"{option_num}. {h}p")
                    q_map[str(option_num)] = f"{h}p"
                    option_num += 1
            else:
                # Fallback: show all standard options
                print("1. 1080p")
                print("2. 720p")
                print("3. 480p")
                print("4. 360p")
                print("5. 144p")
                q_map = {'1': '1080p', '2': '720p', '3': '480p', '4': '360p', '5': '144p'}
            
            # Default to highest quality (option 1)
            q_choice = input("Choice [1]: ").strip() or '1'
            quality = q_map.get(q_choice)
            # Clear quality selection (dynamic lines based on options)
            clear_section_after_delay(len(q_map) + 3)
        elif is_video and not quality and is_tiktok:
            # TikTok: Auto-select best, no menu
            quality = 'best'
             
        # 3. Cut Resolution
        cut = current_flags.get('cut')
        if not cut:
             pass
             
        return {
            'audio': is_audio, 
            'video': is_video, 
            'quality': quality,
            'cut': cut 
        }

    # Removing old _interactive_playlist_config as it's replaced by logic below
    # def _interactive_playlist_config(self): ... 
    
    def do_add(self, arg):
        """Add a download to queue: add <url> [flags] [--item <sel>:<opts>]..."""
        # --- GUARD: Workspace Protection ---
        # "Add" is forbidden inside workspace to prevent stiff tasks from being created there.
        # User must be in a user-facing directory.
        if self._is_inside_workspace_context():
             print("Error: Cannot create tasks inside the internal workspace.")
             print("Please navigate to a user folder (e.g. 'cd /') before adding tasks.")
             return
        # -----------------------------------

        if not arg:
            print("Error: URL required.")
            return

        # 1. Split and Parse Overrides
        raw_parts = shlex.split(arg, posix=False)
        overrides_data, parts = self._parse_item_overrides(raw_parts)
        
        # 2. Parse Global Flags (from clean 'parts')
        url = None
        resume_flag = False; resume_path = None
        flag_audio = False; flag_video = False
        flag_quality = None; flag_cut = None
        flag_vocals = False; flag_vocals_gpu = False; flag_vocals_all = False
        flag_output = None; flag_rename = None
        flag_referer = None # New flag
        flag_only = None # New flag
        flag_limit = None # New flag

        i = 0
        while i < len(parts):
            part = parts[i]
            if part == '-r':
                resume_flag = True
                if i + 1 < len(parts): 
                    resume_path = parts[i+1]; i+=1
            elif part == '--audio': flag_audio = True
            elif part == '--video': flag_video = True
            elif part == '--limit':
                if i+1 < len(parts):
                    try: flag_limit = int(parts[i+1]); i+=1
                    except ValueError: pass
            elif part == '--quality':
                if i+1 < len(parts): flag_quality = parts[i+1]; i+=1
            elif part == '--output':
                if i+1 < len(parts): flag_output = parts[i+1].strip('"').strip("'"); i+=1
            elif part == '--rename':
                if i+1 < len(parts): flag_rename = parts[i+1].strip('"').strip("'"); i+=1
            elif part == '--referer':
                if i+1 < len(parts): flag_referer = parts[i+1].strip('"').strip("'"); i+=1
            elif part == '--only':
                if i+1 < len(parts): flag_only = parts[i+1]; i+=1
            elif part == '--cut':
                if i+1 < len(parts) and not parts[i+1].startswith('-'):
                    flag_cut = parts[i+1]; i+=1
                else: flag_cut = True
            elif part == '--vocals': flag_vocals = True
            elif part == '--vls': flag_vocals = True
            elif part == '--vocals-gpu': flag_vocals = True; flag_vocals_gpu = True
            elif part in ['--all', '-a']: flag_vocals_all = True
            elif url is None and not part.startswith('-'):
                 url = part.strip('"').strip("'")
            i += 1

        if not url:
            print("Error: URL required.")
            return

        # 3. Extraction
        try:
            print("Analyzing...", end='', flush=True) # Stay on line
            extract_result = self.media_service.extract_info(url, limit=flag_limit)
            
            # Clear Analyzing
            print("\r" + " " * 20 + "\r", end='', flush=True)
            
            if not extract_result:
                # Fallback purely URL based
                self.bus.handle(AddDownload(url=url, referer=flag_referer, folder_id=self.current_folder_id))
                print("Queued (RAW).")
                return
        except NotImplementedError as e:
            # Clear Analyzing line
            print("\r" + " " * 20 + "\r", end='', flush=True)
            # Clean error message for missing dependencies
            error_msg = str(e)
            if "yt-dlp" in error_msg or "yt_dlp" in error_msg:
                print("❌ Error: yt-dlp is not installed.")
                print("📦 Install it via Feature Manager:")
                print("   dlm launcher → Select 'Social' feature")
                print("\nOr manually: pip install yt-dlp")
            else:
                print(f"❌ Error: {error_msg}")
            return
        except Exception as e:
            # Clear Analyzing line
            print("\r" + " " * 20 + "\r", end='', flush=True)
            # For other errors, show a brief message
            print(f"❌ Error analyzing URL: {str(e)}")
            return

        # 4. Collection / Torrent Handling
        if extract_result.platform == 'torrent':
            # Torrent Special Handling
            metadata = extract_result.metadata
            
            # If magnet, we might need a resolution step here if not done in extractor
            if not metadata or not metadata.files:
                print(f"[TORRENT] Resolving metadata for: {metadata.title if metadata else url}...")
                # In a real implementation, this would call a method to resolve magnet metadata
                # via DHT. For now, we'll assume it's resolved or show failure.
                if not metadata:
                    print("Error: Could not resolve torrent metadata.")
                    return
                
                print(f"\n[TORRENT DISCOVERY]")
                print(f"Name: {metadata.title}")
                import math
                def format_size(size):
                    if size == 0: return "0 B"
                    size_name = ("B", "KB", "MB", "GB", "TB")
                    i = int(math.floor(math.log(size, 1024)))
                    p = math.pow(1024, i)
                    s = round(size/p, 2)
                    return "%s %s" % (s, size_name[i])
                
                print(f"Total Size: {format_size(metadata.total_size)}")
                print("\nFiles:")
                for file in metadata.files:
                    print(f"  [{file.index}] {file.name} ({format_size(file.size)})")
                
                # 1. File Selection
                print("\nSelect files:")
                print("- Examples: 1 2 4-6 , all , !3")
                while True:
                    sel_str = input("\nSelection [all]: ").strip()
                    if not sel_str or sel_str.lower() == 'all':
                        selected_indices = [f.index for f in metadata.files]
                        break
                    try:
                        # Map selection syntax to indices
                        # Support ! exclusion and ranges
                        raw_indices = self._parse_playlist_selector(sel_str, len(metadata.files))
                        selected_indices = sorted(list(raw_indices))
                        if selected_indices:
                            break
                        print("No valid files selected.")
                    except:
                        print("Invalid selection syntax.")

                # 2. Task Mode Selection
                print("\nCreate tasks as:")
                print("  [1] Single task (all selected files together)")
                print("  [2] One task per file")
                mode_choice = input("Choice [1]: ").strip() or '1'
                
                creation_mode = 'single' if mode_choice == '1' else 'multi'
                
                # 3. Task Creation
                if creation_mode == 'single':
                    # One task for the whole selected set
                    filenames = [f.name for f in metadata.files if f.index in selected_indices]
                    display_name = metadata.title if len(selected_indices) > 1 else filenames[0]
                    total_size = sum(f.size for f in metadata.files if f.index in selected_indices)
                    self.bus.handle(AddDownload(
                        url=url,
                        source='torrent',
                        title=display_name,
                        output_template=flag_output,
                        rename_template=flag_rename,
                        referer=flag_referer,
                        # Additional meta for torrent adapter
                        torrent_files=selected_indices,
                        torrent_file_offset=metadata.files[selected_indices[0]].offset if selected_indices else 0,
                        total_size=total_size,
                        folder_id=self.current_folder_id
                    ))
                else:
                    # One task per file
                    for idx in selected_indices:
                        file = next(f for f in metadata.files if f.index == idx)
                        self.bus.handle(AddDownload(
                            url=url,
                            source='torrent',
                            title=file.name,
                            output_template=flag_output,
                            rename_template=flag_rename,
                            referer=flag_referer,
                            torrent_files=[idx],
                            torrent_file_offset=file.offset,
                            total_size=file.size,
                            folder_id=self.current_folder_id
                        ))
                
                print(f"Created {'task' if creation_mode == 'single' else f'{len(selected_indices)} tasks'}.")
                return

            if extract_result.is_collection:
                entries = extract_result.entries
                
                # SPECIAL HANDLING FOR TIKTOK PROFILES
                is_tiktok_profile = extract_result.platform == "tiktok"
                
                if is_tiktok_profile:
                    # TikTok Profile UX: NO listing, total from metadata
                    total = getattr(extract_result.metadata, 'total_count', len(entries))
                    profile_name = getattr(extract_result.metadata, 'title', '@username')
                    
                    print(f"\nProfile: {profile_name}")
                    print(f"Total videos: {total}")
                    
                    # Selection prompt
                    print("\nSelect videos (default = all):")
                    print("- Examples: 1..5 , 3.. , *")
                    print("- Exclude: !2")
                    print("- Combine: 1,4,6..8")
                
                    # Same logic as interactive selection but inline to match visual requirement
                    while True:
                        choice = input("\nSelection [*]: ").strip()
                        if not choice or choice == '*':
                            selected_indices = list(range(1, total + 1))
                            break
                        try:
                            selected_indices = sorted(list(self._parse_playlist_selector(choice, total)))
                            if selected_indices:
                                break
                            print("No valid indices selected. Try again.")
                        except Exception as e:
                            print(f"Invalid selection: {e}")
                    
                    # Clear selection after (6 lines: headers + input)
                    clear_section_after_delay(7)
                else:
                    # Generic / YouTube Selection
                    total = len(entries)
                    print(f"Found collection with {total} items.")
                    
                    if flag_rename:
                        print("Error: rename is not allowed as a global option for playlists. Use --item <selection>:rename=...")
                        return

                    # --- STEP 1: Item Selection ---
                    selected_indices = []
                    if flag_only:
                        # Parse --only flag
                        selected_indices = sorted(list(self._parse_playlist_selector(flag_only, total)))
                        print(f"Selected {len(selected_indices)} items via --only.")
                    else:
                        # Interactive Selection
                        selected_indices = self._interactive_item_selection(total)
                        # Clear selection prompt (3 lines: header + examples + input)
                        clear_section_after_delay(4)

                if not selected_indices:
                    print("No items selected. Aborting.")
                    return

                # --- STEP 2: Configuration Scope ---
                # Check for explicit flags OR overrides
                has_global_flags = any([flag_audio, flag_video, flag_quality, flag_cut, flag_output])
                has_overrides = bool(overrides_data.get('raw_rules'))
                
                apply_global = True # Default
                
                if has_global_flags:
                    apply_global = True # Flags force global scope for non-overridden items
                elif has_overrides:
                    # If overrides exist but no global flags, we should ask?
                    # "Ask ONLY IF no configuration flags are provided"
                    # If user provides overrides, they are configuring. 
                    # But what about the REST of the items?
                    # Let's assume if overrides exist, the rest use default (Video/Best) or we ask?
                    # Simpler: If NO global flags provided, ASK. Even if overrides exist.
                    # Because overrides only cover specific items.
                    print("\n[?] Configuration Scope")
                    print("    You have item overrides but no global configuration.")
                    if input("    Apply one configuration to remaining items? [Y/n]: ").lower().startswith('n'):
                        apply_global = False
                    # Clear scope question (3 lines)
                    clear_section_after_delay(3)
                else:
                    # No flags, No overrides. Pure interactive.
                    print("\n[?] Configuration Scope")
                    print("    Apply same configuration to all selected items?")
                    if input("    Choice [Y/n]: ").lower().startswith('n'):
                        apply_global = False
                    # Clear scope question (3 lines)
                    clear_section_after_delay(3)

                # --- STEP 3: Configuration Resolution ---
                
                # Resolve Overrides first (fixed logic)
                # Structure: { index: { 'variants': set(), 'config': ... } }
                final_overrides = {}
                if 'raw_rules' in overrides_data:
                    for rule in overrides_data['raw_rules']:
                        indices = self._parse_playlist_selector(rule['selector'], total)
                        for idx in indices:
                            if idx not in final_overrides: final_overrides[idx] = {'variants': set(), 'config': {'video':{}, 'audio':{}}}
                            if rule['variants']: final_overrides[idx]['variants'].update(rule['variants'])
                            final_overrides[idx]['config']['video'].update(rule['config']['video'])
                            final_overrides[idx]['config']['audio'].update(rule['config']['audio'])

                # Global Config Cache (if apply_global)
                global_config = None
                if apply_global:
                    # Resolve ONCE (no URL for global, use standard options)
                    global_config = self._resolve_interactive_config(
                        {'audio': flag_audio, 'video': flag_video, 'quality': flag_quality, 'cut': flag_cut}, 
                        prompt_prefix="Global: ",
                        platform = extract_result.platform if extract_result else None
                    )
                
                # --- Processing ---
                count = 0
                for i, entry in enumerate(entries):
                    idx = i + 1
                    
                    # Skip unselected items
                    if idx not in selected_indices:
                        continue
                        
                    item_url = entry.get('url')
                    title = entry.get('title')
                    duration = entry.get('duration')
                    
                    # 1. Determine Item Config
                    ov = final_overrides.get(idx)
                    
                    tasks_to_create = [] # (mode, q, c, o, r)
                    
                    if ov:
                         # Override Logic (Existing)
                        modes = ov['variants']
                        if not modes:
                            # If override has config but no mode, inherit Global/Interactive mode
                            # But wait, if we are in "Per Item" mode, what do we inherit?
                            # If per-item, we interactively resolve the base mode for this item if not set?
                            # This gets complex.
                            # Simplification: If apply_global, use global mode. 
                            # If NOT apply_global, we effectively need to ask for this item if not specified.
                            
                            if apply_global:
                                if global_config['audio']: modes.add('audio')
                                else: modes.add('video')
                            else:
                                # Per Item Interaction needed for base mode if not in override
                                # Let's interactively resolve for this item
                                ic = self._resolve_interactive_config({}, prompt_prefix=f"Item {idx} ({title}): ", video_url=item_url)
                                if ic['audio']: modes.add('audio')
                                else: modes.add('video')
                                
                        for m in modes:
                            # Resolve Params
                            q = ov.get('config', {}).get(m, {}).get('quality')
                            c = ov.get('config', {}).get(m, {}).get('cut')
                            o = ov.get('config', {}).get(m, {}).get('output')
                            r = ov.get('config', {}).get(m, {}).get('rename')
                            
                            # Fallback to Global/Interactive
                            if q is None: 
                                if apply_global: q = global_config['quality']
                                else: q = self._resolve_interactive_config({'video': (m=='video')}, prompt_prefix=f"Item {idx} ({title}): ", video_url=item_url)['quality']
                            
                            if c is None:
                                 if apply_global: c = global_config['cut']
                                 # We don't ask for cut per item interactively unless implemented, usually we don't.
                                 
                            if o is None: o = flag_output # Output is Global Flag usually, or Override.
                            
                            tasks_to_create.append((m, q, c, o, r))

                    else:
                        # No Override
                        if apply_global:
                            # Use Global Config
                            m_list = ['audio'] if global_config['audio'] else ['video']
                            q = global_config['quality']
                            c = global_config['cut']
                            for m in m_list:
                                tasks_to_create.append((m, q, c, flag_output, flag_rename))
                        else:
                            # Per Item Interaction
                            display_title = fix_text_display(title or 'Unknown')
                            sys.stdout.write(f"\rConfiguring item {idx}/{total}: {display_title}" + " " * 20)
                            sys.stdout.flush()
                            
                            ic = self._resolve_interactive_config(
                                {'audio': flag_audio, 'video': flag_video, 'quality': flag_quality, 'cut': flag_cut},
                                video_url=item_url
                            )
                            # Clear the "Configuring item X/Y" line after config is done
                            sys.stdout.write('\r' + ' ' * 80 + '\r') # Clear line
                            sys.stdout.flush()
                            m_list = ['audio'] if ic['audio'] else ['video']
                            for m in m_list:
                                tasks_to_create.append((m, ic['quality'], ic['cut'], flag_output, flag_rename))

                    # Queue Tasks
                    for mode, q, c, o, r in tasks_to_create:
                        self.bus.handle(AddDownload(
                            url=item_url,
                            source=extract_result.platform, 
                            media_type=mode,
                            quality=q,
                            cut_range=c,
                            conversion_required=(mode=='audio'),
                            title=title,
                            duration=duration,
                            audio_mode='vocals' if flag_vocals else None,
                            vocals_gpu=flag_vocals_gpu,
                            vocals_keep_all=flag_vocals_all,
                            output_template=o,
                            rename_template=r,
                            referer=flag_referer,
                            folder_id=self.current_folder_id
                        ))
                        count += 1
                        
                # Clear the last "Configuring item X/Y" line if it was shown
                if not apply_global:
                    clear_section_after_delay(1, delay=0.2)
                    
                print(f"Queued {count} tasks from playlist.")

            else:
                # 5. Single Item
                # Check for explicit flags
                has_flags = any([flag_audio, flag_video, flag_quality, flag_cut, flag_vocals, flag_vocals_gpu, flag_output])
                
                # Interactive Prompts (Partial or Full)
                # Even if some flags are provided, we should ask for missing ones
                ic = self._resolve_interactive_config(
                    {'audio': flag_audio, 'video': flag_video, 'quality': flag_quality, 'cut': flag_cut},
                    video_url=extract_result.source_url if hasattr(extract_result, 'source_url') else url,
                    platform=extract_result.platform if extract_result else None
                )
                # Update flags with choices
                flag_audio = ic['audio']
                flag_video = ic['video']
                flag_quality = ic['quality']
                flag_cut = ic['cut']
                    
                self._handle_media_add(extract_result, flag_audio, flag_video, flag_quality, flag_cut, flag_vocals, flag_vocals_gpu, flag_vocals_all, flag_output, flag_rename, referer=flag_referer)

    def _handle_media_add(self, res, audio=False, video=False, quality=None, cut=None, vocals=False, vocals_gpu=False, vocals_all=False, output=None, rename=None, referer=None):
        """Handle single item addition."""
        # Modes
        modes = []
        if audio: modes.append('audio')
        elif video: modes.append('video')
        elif quality: modes.append('video')
        else: modes.append('video') # Default
        
        for m in modes:
            self.bus.handle(AddDownload(
                url=res.source_url,
                source=res.platform,
                media_type=m,
                quality=quality if m == 'video' else None,
                cut_range=cut,
                conversion_required=(m=='audio'),
                title=getattr(res.metadata, 'title', None) if res.metadata else None,
                duration=getattr(res.metadata, 'duration', None) if res.metadata else None,
                audio_mode='vocals' if vocals else None,
                vocals_gpu=vocals_gpu,
                vocals_keep_all=vocals_all,
                output_template=output,
                rename_template=rename,
                referer=referer,
                folder_id=self.current_folder_id
            ))
        print("Queued.")

    def do_start(self, arg):
        """Start queued downloads: start <selector> [--brw]
        
        Examples: start 1   start 1 --brw   start 1..5   start *   start folder1"""
        if not arg:
            print("Selector required")
            return
        
        brw_mode = "--brw" in arg
        clean_arg = arg.replace("--brw", "").strip()
        
        from dlm.app.commands import StartDownload, ListDownloads, PauseDownload, ResumeDownload
        
        try:
            # 1. Check if selector is '*'
            if clean_arg == '*':
                self.bus.handle(StartDownload(id="", brw=brw_mode, folder_id=self.current_folder_id, recursive=False))
                print(f"Starting all tasks in current folder{' (Browser)' if brw_mode else ''}...")
                print("")
                self.do_ls("")
                return

            # 2. Check if selector is a folder name in current view
            if not clean_arg.isdigit() and '..' not in clean_arg:
                folder = self.service.repository.get_folder_by_name(clean_arg, self.current_folder_id)
                if folder:
                    self.bus.handle(StartDownload(id="", brw=brw_mode, folder_id=folder['id'], recursive=True))
                    print(f"Starting tasks in folder '{clean_arg}' recursively...")
                    print("")
                    self.do_ls("")
                    return

            # 3. Standard Selector for specific tasks/folders by index
            selected = self._parse_selector(clean_arg, brw=brw_mode)
            if not selected:
                print("No valid indices found.")
                return
            
            downloads = self.bus.handle(ListDownloads(brw=brw_mode, folder_id=self.current_folder_id))
            dl_map = {d['id']: d for d in downloads}
            
            count = 0
            for idx, uuid_str in selected:
                if uuid_str.startswith("folder:"):
                    folder_id = int(uuid_str.replace("folder:", ""))
                    self.bus.handle(StartDownload(id="", brw=brw_mode, folder_id=folder_id, recursive=True))
                    count += 1
                else:
                    d = dl_map.get(uuid_str)
                    if d and d.get('state') != 'COMPLETED':
                        self.bus.handle(StartDownload(id=uuid_str, brw=brw_mode))
                        count += 1
            
            print(f"Started {count} item(s).")
            print("")
            self.do_ls("")
                 
        except Exception as e:
            print(f"Error: {e}")
            import traceback; traceback.print_exc()


    def do_pause(self, arg):
        """Pause downloads: pause <selector>
        
        Examples: pause 1   pause 1 2 3   pause 1..5   pause *"""
        try:
            if not arg:
                print("Selector required")
                return
            
            selected = self._parse_selector(arg)
            if not selected:
                print("No valid indices found.")
                return
            
            # Collect IDs first
            ids_to_pause = [uuid for idx, uuid in selected]
            
            count = 0
            for uuid in ids_to_pause:
                try:
                    self.bus.handle(PauseDownload(id=uuid))
                    count += 1
                except Exception:
                    pass
            
            print(f"Paused {count} download(s).")
        except Exception as e:
            print(f"Error: {e}")

    def do_resume(self, arg):
        """Resume downloads: resume <selector>
        
        Examples: resume 1   resume 1 2 3   resume 1..5   resume *"""
        try:
            if not arg:
                print("Selector required")
                return
            
            selected = self._parse_selector(arg)
            if not selected:
                print("No valid indices found.")
                return
            
            # Collect IDs first
            ids_to_resume = [uuid for idx, uuid in selected]
            
            count = 0
            for uuid in ids_to_resume:
                try:
                    self.bus.handle(ResumeDownload(id=uuid))
                    count += 1
                except Exception:
                    pass
            
            print(f"Resumed {count} download(s).")
        except Exception as e:
            print(f"Error: {e}")

    def do_remove(self, arg):
        """Deprecated legacy command. Redirects to 'rm'."""
        self.do_rm(arg)
    
    def do_mkdir(self, arg):
        """Create a new folder: mkdir <name>"""
        try:
            if not arg:
                print("Usage: mkdir <name>")
                return
            
            name = arg.strip()
            existing = self.service.repository.get_folder_by_name(name, self.current_folder_id)
            if existing:
                print(f"Error: Folder '{name}' already exists.")
                return
            
            from dlm.app.commands import CreateFolder
            self.bus.handle(CreateFolder(name=name, parent_id=self.current_folder_id))
            print(f"Folder '{name}' created.\n")
            self.do_ls("")
        except Exception as e:
            print(f"Error: {e}")

    def do_cd(self, arg):
        """Change current folder: cd <path/index> (supports .., /, index, or name)"""
        try:
            if not arg or arg == "/":
                self.current_folder_id = None
                self.do_ls("")
                return
            
            path = arg.strip()
            
            # Support navigation by Index (from LS view)
            if path.isdigit():
                try:
                    uuid_str = self._get_uuid_by_index(path)
                    if uuid_str.startswith("folder:"):
                        self.current_folder_id = int(uuid_str.replace("folder:", ""))
                        self.do_ls("")
                        return
                    else:
                        print(f"Error: Index {path} is a task, not a folder.")
                        return
                except:
                    pass # Continue and try as folder name if index lookup fails

            if path == "..":
                if self.current_folder_id is not None:
                    folder = self.service.repository.get_folder(self.current_folder_id)
                    self.current_folder_id = folder['parent_id']
                self.do_ls("")
                return

            # Support nested paths: folder1/folder2
            target_id = self.current_folder_id if not path.startswith('/') else None
            parts = path.split('/')
            
            for part in parts:
                if not part: continue
                if part == "..":
                    if target_id is not None:
                        folder = self.service.repository.get_folder(target_id)
                        target_id = folder['parent_id']
                    continue
                elif part == ".":
                    continue
                
                folder = self.service.repository.get_folder_by_name(part, target_id)
                if not folder:
                    print(f"Error: Folder '{part}' not found.")
                    return
                target_id = folder['id']
                
            self.current_folder_id = target_id
            self.do_ls("")
        except Exception as e:
            print(f"Error: {e}")

    def do_ls(self, arg):
        """List folders and downloads: ls [folder_name or id] [--brw]"""
        try:
            brw_mode = "--brw" in arg
            clean_arg = arg.replace("--brw", "").strip()
            
            target_folder_id = self.current_folder_id
            
            if clean_arg:
                # Try to resolve folder by name or ID
                try:
                    # If numeric, check if it's an ID
                    fid = int(clean_arg)
                    folder = self.service.repository.get_folder(fid)
                    if folder:
                        target_folder_id = folder['id']
                    else:
                        # Not an ID, try as name in current folder
                        folder = self.service.repository.get_folder_by_name(clean_arg, self.current_folder_id)
                        if folder: target_folder_id = folder['id']
                        else:
                            print(f"Error: Folder '{clean_arg}' not found.")
                            return
                except ValueError:
                    # Try as name
                    folder = self.service.repository.get_folder_by_name(clean_arg, self.current_folder_id)
                    if folder: target_folder_id = folder['id']
                    else:
                        print(f"Error: Folder '{clean_arg}' not found.")
                        return

            from dlm.app.commands import ListDownloads
            items = self.bus.handle(ListDownloads(brw=brw_mode, folder_id=target_folder_id, include_workspace=self.show_workspace))
            
            if not items:
                print("No items.")
                return

            # --- Filter Workspace ---
            if not self.show_workspace and target_folder_id == self.current_folder_id: # Only filter if listing current view
                 items = [i for i in items if i.get('filename') != WorkspaceManager.WORKSPACE_DIR_NAME]
            # ------------------------

            print(f"{'STAT':<4} {'#':<3} {'TAGS':<5} {'Filename':<26} {'Size':<10} {'Progress'}")
            print("-" * 75)
            
            for d in items:
                if d.get('is_folder'):
                    symbol = "[FLD]"
                    filename = truncate_middle(f"/{d['filename']}", 26)
                    tag_str = ""
                else:
                    state_map = {
                        'DOWNLOADING': '[↓]', 'PAUSED': '[||]', 'QUEUED': '[>]', 
                        'COMPLETED': '[✓]', 'FAILED': '[✗]', 'WAITING': '[…]', 'INITIALIZING': '[…]'
                    }
                    symbol = state_map.get(d['state'], '[ ]')
                    filename = truncate_middle(d['filename'], 26)
                    
                    tags = []
                    if d.get('source') == 'youtube': tags.append("YT")
                    elif d.get('source') == 'tiktok': tags.append("TT")
                    elif d.get('source') == 'browser': tags.append("BRW")
                    tag_str = " ".join(tags)

                size_val = d.get('total') or d.get('size') or 0
                size_str = self._format_size(size_val) if size_val > 0 else "-"
                progress_str = d['progress']

                print(f"{symbol:<4} {d['index']:<3} {tag_str:<5} {filename:<26} {size_str:<10} {progress_str}")
        except Exception as e:
            print(f"Error: {e}")

    def do_ws(self, arg):
        """Toggle workspace visibility: ws [on|off]"""
        mode = arg.strip().lower()
        if mode == 'on':
            self.show_workspace = True
            print("Workspace visible.")
        elif mode == 'off':
            self.show_workspace = False
            print("Workspace hidden.")
        else:
            # Toggle
            self.show_workspace = not self.show_workspace
            print(f"Workspace {'visible' if self.show_workspace else 'hidden'}.")
        
        self.do_ls("")

    def _resolve_path(self, path: str) -> Optional[int]:
        """Resolves a path string to a folder_id. Returns None for root."""
        if path == "/":
            return None
        
        target_id = self.current_folder_id if not path.startswith('/') else None
        parts = path.split('/')
        
        for part in parts:
            if not part: continue
            if part == "..":
                if target_id is not None:
                    folder = self.service.repository.get_folder(target_id)
                    if folder: target_id = folder['parent_id']
                continue
            elif part == ".":
                continue
            
            folder = self.service.repository.get_folder_by_name(part, target_id)
            if not folder:
                # Try as ID
                try:
                    fid = int(part)
                    f = self.service.repository.get_folder(fid)
                    if f: 
                        target_id = f['id']
                        continue
                except: pass
                raise ValueError(f"Folder '{part}' not found in path '{path}'")
            target_id = folder['id']
            
        return target_id

    def do_mv(self, arg):
        """Move task or folder: mv <selector> <target_path>"""
        try:
            parts = shlex.split(arg)
            if len(parts) < 2:
                print("Usage: mv <selector> <target_path> (e.g. mv 1 /movies/action)")
                return
            
            selector_arg = parts[0]
            target_ref = parts[1]
            
            # 1. Resolve Target Folder ID
            try:
                target_folder_id = self._resolve_path(target_ref)
            except ValueError as e:
                print(f"Error: {e}")
                return

            # 2. Resolve Sources
            selected = self._parse_selector(selector_arg)
            if not selected:
                print(f"No valid items found for selector '{selector_arg}'")
                return

            # 3. Handle Move
            from dlm.app.commands import MoveTask
            count = 0
            for idx, uuid_str in selected:
                is_source_folder = uuid_str.startswith("folder:")
                real_source_id = uuid_str.replace("folder:", "")
                
                if is_source_folder and target_folder_id is not None and int(real_source_id) == target_folder_id:
                    print(f"Skipping folder #{idx}: Cannot move a folder into itself.")
                    continue

                self.bus.handle(MoveTask(source_id=real_source_id, target_folder_id=target_folder_id, is_folder=is_source_folder))
                count += 1
            
            if count > 0:
                print(f"Moved {count} item(s) to {target_ref}.")
                self.do_ls("")
        except Exception as e:
            print(f"Error: {e}")
            
    def do_copy(self, arg):
        """Copy items to move later (Clipboard): copy <selector> (Alias: cp)"""
        try:
            if not arg:
                print("Usage: copy <selector>")
                return
            
            selected = self._parse_selector(arg)
            if not selected:
                print("No valid indices found.")
                return

            added = 0
            for idx, uuid_str in selected:
                is_folder = uuid_str.startswith("folder:")
                item = (uuid_str, is_folder)
                if item not in self.copied_items:
                    self.copied_items.add(item)
                    added += 1
            
            print(f"Copied {added} item(s). Total in clipboard: {len(self.copied_items)}")
            self.do_ls("")
        except Exception as e:
            print(f"Error: {e}")

    def do_uncopy(self, arg):
        """Uncopy items: uncopy <selector> or uncopy *"""
        try:
            if arg == '*':
                self.copied_items.clear()
                print("All items removed from clipboard.")
                return
                
            selected = self._parse_selector(arg)
            removed = 0
            for idx, uuid_str in selected:
                is_folder = uuid_str.startswith("folder:")
                item = (uuid_str, is_folder)
                if item in self.copied_items:
                    self.copied_items.remove(item)
                    removed += 1
            print(f"Removed {removed} item(s) from clipboard. Remaining: {len(self.copied_items)}")
        except Exception as e:
            print(f"Error: {e}")

    def do_paste(self, arg):
        """Move all copied items to the current folder: paste (Alias: v)"""
        try:
            if not self.copied_items:
                print("Clipboard is empty. Use 'copy' (cp) first.")
                return
            
            from dlm.app.commands import MoveTask
            count = 0
            target_folder_id = self.current_folder_id
            
            # Copy items to list because we'll clear the set
            items_to_move = list(self.copied_items)
            
            for uuid_str, is_folder in items_to_move:
                real_id = uuid_str.replace("folder:", "")
                
                if is_folder and target_folder_id is not None and int(real_id) == target_folder_id:
                    print(f"Skipping folder {real_id}: Cannot move a folder into itself.")
                    continue
                
                try:
                    self.bus.handle(MoveTask(source_id=real_id, target_folder_id=target_folder_id, is_folder=is_folder))
                    count += 1
                except Exception as e:
                    print(f"Error moving {uuid_str}: {e}")

            self.copied_items.clear()
            print(f"Pasted {count} item(s) to {self.get_current_path()}.")
            self.do_ls("")
        except Exception as e:
            print(f"Error: {e}")

    def do_rm(self, arg):
        """Remove task or folder: rm <selector> [--force] [--brw]"""
        try:
            brw_mode = "--brw" in arg
            force = "--force" in arg
            clean_arg = arg.replace("--brw", "").replace("--force", "").strip()
            
            if not clean_arg:
                print("Usage: rm <selector> [--force] [--brw]")
                return
            
            # Check if inside workspace - simplified approach
            if self._is_inside_workspace_context():
                # Get current path using existing method
                current_path_str = self.get_current_path()
                
                # At __workspace__ root? Allow rm of workspace folders
                if current_path_str == "/__workspace__":
                    # Allow normal selection and deletion of workspace folders
                    pass  # Continue to normal rm logic
                else:
                    # Inside a workspace folder - check depth
                    from dlm.core.workspace import WorkspaceManager
                    from pathlib import Path
                    
                    # Parse current path to check depth
                    # current_path_str is like "/__workspace__/cod-wwii.iso" or "/__workspace__/cod-wwii.iso/segments"
                    parts = [p for p in current_path_str.split('/') if p]
                    
                    if len(parts) == 2:  # __workspace__/task_name
                        # At task workspace root - allow deletion with confirmation
                        if clean_arg.lower() in ['.', 'current', '*']:
                            workspace_name = parts[1]
                            confirm = input(f"Delete entire workspace '{workspace_name}'? [y/N]: ")
                            if confirm.lower() == 'y':
                                # Get the actual filesystem path
                                wm = WorkspaceManager(Path.cwd())
                                task_folder = wm.workspace_root / workspace_name
                                
                                if task_folder.exists():
                                    import shutil
                                    shutil.rmtree(task_folder)
                                    print(f"✅ Workspace '{workspace_name}' deleted.")
                                    # Navigate back to parent
                                    self.do_cd('..')
                                    return
                                else:
                                    print(f"Error: Workspace folder not found at {task_folder}")
                                    return
                            else:
                                print("Cancelled.")
                                return
                        else:
                            print("Error: Can only delete current workspace. Use 'rm .' or 'rm current'")
                            return
                    elif len(parts) > 2:  # Inside subdirectory
                        # Block deletion inside subdirectories
                        print("Error: Cannot delete items inside workspace subdirectories.")
                        print("Navigate to workspace root to delete the entire workspace.")
                        return
            
            selected = self._parse_selector(clean_arg, brw=brw_mode)
            if not selected:
                print("No valid indices found.")
                return

            for idx, uuid_str in reversed(selected):
                if uuid_str.startswith("folder:"):
                    folder_id = int(uuid_str.replace("folder:", ""))
                    if not force:
                        confirm = input(f"Are you sure you want to delete folder #{idx} and all its contents? [y/N]: ").lower()
                        if confirm != 'y': continue
                    
                    from dlm.app.commands import DeleteFolder
                    try:
                        self.bus.handle(DeleteFolder(folder_id=int(uuid_str.replace("folder:", "")), force=force))
                        print(f"Folder #{idx} removed.")
                    except ValueError as e:
                        print(f"Error removing folder #{idx}: {e}")
                else:
                    if brw_mode:
                        from dlm.app.commands import RemoveBrowserDownload
                        # uuid_str here is actually the stringified ID from `ls --brw` mapping
                        # bootstrap.py: `_browser_index_to_id = {i: item['id'] ...}`
                        # so uuid_str is likely "123" (the DB ID)
                        try:
                            self.bus.handle(RemoveBrowserDownload(id=int(uuid_str)))
                            print(f"Browser Task #{idx} removed.")
                        except ValueError:
                             print(f"Error: Invalid browser ID {uuid_str}")
                    else:
                        from dlm.app.commands import RemoveDownload
                        self.bus.handle(RemoveDownload(id=uuid_str))
                        print(f"Task #{idx} removed.")
            self.do_ls("--brw" if brw_mode else "") # Refresh same view
        except Exception as e:
            print(f"Error: {e}")
    
    def do_split(self, arg):
        """Split download into parts: split <id> --parts <N> --users <u1> <u2> ... [--assign <u1_parts> <u2_parts> ...]
        
        Example: split 1 --parts 8 --users Alice Bob --assign 1..3,5 4,6..8"""
        import re
        
        if not arg:
            print("Error: Arguments required.")
            print("Usage: split <id> --parts <N> --users <u1> <u2> ... [--assign <u1_parts> <u2_parts> ...]")
            return
        
        parts_arg = arg.split()
        if len(parts_arg) < 1:
            print("Error: Download ID required.")
            return

        # Fix: Check if first arg is a flag (missing ID)
        if parts_arg[0].startswith('-'):
            print("Error: Download ID must be the first argument (e.g., 'split 1 --parts ...').")
            return
        
        try:
            idx = int(parts_arg[0])
            from dlm.bootstrap import get_uuid_by_index
            download_id = get_uuid_by_index(idx)
            
            # --- GUARD: Block Torrent Splitting ---
            downloads = self.bus.handle(ListDownloads(folder_id=self.current_folder_id))
            task = next((d for d in downloads if d['id'] == download_id), None)
            if task and task.get('source') == 'torrent':
                print("Error: Torrent splitting is not supported. Only HTTP/YouTube downloads can be split.")
                return
            # --------------------------------------
            
        except ValueError:
            download_id = parts_arg[0]
        except Exception as e:
            print(f"Error: Invalid download ID: {e}")
            return
        
        try:
            parts_idx = parts_arg.index('--parts')
            num_parts = int(parts_arg[parts_idx + 1])
        except (ValueError, IndexError):
            print("Error: --parts <N> required.")
            return
        

            
        users = []
        if '--users' in parts_arg:
            try:
                 users_idx = parts_arg.index('--users')
                 assign_idx = parts_arg.index('--assign') if '--assign' in parts_arg else len(parts_arg)
                 users_input = parts_arg[users_idx + 1:assign_idx]
                 if not users_input: raise ValueError("Empty users")
                 
                 if len(users_input) == 1 and users_input[0].isdigit():
                    num_users = int(users_input[0])
                    users = [f"user_{i}" for i in range(1, num_users + 1)]
                 else:
                    users = users_input
            except Exception:
                 print("Error parsing --users")
                 return
        else:
            # Default to 1 user "Local"
            users = ["Local"]
        
        partial_assignments = {}
        if '--assign' in parts_arg:
            assign_idx = parts_arg.index('--assign')
            assign_remainder = ' '.join(parts_arg[assign_idx + 1:])
            
            if '|' in assign_remainder:
                assign_specs = [spec.strip() for spec in assign_remainder.split('|')]
            else:
                assign_specs = parts_arg[assign_idx + 1:]
            
            if len(assign_specs) > len(users):
                print(f"Error: Too many assignments ({len(assign_specs)}) for {len(users)} users.")
                return
            
            for i, spec in enumerate(assign_specs):
                user_idx = i + 1
                try:
                    parsed = self._parse_parts_spec(spec, num_parts)
                    if parsed:
                        partial_assignments[user_idx] = parsed
                except Exception as e:
                    print(f"Error parsing assignment for user {user_idx}: {e}")
                    return
        
        assignments = partial_assignments.copy()
        remaining_parts = set(range(1, num_parts + 1))
        
        for parts_list in assignments.values():
            remaining_parts -= set(parts_list)
        
        # --- Auto-Assignment Logic ---
        if not assignments:
             # Case 1: Single User -> Auto-assign all
             if len(users) == 1:
                 assignments[1] = sorted(list(remaining_parts))
                 remaining_parts.clear()
                 # Silent auto-assign for single user
             
             # Case 2: Multiple Users -> Propose Even Split
             else:
                 per_user = num_parts // len(users)
                 print(f"\n[?] No assignments provided.")
                 print(f"    Distribute {num_parts} parts among {len(users)} users (approx {per_user} each)?")
                 
                 choice = input("    Auto-assign? [Y/n]: ").strip().lower()
                 if not choice or choice == 'y':
                     # Execute Even Split
                     current_part = 1
                     all_p = list(range(1, num_parts + 1))
                     
                     start_idx = 0
                     for u_idx in range(1, len(users) + 1):
                         is_last = (u_idx == len(users))
                         
                         if is_last:
                             # Remainder to last
                             chunk = all_p[start_idx:]
                         else:
                             chunk = all_p[start_idx : start_idx + per_user]
                             start_idx += per_user
                         
                         assignments[u_idx] = chunk
                         
                     remaining_parts.clear()
                     print("Auto-assignment complete.")
                     
        # If still unassigned (user said 'n' or partial manual), fall through to Interactive
        if len(assignments) < len(users):
            print(f"\n{'='*60}")
            print("Interactive Part Assignment")
            print(f"{'='*60}\n")
            
            for user_idx in range(1, len(users) + 1):
                if user_idx in assignments:
                    continue
                
                print(f"User {user_idx} ({users[user_idx-1]}):")
                print(f"  Remaining parts: {sorted(list(remaining_parts))}")
                
                while True:
                    spec = input(f"  Assign parts (e.g. 1, 3-5, *): ").strip()
                    if not spec: continue
                    
                    try:
                        if spec == '*':
                            parsed = sorted(list(remaining_parts))
                        else:
                            parsed = self._parse_parts_spec(spec, num_parts)
                            
                        # Validate availability
                        invalid = [p for p in parsed if p not in remaining_parts]
                        if invalid:
                            print(f"  Error: Parts {invalid} already assigned or invalid.")
                            continue
                            
                        assignments[user_idx] = parsed
                        remaining_parts -= set(parsed)
                        break
                    except Exception as e:
                        print(f"  Error: {e}")
        
        if remaining_parts:
            remaining_display = self._format_parts_set(remaining_parts)
            print(f"\nError: unassigned parts remain: {remaining_display}")
            print("Split operation aborted.")
            return
        
        # Parse workspace name
        name = None
        if '--name' in parts_arg:
            try:
                name_idx = parts_arg.index('--name')
                name = parts_arg[name_idx + 1]
            except IndexError:
                print("Error: --name requires a value")
                return
        elif '--workspace-name' in parts_arg:
            try:
                name_idx = parts_arg.index('--workspace-name')
                name = parts_arg[name_idx + 1]
            except IndexError:
                print("Error: --workspace-name requires a value")
                return

        try:
            folder = self.bus.handle(SplitDownload(download_id, num_parts, users, assignments, workspace_name=name))
            print(f"\n{'='*60}")
            print(f"Split complete!")
            print(f"{'='*60}")
            print(f"Manifests saved to: {folder}")
            print(f"\nGenerated files:")
            print(f"  - task.manifest.json")
            for i in range(1, len(users) + 1):
                print(f"  - user_{i}.manifest.json ({users[i-1]})")
        except Exception as e:
            print(f"Error: {e}")
    
    def _parse_parts_spec(self, spec: str, max_parts: int) -> list:
        if spec.strip() == '*':
            return sorted(list(range(1, max_parts + 1)))

        parts = set()
        for segment in spec.split(','):
            segment = segment.strip()
            if not segment: continue
            if '..' in segment:
                try:
                    start, end = segment.split('..')
                    start, end = int(start.strip()), int(end.strip())
                    if start < 1 or end > max_parts or start > end:
                        raise ValueError(f"Invalid range {segment}")
                    for i in range(start, end + 1): parts.add(i)
                except ValueError: raise ValueError(f"Invalid range format: {segment}")
            else:
                try:
                    part = int(segment)
                    if part < 1 or part > max_parts:
                        raise ValueError(f"Part {part} out of range (1..{max_parts})")
                    parts.add(part)
                except ValueError: raise ValueError(f"Invalid part number: {segment}")
        return sorted(list(parts))
    
    def _format_parts_set(self, parts_set: set) -> str:
        if not parts_set: return "none"
        parts = sorted(list(parts_set))
        ranges = []
        if not parts: return "none"
        
        start = parts[0]
        end = parts[0]
        for i in range(1, len(parts)):
            if parts[i] == end + 1: 
                end = parts[i]
            else:
                ranges.append(str(start) if start == end else f"{start}..{end}")
                start = end = parts[i]
        ranges.append(str(start) if start == end else f"{start}..{end}")
        return ",".join(ranges)
    
    def do_import(self, arg):
        """Import downloads from a workspace manifest: import <path_to_manifest.json> [--sep] [--target <path>]"""
        try:
            from pathlib import Path
            wm = WorkspaceManager(get_project_root())
        except Exception as e:
            print(f"Error: {e}")
            return
        args = shlex.split(arg, posix=False)
        # We don't return if 'args' is empty, because we want to trigger the GUI picker below.
        # But we DO check for browser mode if args exist.

        # Determine Context and Base Target
        context_id = self.current_folder_id
        base_target_id = context_id
        
        # If inside workspace, new tasks/folders must go to Root (or user folder)
        if self._is_inside_workspace_context():
            base_target_id = None # Root

        # Check for --fld flag
        target_id = base_target_id
        
        if '--fld' in args:
            idx = args.index('--fld')
            args.pop(idx)
            if idx < len(args):
                folder_name = args.pop(idx)
                # Create folder in base destination
                from dlm.app.commands import CreateFolder
                try:
                    target_id = self.bus.handle(CreateFolder(name=folder_name, parent_id=base_target_id))
                    print(f"Created folder: {folder_name}")
                except Exception as e:
                    print(f"Error creating folder: {e}")
                    return
            else:
                print("Error: Missing folder name for --fld")
                return

        # Check for browser mode
        if '--brw' in args:
            args.remove('--brw')
            selector = " ".join(args)
            
            # Fetch browser captures to get total count
            from dlm.app.commands import ListDownloads, PromoteBrowserDownload
            items = self.bus.handle(ListDownloads(brw=True))
            if not items:
                print("No browser captures found.")
                return

            try:
                indices = self._parse_playlist_selector(selector, len(items))
                if not indices:
                    print("No valid IDs selected.")
                    return
                
                count = 0
                for idx in indices:
                    # Get capture_id (which is hidden in the item list returned by bootstrap)
                    # The bootstrap handler for ListDownloads(brw=True) returns a list of DICTs.
                    # I need to make sure 'id' is there and it's the DATABASE ID.
                    capture = items[idx-1]
                    capture_id = int(capture['id'])
                    self.bus.handle(PromoteBrowserDownload(capture_id=capture_id, folder_id=target_id))
                    count += 1
                
                print(f"✅ Imported {count} task(s) to the main list.")
            except Exception as e:
                print(f"Error importing browser captures: {e}")
            return

        # Existing manifest import logic
        from pathlib import Path
        
        path_str = None
        filter_parts = None
        separate = False
        
        # 1. Parse flags
        remaining_args = []
        i = 0
        while i < len(args):
            if args[i] == '--parts':
                # Collect until next flag
                sel_parts = []
                i += 1
                while i < len(args) and not args[i].startswith('--'):
                    sel_parts.append(args[i])
                    i += 1
                if sel_parts:
                    try:
                        filter_parts = self._parse_parts_spec(' '.join(sel_parts).replace(' ', ','), 1000)
                    except Exception as e:
                        print(f"Error parsing parts: {e}")
                        return
                continue
            elif args[i] in ['--separate', '--sep']:
                separate = True
                i += 1
            else:
                remaining_args.append(args[i])
                i += 1
        
        if not remaining_args:
            path_str = "" # Signal for GUI picker
        else:
            path_str = remaining_args[0]
        
        # 2. Determine path
        manifest_path_str = path_str.strip('"').strip("'")
        
        if not manifest_path_str:
            # Trigger picker
            if is_mobile_env():
                manifest_path_str = input("Enter manifest path: ").strip().strip('"').strip("'")
            else:
                gui_ok, picker_path = try_file_picker(
                    title="Select manifest or DSL file",
                    filetypes=[
                        ("DLM Import DSL", "*.dlm"),
                        ("JSON Manifest", "*.json"),
                        ("Torrent Files", "*.torrent"),
                        ("All Files", "*.*")
                    ]
                )
                if gui_ok:
                    manifest_path_str = picker_path
                else:
                    manifest_path_str = input("Enter manifest path: ").strip().strip('"').strip("'")
        
        if not manifest_path_str:
            print("Cancelled.")
            return

        if manifest_path_str.lower().endswith('.torrent'):
            print("\n[TIP] .torrent files should be added using 'add' command, not 'import'.")
            print(f"Running: add \"{manifest_path_str}\"")
            self.do_add(f'"{manifest_path_str}"')
            return
            
        manifest_path = Path(manifest_path_str)
        if not manifest_path.exists():
            print(f"Error: File not found: {manifest_path}")
            return
        
        # --- NEW: DSL Detection & Processing ---
        is_json = False
        content = ""
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                content = f.read()
            import json
            json.loads(content)
            is_json = True
        except:
            is_json = False

        if not is_json:
            # Treat as DSL
            from dlm.infra.dsl.parser import DSLParser, DSLEvaluator
            parser = DSLParser()
            evaluator = DSLEvaluator()
            
            try:
                ast = parser.parse(content)
                tasks = evaluator.evaluate(ast)
                if evaluator.errors:
                    for err in evaluator.errors:
                         print(err)
                    return
                
                if not tasks:
                    print("No tasks found in DSL file.")
                    return
                
                print(f"Importing {len(tasks)} target(s) from DSL...")
                self._process_dsl_tasks(tasks)
                return
            except Exception as e:
                print(f"DSL Error: {e}")
                return

        # Legacy manifest import logic
        if filter_parts is None:
            try:
                import json
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    manifest = json.load(f)
                
                total_parts = 0
                # Check different manifest formats
                if 'parts' in manifest:
                    total_parts = manifest['parts']
                elif 'assigned_parts' in manifest:
                    total_parts = len(manifest['assigned_parts'])
                
                if total_parts > 0:
                    print(f"\nThis file has {total_parts} parts.")
                    print("Select parts to add:")
                    print("  Examples: 1 2 3, 1..10, !4, * (all)")
                    
                    while True:
                        try:
                            user_input = input("> ").strip().lower()
                            if not user_input:
                                continue
                            if user_input in ['exit', 'quit', 'cancel', 'c']:
                                print("Cancelled.")
                                return
                            
                            if user_input == '*':
                                # All parts
                                filter_parts = list(range(1, total_parts + 1))
                                break
                            else:
                                # Parse spec
                                filter_parts = self._parse_parts_spec(user_input, total_parts)
                                break
                        except KeyboardInterrupt:
                            print("\nCancelled.")
                            return
                        except Exception as e:
                            print(f"Invalid input: {e}. Try again:")
            except Exception as e:
                # If we can't read manifest, proceed without filter
                print(f"Warning: Could not read manifest for part selection: {e}")
            
        try:
            self.bus.handle(ImportDownload(
                manifest_path=str(manifest_path),
                parts=filter_parts,
                separate=separate,
                folder_id=context_id,
                target_id=target_id
            ))
            # Note: Success message is now printed by service layer for better detail
        except Exception as e:
            print(f"Error: {e}")
            
    do_i = do_import

    def _process_dsl_tasks(self, tasks: List[Dict[str, Any]]):
        """Evaluate and queue tasks from DSL evaluation."""
        from dlm.app.commands import AddDownload, ListDownloads
        
        for task in tasks:
            url = task['url']
            config = task['config']
            overrides = task['overrides']
            
            # 1. Extract Info to see if it's a playlist
            print(f"Analyzing: {url}...")
            res = self.media_service.extract_info(url)
            if not res:
                print(f"Error: Could not resolve URL: {url}")
                continue
            
            if not res.is_collection:
                # Single Item logic
                # Merge missing info interactively
                final_config = self._resolve_interactive_config(
                    {
                        'audio': config.get('mode') == 'audio',
                        'video': config.get('mode') == 'video',
                        'quality': config.get('quality'),
                        'cut': config.get('cut')
                    },
                    video_url=url,
                    platform=res.platform
                )
                
                self._handle_media_add(
                    res, 
                    audio=final_config['audio'],
                    video=final_config['video'],
                    quality=final_config['quality'] if final_config['video'] else None,
                    cut=final_config['cut'],
                    vocals=config.get('vocals', False),
                    vocals_gpu=config.get('gpu', False),
                    output=config.get('output')
                )
            else:
                # Playlist Logic (Smart Resolver)
                self._process_dsl_playlist(res, config, overrides)

    def _process_dsl_playlist(self, res, global_config, overrides):
        """Handle playlist import with smart missing info resolution."""
        from dlm.app.commands import AddDownload
        entries = res.entries
        total = len(entries)
        print(f"Processing playlist: {res.metadata.title} ({total} items)")
        
        # 1. Map all items to their initial config (Override > Global DSL Scope)
        items_config = {}
        for i in range(1, total + 1):
            if i in overrides:
                items_config[i] = overrides[i]
            else:
                items_config[i] = global_config.copy()

        # 2. Identify Missing Info (Mode or Quality if Video)
        def get_deficiency(cfg):
             mode_missing = cfg.get('mode') is None
             quality_missing = (cfg.get('mode') == 'video' or mode_missing) and cfg.get('quality') is None
             return (mode_missing, quality_missing)

        groups = {}
        for i in range(1, total + 1):
            deficiency = get_deficiency(items_config[i])
            if deficiency[0] or deficiency[1]:
                if deficiency not in groups: groups[deficiency] = []
                groups[deficiency].append(i)

        # 3. Resolve Groups (Smart Question)
        for (m_miss, q_miss), indices in groups.items():
            if not indices: continue
            
            print(f"\nMissing information for items: {self._format_parts_set(set(indices))}")
            choice = input("Apply same value to all in this group? [Y/n]: ").strip().lower()
            
            if choice != 'n':
                # Batch Resolve
                resolved = self._resolve_interactive_config(
                    {
                        'audio': items_config[indices[0]].get('mode') == 'audio',
                        'video': items_config[indices[0]].get('mode') == 'video',
                        'quality': items_config[indices[0]].get('quality')
                    },
                    prompt_prefix="Group Configuration: ",
                    video_url=entries[indices[0]-1].get('url'),
                    platform=res.platform
                )
                # Update all in group
                for idx in indices:
                    items_config[idx]['mode'] = 'audio' if resolved['audio'] else 'video'
                    items_config[idx]['quality'] = resolved['quality']
            else:
                # Individual Resolve
                for idx in indices:
                    title = entries[idx-1].get('title', f"Item {idx}")
                    resolved = self._resolve_interactive_config(
                        {
                            'audio': items_config[idx].get('mode') == 'audio',
                            'video': items_config[idx].get('mode') == 'video',
                            'quality': items_config[idx].get('quality')
                        },
                        prompt_prefix=f"Configuring {idx} ({title}): ",
                        video_url=entries[idx-1].get('url'),
                        platform=res.platform
                    )
                    items_config[idx]['mode'] = 'audio' if resolved['audio'] else 'video'
                    items_config[idx]['quality'] = resolved['quality']

        # 4. Final Queueing
        count = 0
        for i, entry in enumerate(entries):
            idx = i + 1
            cfg = items_config[idx]
            
            mode = cfg.get('mode', 'video')
            self.bus.handle(AddDownload(
                url=entry.get('url'),
                source=res.platform,
                media_type=mode,
                quality=cfg.get('quality') if mode == 'video' else None,
                cut_range=cfg.get('cut'),
                conversion_required=(mode == 'audio'),
                title=entry.get('title'),
                duration=entry.get('duration'),
                audio_mode='vocals' if cfg.get('vocals') else None,
                vocals_gpu=cfg.get('gpu', False),
                output_template=cfg.get('output'),
                folder_id=self.current_folder_id
            ))
            count += 1
            
        print(f"Queued {count} tasks from playlist DSL.")
    
    def do_export(self, arg):
        """Export workspace task: export [--final] [destination]
        
        --final: Mark task as complete and move file to 'exported' or destination.
        """
        # CRITICAL: Context check
        if not self._is_inside_workspace_context():
             print("Error: export must be run from inside a task workspace.")
             return
             
        args = shlex.split(arg)
        is_final = '--final' in args
        if '--final' in args: args.remove('--final')
        
        destination = args[0] if args else "exported"
        
        if not is_final:
             print("Usage: export --final (to finalize and move to exported/)")
             print("       export --final <path> (to move specific destination)")
             return
             
        from dlm.core.workspace import WorkspaceManager
        from dlm.bootstrap import get_project_root
        wm = WorkspaceManager(get_project_root())
        
        # We are likely inside /__workspace__/task or /__workspace__/task/segments
        # Let's resolve the task root
        current = Path(self.get_current_path().strip('/')) # e.g. "__workspace__/my_task"
        if str(current).endswith("/segments"):
             task_folder_name = current.parent.name
        else:
             task_folder_name = current.name
             
        task_folder = wm.workspace_root / task_folder_name
        if not task_folder.exists():
             print("Error: Could not locate task workspace.")
             return
             
        # Check completion
        segments_dir = task_folder / "segments"
        if segments_dir.exists():
            missing = list(segments_dir.glob("*.missing"))
            if missing:
                print(f"Error: Cannot finalize. {len(missing)} parts are missing.")
                return
        
        # Load manifest for filename
        manifest_path = task_folder / "task.manifest.json"
        filename = "output.bin"
        if manifest_path.exists():
             try:
                 import json
                 with open(manifest_path, 'r', encoding='utf-8') as f:
                     m = json.load(f)
                     filename = m.get('filename', 'output.bin')
             except: pass
        
        data_part = task_folder / "data.part"
        if not data_part.exists():
             print("Error: data.part missing.")
             return
        
        # Determine Target
        if destination == "exported":
            # If --final but no destination, default to downloads
            from dlm.bootstrap import get_project_root
            target_dir = get_project_root() / "downloads"
            target_dir.mkdir(exist_ok=True)
            target_file = target_dir / filename
        else:
            target_dir = Path(destination)
            if not target_dir.is_absolute():
                 target_dir = Path.cwd() / destination
            target_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_dir / filename
        
        try:
             import shutil
             print(f"Finalizing task '{task_folder_name}'...")
             shutil.move(str(data_part), str(target_file))
             print(f"✅ Exported to: {target_file}")
             print("Workspace is now empty of data. auto-cleanup suggested via 'rm .'")
        except Exception as e:
             print(f"Error exporting: {e}")

    def do_vls(self, arg):
        """Alias for vocals."""
        self.do_vocals(arg)

    def do_vocals(self, arg):
        """Separate vocals from audio/video files (Background Supported).
        
        Usage: 
          vocals <file|folder> [--gpu] [--all]
          vocals --monitor (or --v)
        
        Options:
          --gpu      Use GPU for faster processing (requires CUDA)
          --all      Keep both Vocals and Instrumental (default: Vocals Only)
          --monitor  Open live queue monitor
          --v        Alias for --monitor
        """
        if is_mobile_env():
            print("\nError: Vocal separation is disabled on mobile devices.")
            return

        parts = shlex.split(arg, posix=False)
        path_arg = None
        use_gpu = False
        keep_all = False
        monitor_only = False
        
        # Parse flags
        for p in parts:
            if p == '--gpu': use_gpu = True
            elif p in ['--all', '-a']: keep_all = True
            elif p in ['--monitor', '--v']: monitor_only = True
            elif not p.startswith('--'): path_arg = p.strip('"').strip("'")
            
        # 1. Monitor Mode
        if monitor_only:
            self._monitor_vocals()
            return

        # 2. Add New Task(s)
        if not path_arg:
             # Try picker
             gui_ok, picker_path = try_file_picker()
             if gui_ok and picker_path: path_arg = picker_path
             else:
                 print("Usage: vocals <file|folder> [options]")
                 return

        target_path = Path(path_arg)
        if not target_path.exists():
            print(f"Error: Path not found: {target_path}")
            return
            
        targets = []
        if target_path.is_file():
            targets.append(target_path)
        elif target_path.is_dir():
            # Batch process folder
            print(f"Scanning folder: {target_path}")
            valid_exts = ['.mp3', '.wav', '.flac', '.m4a', '.mp4', '.mkv', '.avi', '.mov']
            for f in target_path.iterdir():
                if f.is_file() and f.suffix.lower() in valid_exts:
                    # Skip existing outputs
                    if "_vocals" in f.name or "_no_music" in f.name or "_clean" in f.name:
                        continue
                    targets.append(f)
            
            if not targets:
                print("No valid media files found in folder.")
                return
            print(f"Found {len(targets)} files to process.")
        
        # Enqueue with keep_all flag
        for t in targets:
            self.service.queue_vocals(t, use_gpu=use_gpu, keep_all=keep_all)

        print(f"Queued {len(targets)} task(s).")
        
        # Auto-start monitor
        self._monitor_vocals()

    def _monitor_vocals(self):
        """Live monitor for vocals queue (Persistent)."""
        import time
        import sys
        
        print("\n[Vocals Monitor] (Ctrl+C to exit check, process continues)")
        time.sleep(1) # Brief pause
        
        print("\033[2J", end="") # Clear once on start
        try:
            while True:
                # Use ANSI Home \033[H instead of Clear \033[2J to prevent flicker
                sys.stdout.write("\033[H")
                
                queue = self.service.get_vocals_queue()
                # Fix Active Count: Include anything not done/failed
                pending = [t for t in queue if t['status'] not in ['done', 'failed']]
                
                print("=" * 60)
                print(f" VOCALS QUEUE MONITOR ({len(pending)} Active)")
                print("=" * 60)
                
                if not queue:
                    print("\n  [Waiting for tasks...]")
                    print("  (Run 'vocals <file>' in another terminal to add tasks)")
                
                else:
                    print(f"{'Filename':<30} {'Status':<15} {'Progress'}")
                    print("-" * 60)
                    
                    # Show last 10 tasks to avoid overflow, or scroll?
                    # Let's show all pending + last 5 done/failed
                    display_list = list(queue)
                    if len(display_list) > 15:
                        # Keep all pending, prune old done
                        done = [t for t in display_list if t['status'] not in ['queued', 'processing']]
                        active = [t for t in display_list if t['status'] in ['queued', 'processing']]
                        display_list = active + done[-5:]
                    
                    for task in display_list:
                         name = truncate_middle(task['filename'], 28)
                         status = truncate_middle(task['status'], 15)
                         progress = task.get('progress', 0)
                         
                         bar = ""
                         if task['status'] in ['queued']:
                             bar = "[Queued]"
                         elif task['status'] == 'done':
                             bar = "[Done]"
                         elif task['status'] == 'failed':
                             bar = f"[Failed] {truncate_middle(str(task.get('error','')), 15)}"
                         else:
                             # Processing (any other state like 'extracting audio...', 'processing')
                             filled = int(progress // 5)
                             bar = f"[{'#' * filled}{'.' * (20-filled)}] {progress}%"
                             
                         print(f"{name:<30} {status:<15} {bar}")

                print("-" * 60)
                print("(Ctrl+C to Exit Monitor Mode)")
                # Clear rest of screen to handle shrinking output
                sys.stdout.write("\033[J")
                sys.stdout.flush()
                
                time.sleep(1)
                
        except KeyboardInterrupt:
            # Show full errors for failed tasks on exit
            queue = self.service.get_vocals_queue()
            failed = [t for t in queue if t['status'] == 'failed']
            
            print("\nMonitor exited. (Press Ctrl+C again to exit DLM)")
            
            if failed:
                print("\n" + "="*60)
                print(f"FAILED TASKS REPORT ({len(failed)})")
                print("="*60)
                for t in failed:
                    print(f"File: {t['filename']}")
                    print(f"Error:\n{t.get('error', 'Unknown Error')}")
                    print("-" * 60)
            print("")

    
    


    def do_monitor(self, arg):
        """Live monitor: monitor"""
        should_list = TUI(self.bus).monitor()
        if should_list:
            # Auto-run ls after monitor exit
            self.do_cls("")
            self.do_ls("")
    
    def do_cls(self, arg):
        """Clear the screen"""
        os.system('cls' if os.name == 'nt' else 'clear')

    def do_exit(self, arg):
        """Exit the shell"""
        print("Bye!")
        return True
    
    do_quit = do_exit
    do_EOF = do_exit
    def do_config(self, arg):
        """
        Manage configuration settings securely.
        Usage: config <key> [value]
        
        Examples:
          config limit 2      (Set simultaneous download limit to 2)
          config limit        (Show current limit)
        """
        args = arg.split()
        if not args:
            print("\nCurrent Configuration:")
            print("-" * 30)
            known_keys = ["concurrency_limit", "spotify_client_id", "default_output_dir"]
            for k in known_keys:
                v = self.service.config.get(k)
                friendly_name = k.replace("concurrency_limit", "limit").replace("default_output_dir", "output")
                print(f"{friendly_name:<20} = {v if v is not None else 'Not Set'}")
            print("\nUsage: config <key> [value]")
            return

        key = args[0].lower()
        
        # Mappings for user friendly keys
        key_map = {
            "limit": "concurrency_limit",
            "parallel": "concurrency_limit",
            "output": "default_output_dir",
            "path": "default_output_dir"
        }
        
        real_key = key_map.get(key, key)
        
        # Special Handler: config spotify
        if real_key == "spotify":
            self._setup_spotify_interactive()
            return
            
        if len(args) == 1:
            # Get Value
            if not self.service.config:
                print("Configuration service not available.")
                return
                
            val = self.service.config.get(real_key)
            if val is None:
                # Defaults
                if real_key == "concurrency_limit": val = 1
                else: val = "Not Set"
                
            print(f"{key} = {val}")
        else:
            # Set Value
            val = args[1]
            if not self.service.config:
                print("Configuration service not available.")
                return
            
            # Type safety
            if real_key == "concurrency_limit":
                if not val.isdigit() or int(val) < 1:
                    print("Error: limit must be a number >= 1")
                    return
                val = int(val)
            
            if real_key == "default_output_dir":
                # Basic normalization and existence check if not empty
                if val.lower() == "none" or val.lower() == "default":
                    val = None
                else:
                    try:
                        p = Path(val).resolve()
                        val = str(p)
                    except Exception as e:
                        print(f"Error: Invalid path: {e}")
                        return
                
            self.service.config.set(real_key, val)
            print(f"Set {key} = {val}")

    def _setup_spotify_interactive(self):
        """Interactive setup for Spotify credentials."""
        print("\nSpotify Configuration Setup")
        print("---------------------------")
        print("To get your credentials:")
        print("1. Go to https://developer.spotify.com/dashboard")
        print("2. Log in and click 'Create App'")
        print("3. Copy Client ID and Client Secret\n")
        
        try:
            client_id = input("Enter Spotify Client ID: ").strip()
            if not client_id:
                print("Cancelled.")
                return
                
            client_secret = input("Enter Spotify Client Secret: ").strip()
            if not client_secret:
                print("Cancelled.")
                return
            
            # Save securely
            self.service.config.set("spotify_client_id", client_id)
            self.service.config.set("spotify_client_secret", client_secret)
            
            print("\nCredentials saved successfully!")
            print("Spotify features are now enabled.")
            
        except KeyboardInterrupt:
            print("\nCancelled.")
            return

    def do_browser(self, arg):
        """Start the browser capture mode: browser"""
        if is_mobile_env():
            print("\nError: The 'browser' command is not supported on Termux/Android.")
            print("Reason: Playwright requires a desktop browser engine which is not available natively in this environment.")
            return

        from dlm.app.commands import BrowserCommand
        self.bus.handle(BrowserCommand())

    def do_verify(self, arg):
        """
        Verify torrent file integrity by checking actual files on disk.
        Usage: verify <torrent_file> [data_path]
        Alias: vr
        
        Examples:
          verify file.torrent                    - Verify using torrent file (auto-detect data path)
          verify file.torrent C:\\path\\to\\data   - Verify specific data location
          verify .                               - Auto-find torrents in workspace
        """
        try:
            if not arg.strip():
                print("Usage: verify <torrent_file> [data_path]")
                print("\nExamples:")
                print("  vr file.torrent                    - Auto-detect data location")
                print("  vr file.torrent C:\\path\\to\\data   - Specify data location")
                print("  vr .                               - Scan workspace for torrents")
                return
            
            # Parse arguments
            args = shlex.split(arg.strip())
            
            if len(args) == 0:
                print("Error: No arguments provided")
                return
            
            torrent_arg = args[0]
            data_path_arg = args[1] if len(args) > 1 else None
            
            # Handle '.' to mean current workspace folder
            if torrent_arg == '.':
                workspace_root = self.service.download_dir.parent / "__workspace__"
                
                # Try to find .torrent files in workspace
                torrent_files = list(workspace_root.glob("**/*.torrent"))
                
                if not torrent_files:
                    print("No .torrent files found in workspace.")
                    print(f"Workspace path: {workspace_root}")
                    return
                
                print(f"Found {len(torrent_files)} torrent file(s) in workspace:")
                for i, tf in enumerate(torrent_files, 1):
                    print(f"  [{i}] {tf.name}")
                
                # Verify all found torrents
                for torrent_path in torrent_files:
                    self._verify_torrent_file(torrent_path, workspace_root)
            else:
                # Direct path provided
                torrent_path = Path(torrent_arg)
                
                if not torrent_path.exists():
                    print(f"Error: Torrent file not found: {torrent_path}")
                    return
                
                if not torrent_path.is_file():
                    print(f"Error: Not a file: {torrent_path}")
                    return
                
                # Determine save_path
                if data_path_arg:
                    save_path = Path(data_path_arg)
                    if not save_path.exists():
                        print(f"Error: Data path not found: {save_path}")
                        return
                else:
                    # Auto-detect: try workspace first, then torrent file's parent
                    workspace_root = self.service.download_dir.parent / "__workspace__"
                    if workspace_root.exists():
                        save_path = workspace_root
                    else:
                        save_path = torrent_path.parent
                
                print(f"Torrent file: {torrent_path}")
                print(f"Data location: {save_path}")
                
                self._verify_torrent_file(torrent_path, save_path)
        
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()

    def _verify_torrent_file(self, torrent_path: Path, save_path: Path):
        """Verify a single torrent file."""
        try:
            import libtorrent as lt
            import ctypes
            import os
            
            print(f"\n{'='*60}")
            print(f"Verifying: {torrent_path.name}")
            print(f"{'='*60}")
            
            # Load torrent info
            try:
                info = lt.torrent_info(str(torrent_path))
            except Exception as e:
                print(f"Error loading torrent: {e}")
                return
            
            total_pieces = info.num_pieces()
            total_size = info.total_size()
            piece_length = info.piece_length()
            
            print(f"Torrent Name: {info.name()}")
            print(f"Total Size: {self._format_size(total_size)}")
            print(f"Pieces: {total_pieces} × {self._format_size(piece_length)}")
            print(f"Files: {info.num_files()}")
            
            # Create a temporary session to check pieces
            session = lt.session({'alert_mask': lt.alert.category_t.error_notification})
            
            params = {
                'save_path': str(save_path),
                'ti': info,
                'flags': lt.add_torrent_params_flags_t.flag_seed_mode  # Don't download, just check
            }
            
            handle = session.add_torrent(params)
            
            # Force recheck
            handle.force_recheck()
            
            # Wait for recheck to complete
            print("\nRechecking files...")
            for _ in range(60):  # Wait up to 30 seconds
                st = handle.status()
                if st.state == lt.torrent_status.seeding or st.state == lt.torrent_status.finished:
                    break
                if st.progress >= 1.0:
                    break
                time.sleep(0.5)
            
            # Get final status
            st = handle.status()
            bitfield = st.pieces
            
            completed_pieces = sum(1 for i in range(len(bitfield)) if bitfield[i])
            completion = (completed_pieces / total_pieces * 100) if total_pieces > 0 else 0
            
            print(f"\n{'─'*60}")
            print(f"VERIFICATION RESULTS:")
            print(f"{'─'*60}")
            
            status_icon = "✓" if completion >= 100 else "⚠" if completion > 0 else "✗"
            print(f"{status_icon} Completion: {completion:.1f}% ({completed_pieces}/{total_pieces} pieces)")
            
            # Calculate actual disk usage
            actual_disk_usage = 0
            allocated_size = 0
            
            print(f"\nFiles:")
            for file_idx in range(info.num_files()):
                file_info = info.file_at(file_idx)
                file_path = save_path / file_info.path
                file_size = file_info.size
                
                allocated_size += file_size
                
                if file_path.exists():
                    # Get actual size on disk (Windows)
                    if os.name == 'nt':
                        try:
                            size_high = ctypes.c_ulonglong(0)
                            size_low = ctypes.windll.kernel32.GetCompressedFileSizeW(
                                str(file_path),
                                ctypes.pointer(size_high)
                            )
                            if size_low != 0xFFFFFFFF:
                                disk_size = (size_high.value << 32) + size_low
                            else:
                                disk_size = file_path.stat().st_size
                        except:
                            disk_size = file_path.stat().st_size
                    else:
                        stat_info = file_path.stat()
                        disk_size = stat_info.st_blocks * 512
                    
                    actual_disk_usage += disk_size
                    
                    exists_icon = "✓"
                    size_str = f"{self._format_size(disk_size)} / {self._format_size(file_size)}"
                else:
                    exists_icon = "✗"
                    size_str = f"MISSING ({self._format_size(file_size)})"
                
                print(f"  {exists_icon} {file_info.path} - {size_str}")
            
            print(f"\n{'─'*60}")
            print(f"DISK USAGE:")
            print(f"{'─'*60}")
            print(f"Expected Size: {self._format_size(allocated_size)}")
            print(f"Actual Disk Usage: {self._format_size(actual_disk_usage)}")
            
            if allocated_size > actual_disk_usage * 1.1:
                print(f"⚠ WARNING: Sparse file detected (allocated but not fully written)")
                print(f"   Missing: ~{self._format_size(allocated_size - actual_disk_usage)}")
            
            if actual_disk_usage > 0:
                print(f"\n💡 NOTES ON SPLIT DOWNLOADS:")
                print(f"   - Piece Alignment: Torrent pieces ({self._format_size(info.piece_length())}) can span multiple files.")
                print(f"     This causes adjacent files (like data.bin) to be touched even if you only")
                print(f"     requested data1.bin. This is expected libtorrent behavior.")
                print(f"   - Progress Discrepancy: The UI shows 'Verified Progress' (Green Bar).")
                print(f"     'Actual Disk Usage' may be higher due to unverified data being written")
                print(f"     to disk before the entire piece is validated.")
            
            # Clean up
            session.remove_torrent(handle)
            
        except Exception as e:
            print(f"Error verifying torrent: {e}")
            import traceback
            traceback.print_exc()




