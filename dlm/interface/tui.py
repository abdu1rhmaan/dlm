import sys
import time
import shutil
import os
from dlm.app.commands import CommandBus, ListDownloads

class TUI:
    def __init__(self, bus: CommandBus):
        self.bus = bus
        self._persistence_cache = {} # {uuid: (last_record, timestamp)}
        self._prev_active_ids = set()

    def monitor(self):
        """Live monitor loop with stable, flicker-free rendering."""
        # Initial clear to start fresh
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        
        try:
            while True:
                # 1. Fetch State (Global)
                all_downloads = self.bus.handle(ListDownloads(recursive=True))
                now = time.time()
                
                # 2. Logic: Process Active and Transitions
                active_list = []
                current_active_ids = set()
                
                for d in all_downloads:
                    status = d['state']
                    uid = d['id']
                    
                    if status in ['DOWNLOADING', 'INITIALIZING']:
                        active_list.append(d)
                        current_active_ids.add(uid)
                        self._persistence_cache.pop(uid, None)
                    elif uid in self._prev_active_ids and status in ['COMPLETED', 'FAILED']:
                        # Transitioned! Start 3s timer
                        self._persistence_cache[uid] = (d, now)
                    else:
                        # If it's in all_downloads but NOT finished and NOT active (e.g. QUEUED/RETRYING)
                        # We MUST clear it from persistence cache to avoid showing stale 100% state
                        self._persistence_cache.pop(uid, None)

                self._prev_active_ids = current_active_ids

                # Combine list (ONLY active or recently finished)
                display_list = active_list[:]
                existing_disp_ids = {d['id'] for d in display_list}
                for uid, (rec, ts) in list(self._persistence_cache.items()):
                    if now - ts < 1.5:
                        if uid not in existing_disp_ids:
                            display_list.append(rec)
                    else:
                        del self._persistence_cache[uid]
                
                display_list.sort(key=lambda x: x.get('index', 0))

                # Dynamic Padding
                max_name_len = 10
                if display_list:
                    max_name_len = max(len(d.get('filename', '')) for d in display_list)
                
                # 3. Render
                self._render_active_tasks(display_list, max_name_len)
                time.sleep(0.25)
        except KeyboardInterrupt:
            # Clear screen on exit
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            return True 

    def monitor_task(self, task_id: str, custom_header=None):
        """
        Monitor a specific task until completion or Ctrl+C.
        custom_header: List[str] OR Callable[[], List[str]]
        """
        # Initial clear
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        
        try:
            while True:
                all_downloads = self.bus.handle(ListDownloads(recursive=True))
                # Filter for our task
                target = next((d for d in all_downloads if d['id'] == task_id), None)
                
                # Resolve Header
                header_lines = custom_header() if callable(custom_header) else custom_header

                if not target:
                    print("Task not found or removed.")
                    break
                
                # Check completion
                if target['state'] in ['COMPLETED', 'FAILED', 'CANCELLED']:
                    # Final Render
                    self._render_active_tasks([target], len(target.get('filename','')), custom_header=header_lines)
                    
                    msg = f"Task {target['state']}."
                    if target['state'] == 'FAILED':
                         err = target.get('error', 'Unknown Error')
                         msg += f" Reason: {err}"
                         print(f"\n\033[1;31m{msg}\033[0m")
                    else:
                         print(f"\n{msg} Press Ctrl+C to exit.")
                    
                    # Keep monitoring for a moment or exit?
                    # User likely wants to see the "Completed" state.
                    # But we break to return to shell.
                    # Wait a bit for user to see.
                    # Actually, let's wait for Ctrl+C if completed, or just exit?
                    # User feedback: "As soon as it completes... green". 
                    # If I return immediately, prompt overwrites it. 
                    # Let's wait for input? No, monitor_task should block.
                    # Let's just return.
                    break

                # Render
                self._render_active_tasks([target], len(target.get('filename','')), custom_header=header_lines)
                time.sleep(0.25)
        except KeyboardInterrupt:
            # Clear screen
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            return True 

    def _render_active_tasks(self, downloads, max_name_len, custom_header: list = None):
        term_width = shutil.get_terminal_size((80, 20)).columns
        
        if custom_header:
             output = ["\033[H"] + [f"{line}\033[K\n" for line in custom_header] + ["─" * min(term_width, 60) + "\033[K\n"]
        else:
             output = ["\033[H", "\033[1;36m[ DLM MONITOR ]\033[0m | Press Ctrl+C to return to shell\n", "─" * min(term_width, 60) + "\033[K\n"]

        if not downloads:
            output.append("\nNo active downloads.\033[K\n")
        else:
            for d in downloads:
                line = self._format_download_line(d, term_width, max_name_len)
                output.append(f"{line}\033[K\n") # Clear to end of line to prevent ghosting
        
        # Clear everything below the current output block
        output.append("\033[J")
        sys.stdout.write("".join(output))
        sys.stdout.flush()

    def _format_download_line(self, d: dict, term_width: int, max_name_len: int) -> str:
        # Colors (RGB 24-bit)
        CLR_COMPLETED = "\033[38;2;114;156;31m" # Lime #729C1F
        CLR_DOWNLOADING = "\033[38;2;249;38;114m" # Pink #F92672
        CLR_WAITING = "\033[38;2;0;170;255m"    # Blue/Cyan
        CLR_TRACK = "\033[38;2;24;24;24m" # Track #181818
        CLR_RESET = "\033[0m"
        
        state = d.get('state')
        if state == 'COMPLETED': main_color = CLR_COMPLETED
        elif state == 'WAITING': main_color = CLR_WAITING
        else: main_color = CLR_DOWNLOADING
        
        filename = d.get('filename', 'Unknown')
        progress_str = d.get('progress', '0.0%')
        downloaded = d.get('downloaded', 0)
        total = d.get('total', 0)
        speed = d.get('speed', 0.0)
        
        def format_size(v):
            if v >= 1024**3: return f"{v/1024**3:.1f}G"
            if v >= 1024**2: return f"{v/1024**2:.1f}M"
            if v >= 1024: return f"{v/1024:.0f}K"
            return f"{v} B"
        
        # Stats Block
        is_active = state in ['DOWNLOADING', 'INITIALIZING']
        if total > 0:
            size_part = f"{format_size(downloaded)}/{format_size(total)}"
        else:
            # For torrents or streams where size is discovered during download
            size_part = f"{format_size(downloaded)}" if downloaded > 0 else "0 B"
        speed_part = f" | {speed/1024**2:.1f}MB/s" if speed > 1024**2 else (f" | {speed/1024:.0f}KB/s" if speed > 1024 else "")
        
        if state == 'COMPLETED': status_text = "Completed"
        elif state == 'WAITING': status_text = "» waiting"
        else: status_text = progress_str
        
        stats_part = f"{status_text:>11} | {size_part}{speed_part if is_active else ''}"
        
        # Title Block
        prefix = f"[#{d.get('index', '?')}] "
        name_cap = int(term_width * 0.40)
        effective_max = min(max_name_len, name_cap)
        if effective_max < 16: effective_max = 16
        
        short_title = filename if len(filename) <= effective_max else self._truncate_middle(filename, effective_max)
        title_block = short_title.ljust(effective_max)
        
        # Bar: Pink/Cyan Filling, ╸ Black Accent, Match-181818 Track
        bar_width = 15
        try: pct = float(progress_str.replace('%',''))
        except: pct = 0.0
        filled = int(pct/100 * bar_width) if (total > 0 or state == 'COMPLETED' or pct > 0) else 0
        
        bar_parts = []
        for i in range(bar_width):
            if i < filled:
                bar_parts.append(f"{main_color}━")
            elif i == filled and pct < 100 and is_active:
                bar_parts.append(f"{main_color}╸") # Pointy tip in same color
            else:
                bar_parts.append(f"{CLR_TRACK}━")
        bar_render = "".join(bar_parts) + CLR_RESET
        
        # Line styling (Standard colors for text)
        return f"{prefix}{title_block} | {bar_render} | {stats_part}"

    def _truncate_middle(self, text: str, max_width: int) -> str:
        if len(text) <= max_width: return text
        if max_width < 5: return text[:max_width]
        last_dot = text.rfind('.')
        ext = text[last_dot:] if (last_dot != -1 and (len(text)-last_dot) < 6) else ""
        available = max_width - 1
        if len(ext) >= available: return text[:available] + "…"
        rem = available - len(ext)
        start = text[:rem//2 + rem%2]
        end = text[last_dot-rem//2:last_dot] if ext else text[-rem//2:]
        return f"{start}…{end}{ext}"
