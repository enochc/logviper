#!/usr/bin/env python3
"""
LogViper - Cross-platform multi-file synchronized log viewer
"""

import re
import os
import glob
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Header, Footer, Static, Input, Label, ListView, ListItem,
    Button, RichLog, SelectionList
)
from textual.widgets.selection_list import Selection
from textual.screen import ModalScreen
from rich.text import Text
import watchdog.observers
import watchdog.events


# ══════════════════════════════════════════════════════════════════════════════
# Timestamp extraction
# ══════════════════════════════════════════════════════════════════════════════

TS_PATTERNS = [
    (re.compile(r'\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d+)'), "%Y-%m-%dT%H:%M:%S.%f"),
    (re.compile(r'\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})'),         "%Y-%m-%dT%H:%M:%S"),
    (re.compile(r'\b(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)'),             "%m-%d %H:%M:%S.%f"),
    (re.compile(r'\b([A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2})'),       "%b %d %H:%M:%S"),
    (re.compile(r'\b(\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2})'),    "%d/%b/%Y:%H:%M:%S"),
    (re.compile(r'\b(\d{2}:\d{2}:\d{2}\.\d+)'),                          "%H:%M:%S.%f"),
    (re.compile(r'\b(\d{2}:\d{2}:\d{2})'),                               "%H:%M:%S"),
    (re.compile(r'\b(\d{13})\b'),                                         "epoch_ms"),
    (re.compile(r'\b(\d{10})\b'),                                         "epoch_s"),
]

def extract_timestamp(line: str) -> Optional[float]:
    for pattern, fmt in TS_PATTERNS:
        m = pattern.search(line)
        if m:
            ts_str = m.group(1)
            try:
                if fmt == "epoch_ms":
                    return float(ts_str) / 1000.0
                elif fmt == "epoch_s":
                    return float(ts_str)
                else:
                    ts_str2 = ts_str.replace("T", " ")
                    dt = datetime.strptime(ts_str2, fmt.replace("T", " "))
                    if "%Y" not in fmt and "%y" not in fmt:
                        dt = dt.replace(year=datetime.now().year)
                    return dt.timestamp()
            except ValueError:
                continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Log line coloring
# ══════════════════════════════════════════════════════════════════════════════

