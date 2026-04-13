"""
panels/rosout.py — /rosout log viewer.
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

_LEVEL_STYLE = {
    "DEBUG": "dim white",
    "INFO": "white",
    "WARN": "yellow",
    "ERROR": "bold red",
    "FATAL": "bold bright_red reverse",
}
_LEVEL_ORDER = ["ALL", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]

# Session start in monotonic time — used to compute relative timestamps
_SESSION_START = time.monotonic()


def _format_line(ts: float, level: str, raw_line: str) -> Text:
    """
    Format a single log entry.
    raw_line is already '[node_name] message text' — the bridge strips the
    leading [LEVEL] prefix before storing, so we just add timestamp + level here.
    """
    t = Text(no_wrap=True)

    # Relative timestamp from session start
    age = max(0.0, ts - _SESSION_START)
    secs = int(age)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    t.append(f"{h:02d}:{m:02d}:{s:02d} ", style="bright_black")

    # Level badge
    t.append(f"[{level:<5}] ", style=_LEVEL_STYLE.get(level, "white"))

    # The raw_line from the store is "[node_name] message"
    # Highlight the node name portion in cyan
    if raw_line.startswith("["):
        end = raw_line.find("]")
        if end > 0:
            t.append(raw_line[: end + 1], style="cyan")
            t.append(raw_line[end + 1 :], style=_LEVEL_STYLE.get(level, "white"))
            return t

    t.append(raw_line, style=_LEVEL_STYLE.get(level, "white"))
    return t


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
        yield Input(placeholder="filter by node…", id="node_filter")
        yield Input(placeholder="keyword search…", id="kw_filter")
        yield Button("Export", id="export_btn", variant="default")


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


class RosoutPanel(Widget):
    BINDINGS = [
        Binding("end", "scroll_end", "Tail", show=True),
        Binding("ctrl+e", "export_logs", "Export", show=True),
        Binding("ctrl+f", "focus_filter", "Filter", show=False),
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
        self._store = store
        self._auto_scroll = True
        self._level = "ALL"
        self._node_filter = ""
        self._kw_filter = ""
        self._last_count = 0

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
            for t, level, line in lines:
                age = max(0.0, t - _SESSION_START)
                secs = int(age)
                h, rem = divmod(secs, 3600)
                m, s = divmod(rem, 60)
                f.write(f"{h:02d}:{m:02d}:{s:02d} [{level:<5}] {line}\n")
        self._update_header(len(lines), exported=fname)

    def action_focus_filter(self) -> None:
        try:
            self.query_one("#node_filter", Input).focus()
        except NoMatches:
            pass

    # -----------------------------------------------------------------------
    # Refresh — only re-render if count changed
    # -----------------------------------------------------------------------

    def _refresh(self) -> None:
        lines = self._store.snapshot_logs(
            node_filter=self._node_filter or None,
            level_filter=self._level if self._level != "ALL" else None,
            keyword_filter=self._kw_filter or None,
            max_lines=500,
        )
        if len(lines) != self._last_count or self._node_filter or self._kw_filter:
            self._last_count = len(lines)
            self._render_lines(lines)

        self._update_header(len(lines))

        if self._auto_scroll:
            self.query_one("#log_view", LogView).scroll_to_bottom()

    def _render_lines(self, lines: List[Tuple[float, str, str]]) -> None:
        result = Text()
        for ts, level, line in lines:
            # line is stored as "[node] message" by _rosout_cb
            result.append_text(_format_line(ts, level, line))
            result.append("\n")
        if not lines:
            result.append("  No log messages yet", style="dim")
        self.query_one("#log_view", LogView).set_content(result)

    def _update_header(self, shown: int, exported: Optional[str] = None) -> None:
        all_lines = self._store.snapshot_logs(max_lines=10000)
        counts: dict = {}
        for _, level, _ in all_lines:
            counts[level] = counts.get(level, 0) + 1

        parts = Text()
        parts.append(" /rosout  ", style="bold")
        for level in ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]:
            n = counts.get(level, 0)
            if n:
                parts.append(f"{level[0]}:{n} ", style=_LEVEL_STYLE.get(level, "white"))

        tail = " [dim]↓ tailing[/dim]" if self._auto_scroll else " [dim]↑ paused[/dim]"
        parts.append(f"  ({shown} shown){tail}")

        if exported:
            parts.append(f"  ✓ → {exported}", style="green")

        self.query_one("#log_header", Static).update(parts)
