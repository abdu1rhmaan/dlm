from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.widgets import Frame, TextArea, Label, Box
from prompt_toolkit.layout.containers import Window, HSplit, VSplit, FloatContainer, Float, WindowAlign, ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style
from prompt_toolkit.filters import Condition

from .registry import FEATURES, get_feature
from .models import FeatureStatus
from .installer import FeatureInstaller
import asyncio
import threading
import time

class FeatureManagerTUI:
    def __init__(self):
        # 1. Group Features by Category
        self.features_by_cat = {}
        for f in FEATURES:
            if f.category not in self.features_by_cat:
                self.features_by_cat[f.category] = []
            self.features_by_cat[f.category].append(f)
            
        # Flatten for navigation, but keep headers in mind
        self.flat_items = [] # List of Feature or ("Header", "Category Name")
        for cat, feats in self.features_by_cat.items():
            self.flat_items.append(("Header", cat))
            self.flat_items.extend(feats)
            
        self.selected_index = 1 # Skip first header
        
        self.statuses = {} 
        self.refresh_statuses()
        
        self.bindings = KeyBindings()
        self._setup_bindings()
        
        # --- UI STATE ---
        self.show_dialog = False
        self.dialog_title = ""
        self.dialog_lines = []
        self.is_installing = False
        self.spinner_idx = 0
        
        # -- LAYOUT --
        self.title_control = FormattedTextControl(" DLM MODULE MANAGER ")
        self.title_window = Window(content=self.title_control, align=WindowAlign.CENTER, height=1, style="class:title")
        
        self.list_control = FormattedTextControl(self._get_list_text)
        self.list_window = Window(content=self.list_control)
        
        self.info_control = FormattedTextControl(self._get_info_text)
        self.info_window = Window(content=self.info_control, height=3, style="class:info")
        
        self.footer_control = FormattedTextControl(" [↑/↓] Navigate  [Space] Select  [i] Install  [u] Uninstall  [q] Quit ")
        self.footer_window = Window(content=self.footer_control, align=WindowAlign.CENTER, height=1, style="class:footer")

        # Dialog (Float)
        self.dialog_control = FormattedTextControl(self._get_dialog_text)
        self.dialog_window = Frame(
            title=lambda: self.dialog_title,
            style="class:dialog",
            width=D(min=30, max=60),
            height=D(min=8, max=20)
        )
        
        self.root_container = FloatContainer(
            content=HSplit([
                self.title_window,
                Frame(self.list_window),  # Main content framed
                self.info_window,
                self.footer_window
            ]),
            floats=[
                Float(content=ConditionalContainer(
                    content=self.dialog_window,
                    filter=Condition(lambda: self.show_dialog)
                ))
            ]
        )
        
        self.layout = Layout(self.root_container)
        
        self.style = Style.from_dict({
            'title': '#00ff00 bold reverse',
            'footer': '#cccccc bg:#222222',
            'header': '#ffffff bold underline',
            'selected': 'reverse',
            'installed': '#00ff00',
            'missing': '#ff4444',
            'partial': '#ffff00',
            'dialog': 'bg:#333333 #ffffff border:#00ff00',
            'info': '#888888 italic'
        })
        
        self.app = Application(layout=self.layout, key_bindings=self.bindings, style=self.style, full_screen=True, refresh_interval=0.1)

        # Start background spinner task
        # We can use the refresh_interval to update spinner index in _get_text, simpler.
        
    def refresh_statuses(self):
        for f in FEATURES:
            self.statuses[f.id] = f.check_status()

    def _get_list_text(self):
        result = []
        # Update spinner frame if installing
        if self.is_installing:
            self.spinner_idx = (self.spinner_idx + 1) % 4
            
        for i, item in enumerate(self.flat_items):
            is_selected = (i == self.selected_index)
            
            if isinstance(item, tuple) and item[0] == "Header":
                # Header Row
                result.append(("", "\n"))
                result.append(("class:header", f" {item[1].upper()} \n"))
                continue
                
            # Feature Row
            f = item
            status = self.statuses.get(f.id, FeatureStatus.MISSING)
            
            if status == FeatureStatus.INSTALLED:
                icon = "MATCH"
                style = "class:installed"
                st_text = "[INSTALLED]"
            elif status == FeatureStatus.PARTIAL:
                icon = "WARN"
                style = "class:partial"
                st_text = "[ BROKEN  ]"
            else:
                icon = "MISS"
                style = "class:missing"
                st_text = "[ MISSING ]"
                
            prefix = " > " if is_selected else "   "
            row_style = "class:selected" if is_selected else ""
            
            # Name padding
            name_pad = f"{f.name:<25}"
            
            # Row content
            # (style, text)
            line = [
                ("", prefix),
                (style, st_text + " "),
                ("", name_pad)
            ]
            
            if is_selected:
                line = [(row_style, t[1]) for t in line]
                
            result.extend(line)
            result.append(("", "\n"))
            
        return result

    def _get_info_text(self):
        item = self.flat_items[self.selected_index]
        if isinstance(item, tuple): return ""
        desc = item.description
        dep_count = len(item.dependencies)
        return f"\n  {desc}\n  Dependencies: {dep_count}"

    def _get_dialog_text(self):
        s = "|/-\\"
        spinner = s[self.spinner_idx] if self.is_installing else " "
        
        text = "\n".join(self.dialog_lines[-7:])
        if self.is_installing:
             return f"\n {spinner} Working...\n\n{text}"
        else:
             return f"\n{text}\n\n (Press Space to Close)"

    def _setup_bindings(self):
        @self.bindings.add('q', filter=Condition(lambda: not self.show_dialog))
        def _(event):
            event.app.exit()

        @self.bindings.add('up', filter=Condition(lambda: not self.show_dialog))
        def _(event):
            self._move_selection(-1)

        @self.bindings.add('down', filter=Condition(lambda: not self.show_dialog))
        def _(event):
            self._move_selection(1)

        @self.bindings.add('space')
        @self.bindings.add('enter')
        def _(event):
            if self.show_dialog and not self.is_installing:
                self.show_dialog = False
                return

            if self.show_dialog: return # Ignore if busy
            
            item = self.flat_items[self.selected_index]
            if isinstance(item, tuple): return # Cannot click header
            
            feature = item
            status = self.statuses.get(feature.id)
            
            dep_lines = self._get_dependency_lines(feature)

            if status == FeatureStatus.INSTALLED:
                # Offer Toggle/Uninstall
                self.dialog_title = f"{feature.name}"
                
                base_lines = ["Status: INSTALLED"]
                if feature.is_core:
                     base_lines.append("(Core Feature - Cannot Uninstall)")
                else:
                     base_lines.append("Press 'u' to UNINSTALL.")
                
                base_lines.append("Press Space to close.")
                self.dialog_lines = base_lines + dep_lines 
                self.show_dialog = True
            else:
                # Show Info Dialog for Missing
                self.dialog_title = f"{feature.name}"
                self.dialog_lines = [
                    "Status: MISSING",
                    " ",
                    f"Description: {feature.description}",
                    " ",
                    "Press 'i' to INSTALL this feature.",
                    "Press Space to close."
                ] + dep_lines
                self.show_dialog = True

        @self.bindings.add('u')
        def _(event):
            item = self.flat_items[self.selected_index]
            if isinstance(item, tuple): return
            
            feature = item
            
            if self.is_installing: return 
            
            # BLOCK CORE REMOVAL
            if feature.is_core:
                self.dialog_title = "Restricted Action"
                self.dialog_lines = [
                    f"'{feature.name}' is a Core System Feature.",
                    " ",
                    "It cannot be uninstalled as it is essential",
                    "for the application to function.",
                    " ",
                    "Press Space to close."
                ]
                self.show_dialog = True
                return

            status = self.statuses.get(feature.id)
            if status == FeatureStatus.INSTALLED:
                self._start_uninstall(feature)

        @self.bindings.add('i')
        def _(event):
            # Direct install shortcut
            item = self.flat_items[self.selected_index]
            if isinstance(item, tuple): return
            
            feature = item
            status = self.statuses.get(feature.id)
            
            if self.is_installing: return

            if status != FeatureStatus.INSTALLED:
                self._start_install(feature)

    def _move_selection(self, delta):
        count = len(self.flat_items)
        if count == 0: return

        # Calculate new index with wrapping
        new_index = (self.selected_index + delta) % count
        
        # Check if header (tuple), if so, skip in same direction
        if isinstance(self.flat_items[new_index], tuple):
             new_index = (new_index + delta) % count
        
        self.selected_index = new_index

    def _get_dependency_lines(self, feature):
        lines = [" ", "Dependencies:"]
        for dep in feature.dependencies:
            mark = "x" if dep.is_met() else " "
            name = dep.name
            if hasattr(dep, 'shared') and dep.shared:
                name += " (Shared)"
            lines.append(f" - [{mark}] {name}")
        return lines

    def _start_install(self, feature):
        self.dialog_title = f"Installing {feature.name}..."
        self.dialog_lines = ["Starting installation..."]
        self.show_dialog = True
        self.is_installing = True
        
        t = threading.Thread(target=self._run_action_thread, args=(feature, True))
        t.daemon = True
        t.start()

    def _start_uninstall(self, feature):
        self.dialog_title = f"Uninstalling {feature.name}..."
        self.dialog_lines = ["Starting removal...", "Note: Uninstalls python packages."]
        self.show_dialog = True
        self.is_installing = True
        
        t = threading.Thread(target=self._run_action_thread, args=(feature, False))
        t.daemon = True
        t.start()

    def _run_action_thread(self, feature, is_install):
        def progress_cb(msg):
            self.dialog_lines.append(msg)
            self.app.invalidate()
            
        try:
            if is_install:
                success = FeatureInstaller.install_feature(feature, on_progress=progress_cb)
                res_msg = "Installed"
            else:
                success = FeatureInstaller.uninstall_feature(feature, on_progress=progress_cb)
                res_msg = "Uninstalled"

            if success:
                self.dialog_lines.append(f"✅ SUCCESS! {res_msg}.")
            else:
                self.dialog_lines.append("❌ FAILED. See log.")
        except Exception as e:
            self.dialog_lines.append(f"Error: {e}")
        
        self.refresh_statuses()
        self.is_installing = False
        self.app.invalidate()

def run_feature_manager():
    tui = FeatureManagerTUI()
    tui.app.run()
