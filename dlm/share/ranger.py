"""Terminal file browser for Share Phase 2 with ranger-style navigation."""

import os
from pathlib import Path
from typing import List, Callable, Optional
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.widgets import Frame
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.filters import Condition


class RangerBrowser:
    """A simple terminal-based file browser inspired by ranger."""
    
    def __init__(self, on_add: Callable[[Path], int]):
        self.on_add = on_add
        self.current_dir = Path.cwd()
        self.selected_index = 0
        self.items: List[Path] = []
        self.history: List[Path] = []
        self.last_added_msg: Optional[str] = None
        self.exit_code: bool = False
        
        # Folder Unit Prompt State
        self.prompt_active = False
        self.prompt_target: Optional[Path] = None
        
        self._refresh_items()
        
        self.style = Style.from_dict({
            'directory': '#00aaff bold',
            'file': '#ffffff',
            'selected': '#00ff00 bold reverse',
            'header': '#00ff00 bold',
            'footer': '#aaaaaa italic',
            'confirm': '#00ff00 bold',
        })
        
        self.kb = KeyBindings()
        self._setup_keybindings()
        
    def _refresh_items(self):
        """Reload items in current directory."""
        try:
            # Sort: Directories first, then files alphabetically
            all_items = sorted(list(self.current_dir.iterdir()), key=lambda x: (not x.is_dir(), x.name.lower()))
            self.items = all_items
        except PermissionError:
            self.items = []
        
        # Keep selection in bounds
        if not self.items:
            self.selected_index = 0
        elif self.selected_index >= len(self.items):
            self.selected_index = len(self.items) - 1

    def _get_header_text(self):
        return HTML(f"  <header>Browsing:</header> {self.current_dir.resolve()}\n")

    def _get_footer_text(self):
        if self.prompt_active:
            msg = f"\n  <confirm>Send '{self.prompt_target.name}' as a Folder Unit? [y/n]</confirm>\n"
            return HTML(msg)
            
        msg = f"\n  <footer-msg>{self.last_added_msg if self.last_added_msg else ''}</footer-msg>\n"
        msg += "  <b>[Enter]</b> Add  <b>[→]</b> Enter Dir  <b>[←/Backspace]</b> Back  <b>[q]</b> Done"
        return HTML(msg)

    def _get_items_text(self):
        if not self.items:
            return HTML("  <i>(Directory empty or inaccessible)</i>")
            
        result = []
        for i, item in enumerate(self.items):
            is_selected = (i == self.selected_index)
            prefix = "> " if is_selected else "  "
            style = "selected" if is_selected else ("directory" if item.is_dir() else "file")
            
            # Label with type marker
            suffix = "/" if item.is_dir() else ""
            line = f"{prefix}<{style}>{item.name}{suffix}</{style}>"
            result.append(line)
            
        return HTML("\n".join(result))

    def _setup_keybindings(self):
        @self.kb.add('up')
        def _(event):
            if self.selected_index > 0:
                self.selected_index -= 1
            else:
                self.selected_index = len(self.items) - 1 if self.items else 0

        @self.kb.add('down')
        def _(event):
            if self.selected_index < len(self.items) - 1:
                self.selected_index += 1
            else:
                self.selected_index = 0

        @self.kb.add('enter')
        def _(event):
            if self.prompt_active or not self.items:
                return
                
            selected = self.items[self.selected_index]
            if selected.is_dir():
                self.prompt_active = True
                self.prompt_target = selected
            else:
                count = self.on_add(selected, False)
                self.last_added_msg = f"✔ Added file: {selected.name}"

        @self.kb.add('y', filter=Condition(lambda: self.prompt_active))
        def _(event):
            count = self.on_add(self.prompt_target, True)
            self.last_added_msg = f"✔ Added folder unit: {self.prompt_target.name}"
            self.prompt_active = False
            self.prompt_target = None

        @self.kb.add('n', filter=Condition(lambda: self.prompt_active))
        def _(event):
            # If no, enter directory instead? Or just cancel?
            # User workflow: Enter -> Prompt "Send Folder?". No -> Do nothing (allow user to Right Arrow into it if they want)
            # OR logic: No -> Enter folder?
            # Let's stick to: No -> Cancel prompt. User can use Right Arrow to browse.
            self.prompt_active = False
            self.prompt_target = None

        @self.kb.add('right')
        def _(event):
            if not self.items:
                return
            selected = self.items[self.selected_index]
            if selected.is_dir():
                self.history.append(self.current_dir)
                self.current_dir = selected
                self.selected_index = 0
                self._refresh_items()

        @self.kb.add('left')
        @self.kb.add('backspace')
        def _(event):
            if self.history:
                self.current_dir = self.history.pop()
            else:
                parent = self.current_dir.parent
                if parent != self.current_dir: # Not root
                    self.current_dir = parent
            self.selected_index = 0
            self._refresh_items()

        @self.kb.add('q')
        def _(event):
            event.app.exit()

    def run(self):
        """Run the browser application."""
        layout = Layout(
            HSplit([
                Window(content=FormattedTextControl(self._get_header_text), height=2),
                Frame(
                    Window(content=FormattedTextControl(self._get_items_text), scroll_offsets=True),
                    title="Files"
                ),
                Window(content=FormattedTextControl(self._get_footer_text), height=3)
            ])
        )
        
        app = Application(
            layout=layout,
            key_bindings=self.kb,
            style=self.style,
            full_screen=True
        )
        
        app.run()


def pick_with_ranger(on_add_callback: Callable[[Path, bool], int]):
    """Helper to launch ranger browser."""
    browser = RangerBrowser(on_add_callback)
    browser.run()
