"""Checklist-style TUI for DLM Feature Launcher."""

import os
import shutil
import subprocess
try:
    import curses
except ImportError:
    curses = None

from typing import List, Set


class LauncherTUI:
    """Checklist-style TUI for feature selection."""
    
    def __init__(self, features):
        self.features = features
        self.selected_ids: Set[str] = {f.id for f in features if f.is_installed()}

    def run(self) -> List[str]:
        """Run the best available checklist UI. Returns list of selected feature IDs."""
        if shutil.which("whiptail"):
            return self._run_whiptail()
        elif shutil.which("dialog"):
            return self._run_dialog()
        elif curses:
            try:
                return self._run_curses()
            except Exception:
                return self._run_simple()
        else:
            return self._run_simple()

    def _run_whiptail(self) -> List[str]:
        return self._run_external("whiptail")

    def _run_dialog(self) -> List[str]:
        return self._run_external("dialog")

    def _run_external(self, cmd: str) -> List[str]:
        """Run whiptail or dialog checklist."""
        args = [
            cmd, "--title", "DLM Feature Manager",
            "--checklist", "\nChoose features to install/enable:\n(Space to toggle, Enter to confirm)",
            "20", "60", "10"
        ]
        
        for f in self.features:
            status = "ON" if f.id in self.selected_ids else "OFF"
            label = f"{f.name} ({f.estimated_size})"
            if f.is_installed():
                label = f"{f.name} (installed)"
            args.extend([f.id, label, status])

        try:
            # External dialogs write selection to stderr
            result = subprocess.run(args, stderr=subprocess.PIPE, text=True)
            if result.returncode == 0:
                output = result.stderr.strip().replace('"', '')
                return output.split()
            return []
        except Exception:
            return self._run_simple()

    def _run_curses(self) -> List[str]:
        """Fallback curses-based checklist."""
        if not curses: return self._run_simple()
        try:
            return curses.wrapper(self._curses_main)
        except Exception:
            return self._run_simple()

    def _curses_main(self, stdscr) -> List[str]:
        curses.curs_set(0)
        stdscr.keypad(True)
        current_row = 0
        
        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            
            stdscr.addstr(1, 2, "DLM Feature Manager", curses.A_BOLD | curses.A_UNDERLINE)
            stdscr.addstr(2, 2, "Use arrows to move, Space to toggle, Enter to confirm, Q to exit")
            
            for i, f in enumerate(self.features):
                x = 4
                y = 4 + i
                if y >= h - 1: break 
                
                check = "[*]" if f.id in self.selected_ids else "[ ]"
                status = "(installed)" if f.is_installed() else f"({f.estimated_size})"
                line = f"{check} {f.name:<20} {status}"
                
                if i == current_row:
                    stdscr.attron(curses.A_REVERSE)
                    stdscr.addstr(y, x, f"> {line}")
                    stdscr.attroff(curses.A_REVERSE)
                else:
                    stdscr.addstr(y, x, f"  {line}")

            stdscr.refresh()
            
            key = stdscr.getch()
            if key == curses.KEY_UP and current_row > 0:
                current_row -= 1
            elif key == curses.KEY_DOWN and current_row < len(self.features) - 1:
                current_row += 1
            elif key == ord(' '):
                f_id = self.features[current_row].id
                if f_id in self.selected_ids:
                    if f_id != "downloader": # Core always selected
                        self.selected_ids.remove(f_id)
                else:
                    self.selected_ids.add(f_id)
            elif key in (10, 13): # Enter
                return list(self.selected_ids)
            elif key in (ord('q'), ord('Q'), 27): # Q or Esc
                return []

    def _run_simple(self) -> List[str]:
        """Simple text-loop fallback for environments without curses."""
        import os
        while True:
            # Clear screen properly
            os.system('cls' if os.name == 'nt' else 'clear')
            
            print("=== DLM Feature Manager ===")
            print("Type number to toggle, or 'done' to finish.\n")
            
            for i, f in enumerate(self.features):
                mark = "[x]" if f.id in self.selected_ids else "[ ]"
                status = "(installed)" if f.is_installed() else ""
                print(f" {i+1}. {mark} {f.name} {status}")
            
            print("\nCommands: [Number] to toggle, [d]one to confirm, [q]uit")
            choice = input("Select> ").strip().lower()
            
            if choice in ('d', 'done', ''):
                return list(self.selected_ids)
            if choice in ('q', 'quit'):
                return []
            
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(self.features):
                    f_id = self.features[idx].id
                    if f_id == 'downloader':
                        print("Core module cannot be disabled.")
                        import time; time.sleep(1) 
                    else:
                        if f_id in self.selected_ids:
                            self.selected_ids.remove(f_id)
                        else:
                            self.selected_ids.add(f_id)
            else:
                pass
