"""
panels/rosout.py — /rosout log viewer.

Features:
  - Live scrolling log with level-based colour coding
  - Filter by: log level (ALL/DEBUG/INFO/WARN/ERROR/FATAL), node name, keyword
  - Auto-scroll toggle (pauses when you scroll up, resumes on End key)
  - Export to timestamped file
  - Level summary counts in header

Colour coding:
  DEBUG  dim white
  INFO   white
  WARN   yellow
  ERROR  bold red
  FATAL  bold bright_red + reverse
"""

import time
from pathlib import Path
from typing import List, Optional, Tuple

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Input, Button, Label, Select
from textual.containers import Horizontal, ScrollableContainer
from textual.binding import Binding
from textual.css.query import NoMatches
from rich.text import Text

from core.data_store import DataStore

# ---------------------------------------------------------------------------
# Level colours (Rich string styles)
# ---------------------------------------------------------------------------
_LEVEL_STYLE = {
    "DEBUG": "dim white",
    "INFO":  "white",
    "WARN":  "yellow",
    "ERROR": "bold red",
    "FATAL": "bold bright_red reverse",
}
_LEVEL_ORDER = ["ALL", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]


def _level_label(level: str) -> Text:
    style = _LEVEL_STYLE.get(level, "white")
    return Text(f"[{level:<5}]", style=style)


def _format_line(ts: float, level: str, line: str) -> Text:
    """Format a single log entry as a Rich Text object."""
    t = Text(no_wrap=True)
    # Timestamp relative to session start — show as HH:MM:SS
    secs = int(ts)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    t.append(f"{h:02d}:{m:02d}:{s:02d} ", style="bright_black")
    t.append(f"[{level:<5}] ", style=_LEVEL_STYLE.get(level, "white"))
    # Highlight node name in brackets
    if line.startswith("["):
        end = line.find("]")
        if end > 0:
            t.append(line[:end + 1], style="cyan")
            t.append(line[end + 1:], style=_LEVEL_STYLE.get(level, "white"))
            return t
    t.append(line, style=_LEVEL_STYLE.get(level, "white"))
    return t


# ---------------------------------------------------------------------------
# Filter bar
# ---------------------------------------------------------------------------

class LogFilterBar(Horizontal):
    DEFAULT_CSS = """
    LogFilterBar {
        height: 3;
        padding: 0 1;
        background: $surface-darken-1;
        border-bottom: solid $accent;
    }
    LogFilterBar Label  { width: auto; content-align: left middle; padding: 0 1; }
    LogFilterBar Select { width: 14; height: 3; }
    LogFilterBar Input  { width: 1fr; height: 3; margin-left: 1; }
    LogFilterBar Button { width: 12; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label("Level:")
        yield Select(
            [(l, l) for l in _LEVEL_ORDER],
            value="ALL",
            id="level_select",
            allow_blank=False,
        )
        yield Input(placeholder="filter by node…",    id="node_filter")
        yield Input(placeholder="keyword search…",    id="kw_filter")
        yield Button("Export", id="export_btn", variant="default")


# ---------------------------------------------------------------------------
# Log view
# ---------------------------------------------------------------------------

class LogView(ScrollableContainer):
    DEFAULT_CSS = """
    LogView {
        height: 1fr;
        overflow-y: scroll;
        overflow-x: auto;
        padding: 0 1;
        background: $surface-darken-2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="log_content")

    def set_content(self, text: Text) -> None:
        try:
            self.query_one("#log_content", Static).update(text)
        except NoMatches:
            pass

    def scroll_to_bottom(self) -> None:
        self.scroll_end(animate=False)


# ---------------------------------------------------------------------------
# RosoutPanel
# ---------------------------------------------------------------------------