LEVEL_PATTERNS = [
    (re.compile(r'\b(FATAL|CRITICAL)\b', re.I), "bold white on red"),
    (re.compile(r'\b(ERROR|ERR)\b',       re.I), "bold red"),
    (re.compile(r'\b(WARN|WARNING)\b',    re.I), "bold yellow"),
    (re.compile(r'\b(INFO)\b',            re.I), "green"),
    (re.compile(r'\b(DEBUG|DBG)\b',       re.I), "cyan"),
    (re.compile(r'\b(TRACE|VERBOSE)\b',   re.I), "dim cyan"),
]
TS_COLOR_RE = re.compile(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|\b\d{2}:\d{2}:\d{2}')

def colorize_line(line: str, highlights: list) -> Text:
    text = Text(line, no_wrap=True, overflow="ellipsis")
    for pattern, style in LEVEL_PATTERNS:
        if pattern.search(line):
            if "red" in style and "on" not in style:
                text.stylize("color(196)")
            elif "yellow" in style:
                text.stylize("color(220)")
            break
    for pattern, style in LEVEL_PATTERNS:
        for m in pattern.finditer(line):
            text.stylize(style, m.start(), m.end())
    for m in TS_COLOR_RE.finditer(line):
        text.stylize("bold blue", m.start(), m.end())
    for hl_pattern in highlights:
        for m in hl_pattern.finditer(line):
            text.stylize("bold black on yellow", m.start(), m.end())
    return text


# ══════════════════════════════════════════════════════════════════════════════
# Rollover-aware file reading
# ══════════════════════════════════════════════════════════════════════════════

def get_rollover_chain(filepath: str) -> list:
    m = re.match(r'^(.*\.(?:log|out|txt|err))(\.\d+)?$', filepath, re.I)
    base = m.group(1) if m else filepath
    files = []
    for f in glob.glob(glob.escape(base) + "*"):
        if f == base:
            files.append((0, f))
        else:
            nm = re.match(r'.*\.(\d+)$', f)
            if nm:
                files.append((int(nm.group(1)), f))
    files.sort(key=lambda x: -x[0])
    return [f for _, f in files]

def read_log_file_chain(filepath: str) -> list:
    lines = []
    for f in get_rollover_chain(filepath):
        try:
            with open(f, 'r', errors='replace') as fh:
                lines.extend(fh.readlines())
        except (OSError, PermissionError):
            pass
    return [l.rstrip('\n') for l in lines]


# ══════════════════════════════════════════════════════════════════════════════
# File discovery
# ══════════════════════════════════════════════════════════════════════════════

LOG_EXTENSIONS = {'.log', '.txt', '.out', '.err'}

def find_log_files(directory: str, max_results: int = 500) -> list:
    results = []
    try:
        for root, dirs, files in os.walk(directory):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
            for fname in sorted(files):
                # Skip rolled-over duplicates — show only base file in listing
                if re.search(r'\.\d+$', fname):
                    continue
                ext = Path(fname).suffix.lower()
                if ext in LOG_EXTENSIONS or 'log' in fname.lower():
                    results.append(os.path.join(root, fname))
                    if len(results) >= max_results:
                        return results
    except PermissionError:
        pass
    return results


# ══════════════════════════════════════════════════════════════════════════════
# File watcher
# ══════════════════════════════════════════════════════════════════════════════

class FileChangeHandler(watchdog.events.FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback
    def on_modified(self, event):
        if not event.is_directory:
            self.callback(event.src_path)


# ══════════════════════════════════════════════════════════════════════════════
# Directory Browser Modal
# ══════════════════════════════════════════════════════════════════════════════

class DirectoryBrowserModal(ModalScreen):
    """
    Scan a directory, see all log files with checkboxes,
    select up to 4 and assign them to panel slots.
    """

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    CSS = """
    DirectoryBrowserModal { align: center middle; }

    #browser-dialog {
        background: $surface;
        border: thick $accent;
        width: 92;
        height: 42;
        padding: 1 2;
    }
    #browser-title {
        text-align: center;
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }
    #path-row { height: 3; align: left middle; }
    #dir-input { width: 1fr; margin-right: 1; }
    #btn-browse { min-width: 10; }
    #filter-input { margin-top: 0; margin-bottom: 1; }
    #file-selection {
        height: 16;
        border: solid $surface-lighten-2;
        background: $background;
        margin-bottom: 1;
    }
    #status-bar { color: $text-muted; height: 1; margin-bottom: 1; }

    #slot-row { height: 3; align: left middle; margin-bottom: 1; }
    #slot-label { color: $text-muted; width: auto; margin-right: 1; }
    .slot-btn { min-width: 11; margin-right: 1; }

    #action-row { height: 3; align: right middle; }
    #btn-select-all { min-width: 14; margin-right: 1; }
    #btn-open-selected { min-width: 18; }
    #btn-cancel { min-width: 10; margin-left: 1; }
    """

    def __init__(self, on_open):
        super().__init__()
        self._on_open = on_open
        self._results: list = []
        self._start_panel = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="browser-dialog"):
            yield Label("📁  Open Log Files from Directory", id="browser-title")

            with Horizontal(id="path-row"):
                yield Input(
                    placeholder="Directory path  (e.g. /var/log  or  ~/myapp/logs  or  .)",
                    id="dir-input",
                )
                yield Button("Scan", variant="primary", id="btn-browse")

            yield Input(
                placeholder="Filter by filename  (e.g. app, nginx, *.log)  — leave blank to show all",
                id="filter-input",
            )

            yield SelectionList(id="file-selection")

            yield Label("Enter a directory path above, then click Scan.", id="status-bar")

            # Panel slot selector row
            with Horizontal(id="slot-row"):
                yield Label("Fill panels starting from:", id="slot-label")
                for i in range(4):
                    yield Button(
                        f"Panel {i+1}",
                        variant="success" if i == 0 else "default",
                        id=f"slot-{i}",
                        classes="slot-btn",
                    )

            with Horizontal(id="action-row"):
                yield Button("☑ Select All",       id="btn-select-all")
                yield Button("Open Selected →",     id="btn-open-selected", variant="success")
                yield Button("Cancel",              id="btn-cancel")

    def on_mount(self):
        self.query_one("#dir-input", Input).focus()

    # ── Slot buttons ────────────────────────────────────────────────────────

    @on(Button.Pressed, ".slot-btn")
    def on_slot(self, event: Button.Pressed):
        self._start_panel = int(event.button.id.split("-")[1])
        for i in range(4):
            self.query_one(f"#slot-{i}", Button).variant = (
                "success" if i == self._start_panel else "default"
            )

    # ── Scan ────────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-browse")
    @on(Input.Submitted, "#dir-input")
    @on(Input.Submitted, "#filter-input")
    def do_scan(self, event=None):
        raw = self.query_one("#dir-input", Input).value.strip()
        if not raw:
            return
        path = os.path.expanduser(raw)
        if not os.path.isdir(path):
            self.query_one("#status-bar", Label).update(
                f"[red]Not a directory: {path}[/red]"
            )
            return

        filt = self.query_one("#filter-input", Input).value.strip().lower()
        all_files = find_log_files(path)

        if filt:
            import fnmatch
            all_files = [
                f for f in all_files
                if filt in os.path.basename(f).lower()
                or fnmatch.fnmatch(os.path.basename(f).lower(), filt)
            ]

        self._results = all_files
        self._base_path = path

        sl = self.query_one("#file-selection", SelectionList)
        sl.clear_options()
        for f in all_files:
            try:
                display = os.path.relpath(f, path)
            except ValueError:
                display = f
            chain = get_rollover_chain(f)
            extra = f"  [dim](+{len(chain)-1} rolled)[/dim]" if len(chain) > 1 else ""
            sl.add_option(Selection(f"{display}{extra}", f, False))

        if not all_files:
            self.query_one("#status-bar", Label).update(
                "[yellow]No log files found. Try a different path or filter.[/yellow]"
            )
        else:
            self.query_one("#status-bar", Label).update(
                f"[green]{len(all_files)} file(s) found.[/green]  "
                f"[dim]Check the files you want, choose starting panel, then click Open.[/dim]"
            )

    # ── Select all toggle ────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-select-all")
    def do_select_all(self):
        sl = self.query_one("#file-selection", SelectionList)
        all_vals = [sl.get_option_at_index(i).value for i in range(len(sl._options))]
        selected = set(sl.selected)
        if len(selected) < len(all_vals):
            for v in all_vals:
                if v not in selected:
                    sl.select(v)
        else:
            sl.deselect_all()

    # ── Open selected ────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-open-selected")
    def do_open(self):
        sl = self.query_one("#file-selection", SelectionList)
        chosen = list(sl.selected)[:4]
        if not chosen:
            self.query_one("#status-bar", Label).update(
                "[yellow]Check at least one file, then click Open.[/yellow]"
            )
            return
        self._on_open(chosen, self._start_panel)
        self.dismiss()

    @on(Button.Pressed, "#btn-cancel")
    def do_cancel(self):
        self.dismiss()


# ══════════════════════════════════════════════════════════════════════════════
# Single-file picker  (used when clicking Open on an individual panel)
# ══════════════════════════════════════════════════════════════════════════════

class SingleFileModal(ModalScreen):
    """Pick one file to load into a specific panel."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    CSS = """
    SingleFileModal { align: center middle; }
    #sf-dialog {
        background: $surface;
        border: thick $accent;
        width: 80;
        height: 34;
        padding: 1 2;
    }
    #sf-title  { text-align: center; color: $accent; text-style: bold; margin-bottom: 1; }
    #sf-row    { height: 3; align: left middle; }
    #sf-input  { width: 1fr; margin-right: 1; }
    #sf-filter { margin-top: 0; margin-bottom: 1; }
    #sf-list   { height: 16; border: solid $surface-lighten-2; background: $background; margin-bottom: 1; }
    #sf-status { color: $text-muted; height: 1; margin-bottom: 1; }
    #sf-actions { height: 3; align: right middle; }
    """

    def __init__(self, panel_idx: int, on_select):
        super().__init__()
        self._panel_idx = panel_idx
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with Vertical(id="sf-dialog"):
            yield Label(f"📄  Open File → Panel {self._panel_idx + 1}", id="sf-title")
            with Horizontal(id="sf-row"):
                yield Input(placeholder="Directory or full file path...", id="sf-input")
                yield Button("Scan", variant="primary", id="sf-scan")
            yield Input(placeholder="Filter by filename...", id="sf-filter")
            yield ListView(id="sf-list")
            yield Label("Enter a path above and click Scan, or type a direct file path.", id="sf-status")
            with Horizontal(id="sf-actions"):
                yield Button("Open", variant="success", id="sf-open")
                yield Button("Cancel", id="sf-cancel")

    def on_mount(self):
        self.query_one("#sf-input", Input).focus()

    @on(Button.Pressed, "#sf-scan")
    @on(Input.Submitted, "#sf-input")
    @on(Input.Submitted, "#sf-filter")
    def do_scan(self, event=None):
        raw = self.query_one("#sf-input", Input).value.strip()
        if not raw:
            return
        path = os.path.expanduser(raw)
        filt = self.query_one("#sf-filter", Input).value.strip().lower()

        if os.path.isfile(path):
            results = [path]
        elif os.path.isdir(path):
            results = find_log_files(path)
            if filt:
                import fnmatch
                results = [f for f in results
                           if filt in os.path.basename(f).lower()
                           or fnmatch.fnmatch(os.path.basename(f).lower(), filt)]
        else:
            self.query_one("#sf-status", Label).update(f"[red]Path not found: {path}[/red]")
            return

        lv = self.query_one("#sf-list", ListView)
        lv.clear()
        for f in results:
            try:
                display = os.path.relpath(f, path) if os.path.isdir(path) else f
            except ValueError:
                display = f
            lv.append(ListItem(Label(display), name=f))

        self.query_one("#sf-status", Label).update(
            f"[green]{len(results)} file(s)[/green]  — double-click or select and press Open"
        )

    @on(ListView.Selected)
    def on_list_selected(self, event: ListView.Selected):
        self._on_select(event.item.name)
        self.dismiss()

    @on(Button.Pressed, "#sf-open")
    def do_open(self):
        lv = self.query_one("#sf-list", ListView)
        if lv.highlighted_child:
            self._on_select(lv.highlighted_child.name)
            self.dismiss()
        else:
            self.query_one("#sf-status", Label).update("[yellow]Select a file from the list first.[/yellow]")

    @on(Button.Pressed, "#sf-cancel")
    def do_cancel(self):
        self.dismiss()


# ══════════════════════════════════════════════════════════════════════════════
# Help screen
# ══════════════════════════════════════════════════════════════════════════════

class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,?", "dismiss", "Close")]
    CSS = """
    HelpScreen { align: center middle; }
    #help-dialog {
        background: $surface;
        border: thick $accent;
        width: 66;
        height: 36;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static("""[bold cyan]LogViper — Keyboard Reference[/bold cyan]

[bold yellow]Opening Files[/bold yellow]
  D         Open Directory Browser
              → Scan a folder, check files, assign to panels
  O         Open single file into the focused panel
  Click ▶   Click the big button inside any empty panel

[bold yellow]Panel Focus[/bold yellow]
  1 / 2 / 3 / 4    Focus that panel (highlighted border)

[bold yellow]Search & Highlight[/bold yellow]
  /         Jump to search box
  Enter     Run regex search across ALL panels
  N / F3    Next match
  Shift+F3  Previous match
  Esc       Clear highlights

[bold yellow]Navigation & View[/bold yellow]
  S    Sync all panels to focused panel's timestamp
  F    Toggle follow / tail mode
  ?    This help screen
  Q    Quit

[bold yellow]Search regex examples[/bold yellow]
  error|warn|fail      timeout.*503
""", markup=True)
            yield Button("Close  (Esc)", id="help-close")

    @on(Button.Pressed, "#help-close")
    def close(self):
        self.dismiss()