class RosoutPanel(Widget):

    BINDINGS = [
        Binding("end",    "scroll_end",    "Tail",         show=True),
        Binding("ctrl+e", "export_logs",   "Export",       show=True),
        Binding("ctrl+f", "focus_filter",  "Filter",       show=False),
    ]

    DEFAULT_CSS = """
    RosoutPanel { height: 1fr; }
    #log_header {
        height: 1;
        padding: 0 1;
        background: $surface-darken-2;
        border-bottom: solid $accent;
    }
    """

    def __init__(self, store: DataStore, **kwargs):
        super().__init__(**kwargs)
        self._store       = store
        self._auto_scroll = True
        self._level       = "ALL"
        self._node_filter = ""
        self._kw_filter   = ""
        self._session_start = time.monotonic()

    def compose(self) -> ComposeResult:
        yield Static("", id="log_header")
        yield LogFilterBar(id="filter_bar")
        yield LogView(id="log_view")

    def on_mount(self) -> None:
        self.set_interval(0.5, self._refresh)

    # -----------------------------------------------------------------------
    # Filter events
    # -----------------------------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "level_select":
            self._level = str(event.value) if event.value != Select.BLANK else "ALL"
            self._refresh()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "node_filter":
            self._node_filter = event.value.strip()
        elif event.input.id == "kw_filter":
            self._kw_filter = event.value.strip()
        self._refresh()

    def on_scroll_changed(self) -> None:
        # If user scrolls up, pause auto-scroll
        view = self.query_one("#log_view", LogView)
        if view.scroll_y < view.max_scroll_y - 2:
            self._auto_scroll = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "export_btn":
            self.action_export_logs()

    # -----------------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------------

    def action_scroll_end(self) -> None:
        self._auto_scroll = True
        self.query_one("#log_view", LogView).scroll_to_bottom()

    def action_export_logs(self) -> None:
        lines = self._store.snapshot_logs(
            node_filter=self._node_filter or None,
            level_filter=self._level if self._level != "ALL" else None,
            keyword_filter=self._kw_filter or None,
            max_lines=10000,
        )
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"rosout_{ts}.log"
        with open(fname, "w") as f:
            for (t, level, line) in lines:
                secs = int(t)
                h, rem = divmod(secs, 3600)
                m, s   = divmod(rem, 60)
                f.write(f"{h:02d}:{m:02d}:{s:02d} [{level:<5}] {line}\n")
        self._update_header(len(lines), exported=fname)

    def action_focus_filter(self) -> None:
        try:
            self.query_one("#node_filter", Input).focus()
        except NoMatches:
            pass

    # -----------------------------------------------------------------------
    # Refresh
    # -----------------------------------------------------------------------

    def _refresh(self) -> None:
        lines = self._store.snapshot_logs(
            node_filter=self._node_filter or None,
            level_filter=self._level if self._level != "ALL" else None,
            keyword_filter=self._kw_filter or None,
            max_lines=500,
        )
        self._render_lines(lines)
        self._update_header(len(lines))

        if self._auto_scroll:
            self.query_one("#log_view", LogView).scroll_to_bottom()

    def _render_lines(self, lines: List[Tuple[float, str, str]]) -> None:
        result = Text()
        for ts, level, line in lines:
            age = ts - self._session_start
            result.append_text(_format_line(age, level, line))
            result.append("\n")
        if not lines:
            result.append("  No log messages yet", style="dim")
        self.query_one("#log_view", LogView).set_content(result)

    def _update_header(self, shown: int, exported: Optional[str] = None) -> None:
        all_lines = self._store.snapshot_logs(max_lines=10000)
        counts = {}
        for _, level, _ in all_lines:
            counts[level] = counts.get(level, 0) + 1

        parts = Text()
        parts.append(" /rosout  ", style="bold")
        for level in ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]:
            n = counts.get(level, 0)
            if n:
                parts.append(f"{level[:1]}:{n} ", style=_LEVEL_STYLE.get(level, "white"))

        scroll_indicator = " [dim]↑ paused[/dim]" if not self._auto_scroll else " [dim]↓ tailing[/dim]"
        parts.append(f" ({shown} shown){scroll_indicator}")

        if exported:
            parts.append(f"  ✓ exported → {exported}", style="green")

        self.query_one("#log_header", Static).update(parts)