# ══════════════════════════════════════════════════════════════════════════════
# Log Panel widget
# ══════════════════════════════════════════════════════════════════════════════

class LogPanel(Vertical):

    DEFAULT_CSS = """
    LogPanel {
        border: solid $surface-lighten-2;
        height: 1fr;
        width: 1fr;
    }
    LogPanel.active-panel {
        border: solid $accent;
    }
    LogPanel > .panel-header {
        background: $surface-lighten-1;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    LogPanel > .panel-header.has-file {
        background: $primary-darken-2;
        color: $text;
    }
    LogPanel > RichLog {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    .empty-prompt {
        height: 1fr;
        align: center middle;
        background: $background;
    }
    .open-panel-btn { min-width: 32; }
    """

    def __init__(self, panel_id: int, open_callback):
        super().__init__()
        self.panel_id = panel_id
        self._open_callback = open_callback
        self.filepath: Optional[str] = None
        self.lines: list = []
        self.highlights: list = []
        self._follow = True
        self._lock = threading.Lock()

    def compose(self) -> ComposeResult:
        yield Static(
            f"[dim]Panel {self.panel_id + 1} — empty[/dim]",
            classes="panel-header",
            id=f"ph-{self.panel_id}",
        )
        yield Vertical(
            Label(f"[dim]Panel {self.panel_id + 1}[/dim]\n"),
            Button(
                f"▶  Open a file into Panel {self.panel_id + 1}",
                variant="primary",
                id=f"open-btn-{self.panel_id}",
                classes="open-panel-btn",
            ),
            Label("\n[dim]Or press  D  to browse a whole directory[/dim]"),
            classes="empty-prompt",
            id=f"empty-{self.panel_id}",
        )
        rl = RichLog(
            highlight=False, markup=False, wrap=False,
            id=f"rl-{self.panel_id}", auto_scroll=False,
        )
        rl.display = False
        yield rl

    @on(Button.Pressed)
    def on_open_btn(self, event: Button.Pressed):
        if event.button.id == f"open-btn-{self.panel_id}":
            event.stop()
            self._open_callback(self.panel_id)

    def load_file(self, filepath: str):
        self.filepath = filepath
        fname = os.path.basename(filepath)
        chain = get_rollover_chain(filepath)
        rolled = f" [dim](+{len(chain)-1} rolled)[/dim]" if len(chain) > 1 else ""

        header = self.query_one(f"#ph-{self.panel_id}", Static)
        header.update(
            f"[bold]P{self.panel_id+1}[/bold] {fname}{rolled}  "
            f"[dim]{os.path.dirname(filepath)}[/dim]"
        )
        header.add_class("has-file")

        self.query_one(f"#empty-{self.panel_id}").display = False
        self.query_one(f"#rl-{self.panel_id}", RichLog).display = True
        self.reload()

    def reload(self, append_only: bool = False):
        if not self.filepath:
            return
        try:
            new_lines = read_log_file_chain(self.filepath)
        except Exception:
            return
        rl = self.query_one(f"#rl-{self.panel_id}", RichLog)
        with self._lock:
            if append_only and self.lines:
                added = new_lines[len(self.lines):]
                self.lines = new_lines
            else:
                added = new_lines
                self.lines = new_lines
                rl.clear()
            for line in added:
                rl.write(colorize_line(line, self.highlights))
        if self._follow:
            rl.scroll_end(animate=False)

    def apply_highlights(self, highlights: list):
        self.highlights = highlights
        self.reload()

    def scroll_to_line(self, line_index: int):
        self.query_one(f"#rl-{self.panel_id}", RichLog).scroll_to(y=line_index, animate=False)

    def scroll_to_timestamp(self, ts: float, tolerance: float = 2.0):
        best_idx, best_diff = 0, float('inf')
        for i, line in enumerate(self.lines):
            lt = extract_timestamp(line)
            if lt is not None:
                diff = abs(lt - ts)
                if diff < best_diff:
                    best_diff, best_idx = diff, i
                    if diff < tolerance:
                        break
        self.scroll_to_line(best_idx)

    def search_lines(self, pattern) -> list:
        return [(i, l) for i, l in enumerate(self.lines) if pattern.search(l)]

    def get_current_timestamp(self) -> Optional[float]:
        rl = self.query_one(f"#rl-{self.panel_id}", RichLog)
        scroll_y = int(rl.scroll_y)
        for line in self.lines[scroll_y: scroll_y + 20]:
            ts = extract_timestamp(line)
            if ts:
                return ts
        return None

    def set_active(self, active: bool):
        if active:
            self.add_class("active-panel")
        else:
            self.remove_class("active-panel")


# ══════════════════════════════════════════════════════════════════════════════
# Main Application
# ══════════════════════════════════════════════════════════════════════════════

class LogViperApp(App):

    TITLE = "LogViper"
    SUB_TITLE = "D = open directory  |  O = open file  |  ? = help"

    CSS = """
    Screen { background: $background; }

    #toolbar {
        height: 3;
        background: $surface;
        padding: 0 1;
        align: left middle;
    }
    #search-input { width: 36; margin: 0 1; }
    #search-status { width: 1fr; color: $text-muted; margin: 0 1; }
    .tb-btn { min-width: 10; height: 1; margin-right: 1; }

    #panels-container { height: 1fr; }
    #panels-row-1, #panels-row-2 { height: 1fr; }
    """

    BINDINGS = [
        Binding("d",        "open_directory", "Open Dir"),
        Binding("o",        "open_file",      "Open File"),
        Binding("s",        "sync_panels",    "Sync"),
        Binding("f",        "toggle_follow",  "Follow"),
        Binding("/",        "focus_search",   "Search"),
        Binding("n,f3",     "next_match",     "Next"),
        Binding("shift+f3", "prev_match",     "Prev"),
        Binding("escape",   "clear_search",   "Clear"),
        Binding("?",        "show_help",      "Help"),
        Binding("q",        "quit",           "Quit"),
        Binding("1", "focus_panel_0", show=False),
        Binding("2", "focus_panel_1", show=False),
        Binding("3", "focus_panel_2", show=False),
        Binding("4", "focus_panel_3", show=False),
    ]

    def __init__(self, files: list = None):
        super().__init__()
        self._initial_files = files or []
        self._panels: list[LogPanel] = []
        self._active_panel = 0
        self._highlights: list = []
        self._search_matches: list = []
        self._match_cursor = -1
        self._follow = True
        self._watcher = watchdog.observers.Observer()
        self._watcher.start()
        self._watched_dirs: set = set()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="toolbar"):
            yield Button("📁 Open Directory [D]", variant="primary", id="btn-dir",    classes="tb-btn")
            yield Button("📄 Open File [O]",      variant="default", id="btn-file",   classes="tb-btn")
            yield Label("│ ")
            yield Input(placeholder="Search / highlight (regex supported)...", id="search-input")
            yield Label("", id="search-status")
            yield Button("⚡ Sync [S]",     id="btn-sync",   classes="tb-btn")
            yield Button("📌 Follow: ON",   id="btn-follow", classes="tb-btn", variant="success")
        with Vertical(id="panels-container"):
            with Horizontal(id="panels-row-1"):
                pass
        yield Footer()

    def on_mount(self):
        # Always create all 4 panels upfront so the layout is obvious
        for _ in range(4):
            self._add_panel()

        for i, f in enumerate(self._initial_files[:4]):
            if os.path.exists(f):
                self._panels[i].load_file(f)
                self._watch_file(f)

        self._set_active(0)

    # ── Panel layout ────────────────────────────────────────────────────────

    def _add_panel(self):
        if len(self._panels) >= 4:
            return
        panel = LogPanel(len(self._panels), open_callback=self._panel_open_callback)
        self._panels.append(panel)
        if len(self._panels) <= 2:
            self.query_one("#panels-row-1", Horizontal).mount(panel)
        else:
            try:
                row2 = self.query_one("#panels-row-2", Horizontal)
            except Exception:
                row2 = Horizontal(id="panels-row-2")
                self.query_one("#panels-container", Vertical).mount(row2)
            row2.mount(panel)

    def _panel_open_callback(self, panel_idx: int):
        self._set_active(panel_idx)
        self.action_open_file()

    def _set_active(self, idx: int):
        for i, p in enumerate(self._panels):
            p.set_active(i == idx)
        self._active_panel = idx

    @property
    def active_panel(self) -> Optional[LogPanel]:
        if 0 <= self._active_panel < len(self._panels):
            return self._panels[self._active_panel]
        return None

    # ── File watching ────────────────────────────────────────────────────────

    def _watch_file(self, filepath: str):
        directory = os.path.dirname(os.path.abspath(filepath))
        if directory not in self._watched_dirs:
            self._watched_dirs.add(directory)
            self._watcher.schedule(
                FileChangeHandler(self._on_file_changed), directory, recursive=False
            )

    def _on_file_changed(self, changed_path: str):
        for panel in self._panels:
            if panel.filepath:
                chain = get_rollover_chain(panel.filepath)
                if changed_path in chain or changed_path == panel.filepath:
                    self.call_from_thread(panel.reload, True)

    # ── Toolbar buttons ──────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-dir")
    def on_btn_dir(self):   self.action_open_directory()

    @on(Button.Pressed, "#btn-file")
    def on_btn_file(self):  self.action_open_file()

    @on(Button.Pressed, "#btn-sync")
    def on_btn_sync(self):  self.action_sync_panels()

    @on(Button.Pressed, "#btn-follow")
    def on_btn_follow(self): self.action_toggle_follow()

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_open_directory(self):
        def on_open(files: list, start_panel: int):
            for offset, filepath in enumerate(files[:4]):
                slot = (start_panel + offset) % 4
                self._panels[slot].load_file(filepath)
                self._watch_file(filepath)
                if self._highlights:
                    self._panels[slot].apply_highlights(self._highlights)
        self.push_screen(DirectoryBrowserModal(on_open=on_open))

    def action_open_file(self):
        panel_idx = self._active_panel
        def on_select(filepath: str):
            self._panels[panel_idx].load_file(filepath)
            self._watch_file(filepath)
            if self._highlights:
                self._panels[panel_idx].apply_highlights(self._highlights)
        self.push_screen(SingleFileModal(panel_idx, on_select))

    def action_focus_panel_0(self): self._set_active(0)
    def action_focus_panel_1(self): self._set_active(1)
    def action_focus_panel_2(self): self._set_active(2)
    def action_focus_panel_3(self): self._set_active(3)

    def action_focus_search(self):
        self.query_one("#search-input", Input).focus()

    def action_sync_panels(self):
        ap = self.active_panel
        if not ap or not ap.filepath:
            self.query_one("#search-status", Label).update(
                "[yellow]Focus a panel with a loaded file first[/yellow]"
            )
            return
        ts = ap.get_current_timestamp()
        if ts is None:
            self.query_one("#search-status", Label).update(
                "[yellow]No timestamp in visible area[/yellow]"
            )
            return
        dt = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        self.query_one("#search-status", Label).update(f"[cyan]Synced all → {dt}[/cyan]")
        for i, panel in enumerate(self._panels):
            if i != self._active_panel and panel.filepath:
                panel.scroll_to_timestamp(ts)

    def action_toggle_follow(self):
        self._follow = not self._follow
        btn = self.query_one("#btn-follow", Button)
        btn.label = "📌 Follow: ON" if self._follow else "📌 Follow: OFF"
        btn.variant = "success" if self._follow else "default"
        for panel in self._panels:
            panel._follow = self._follow

    @on(Input.Submitted, "#search-input")
    def on_search(self, event: Input.Submitted):
        query = event.value.strip()
        if not query:
            self.action_clear_search()
            return
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            self.query_one("#search-status", Label).update(f"[red]Bad regex: {e}[/red]")
            return

        self._highlights = [pattern]
        self._search_matches = []
        for i, panel in enumerate(self._panels):
            panel.apply_highlights(self._highlights)
            for line_idx, _ in panel.search_lines(pattern):
                self._search_matches.append((i, line_idx))

        self._match_cursor = 0 if self._search_matches else -1
        total = len(self._search_matches)
        loaded = sum(1 for p in self._panels if p.filepath)
        self.query_one("#search-status", Label).update(
            f"[green]{total} matches across {loaded} panel(s)[/green]"
            if total else "[yellow]No matches[/yellow]"
        )
        if self._search_matches:
            self._jump_to_match(0)

    def action_next_match(self):
        if not self._search_matches:
            return
        self._match_cursor = (self._match_cursor + 1) % len(self._search_matches)
        self._jump_to_match(self._match_cursor)

    def action_prev_match(self):
        if not self._search_matches:
            return
        self._match_cursor = (self._match_cursor - 1) % len(self._search_matches)
        self._jump_to_match(self._match_cursor)

    def _jump_to_match(self, cursor: int):
        if not self._search_matches or cursor >= len(self._search_matches):
            return
        panel_idx, line_idx = self._search_matches[cursor]
        self._panels[panel_idx].scroll_to_line(line_idx)
        n = len(self._search_matches)
        self.query_one("#search-status", Label).update(
            f"[green]Match {cursor+1}/{n}[/green] — Panel {panel_idx+1}, line {line_idx+1}"
        )

    def action_clear_search(self):
        self._highlights = []
        self._search_matches = []
        self._match_cursor = -1
        for panel in self._panels:
            panel.highlights = []
            if panel.filepath:
                panel.reload()
        self.query_one("#search-status", Label).update("")
        self.query_one("#search-input", Input).value = ""

    def action_show_help(self):
        self.push_screen(HelpScreen())

    def on_unmount(self):
        self._watcher.stop()
        self._watcher.join()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="LogViper — Multi-file synchronized log viewer")
    parser.add_argument("files", nargs="*", help="Log files to open (up to 4)")
    args = parser.parse_args()
    files = [f for f in args.files if os.path.isfile(f)][:4]
    LogViperApp(files=files).run()


if __name__ == "__main__":
    main()
