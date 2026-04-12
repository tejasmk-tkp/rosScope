"""
panels/plot_panel.py — Time-series plot panel.

Topic selector: searchable dropdown (Input + OptionList overlay).
Braille dot renderer via Static.update().
Pinned topics shown in a focusable chip bar — navigate with ←/→, unpin with Delete/x.
"""

import time
from typing import Callable, Dict, List, Optional, Tuple

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Input, Button, OptionList
from textual.widgets._option_list import Option
from textual.containers import Horizontal, Vertical
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.message import Message
from rich.text import Text
from rich.style import Style

from core.data_store import DataStore, PlotPoint, ParamChangeMarker

# ---------------------------------------------------------------------------
# Braille renderer
# ---------------------------------------------------------------------------
_BRAILLE_BASE = 0x2800
_DOT_MAP = [
    [0x01, 0x02, 0x04, 0x40],
    [0x08, 0x10, 0x20, 0x80],
]
_COLORS = [
    "bright_cyan",
    "bright_red",
    "bright_yellow",
    "bright_green",
    "bright_magenta",
    "bright_blue",
    "white",
]
_WINDOW_OPTIONS = [10, 30, 60, 120, 300]


def _make_canvas(w: int, h: int) -> List[List[int]]:
    return [[0] * w for _ in range(h)]


def _plot_dot(canvas, dot_x: int, dot_y: int, cw: int, ch: int):
    cx, bit_col = divmod(max(0, min(dot_x, cw * 2 - 1)), 2)
    cy, bit_row = divmod(max(0, min(dot_y, ch * 4 - 1)), 4)
    if 0 <= cx < cw and 0 <= cy < ch:
        canvas[cy][cx] |= _DOT_MAP[bit_col][bit_row]


def _draw_line_on_canvas(canvas, x0n, y0, x1n, y1, cw, ch, ymin, ymax):
    dw, dh = cw * 2, ch * 4
    yr = ymax - ymin if ymax != ymin else 1.0

    def to_dots(xn, yv):
        return (int(xn * (dw - 1)), int((1.0 - (yv - ymin) / yr) * (dh - 1)))

    dx0, dy0 = to_dots(x0n, y0)
    dx1, dy1 = to_dots(x1n, y1)
    steps = max(abs(dx1 - dx0), abs(dy1 - dy0), 1)
    for i in range(steps + 1):
        t = i / steps
        _plot_dot(canvas, int(dx0 + t * (dx1 - dx0)),
                  int(dy0 + t * (dy1 - dy0)), cw, ch)


def _draw_vline(canvas, xn, cw, ch):
    dx = int(xn * (cw * 2 - 1))
    for dy in range(ch * 4):
        _plot_dot(canvas, dx, dy, cw, ch)


def render_plot_text(series, markers, window, cw, ch) -> Text:
    if cw < 4 or ch < 4:
        return Text("(too small)")

    now = time.monotonic()
    t0 = now - window
    all_vals = [p.value for pts in series.values() for p in pts]
    if not all_vals:
        return Text("  No data — select and pin a topic above", style="dim")

    ymin, ymax = min(all_vals), max(all_vals)
    if ymin == ymax:
        ymin -= 1.0
        ymax += 1.0

    topic_layers = []
    for idx, (topic, points) in enumerate(series.items()):
        color = _COLORS[idx % len(_COLORS)]
        layer = _make_canvas(cw, ch)
        pts = [p for p in points if p.timestamp >= t0]
        for i in range(len(pts) - 1):
            xn0 = (pts[i].timestamp - t0) / window
            xn1 = (pts[i + 1].timestamp - t0) / window
            _draw_line_on_canvas(layer, xn0, pts[i].value, xn1,
                                 pts[i + 1].value, cw, ch, ymin, ymax)
        topic_layers.append((layer, color))

    mcanvas = _make_canvas(cw, ch)
    for m in markers:
        if t0 <= m.timestamp <= now:
            _draw_vline(mcanvas, (m.timestamp - t0) / window, cw, ch)

    tick_rows = {0, ch // 4, ch // 2, 3 * ch // 4, ch - 1}
    AXIS_W = 8

    result = Text()
    for cy in range(ch):
        if cy in tick_rows:
            frac = 1.0 - cy / max(ch - 1, 1)
            label = f"{ymin + frac * (ymax - ymin):7.2f} "
        else:
            label = " " * AXIS_W
        result.append(label, style=Style(color="bright_black"))

        for cx in range(max(cw - AXIS_W, 1)):
            if mcanvas[cy][cx]:
                result.append(chr(_BRAILLE_BASE | mcanvas[cy][cx]),
                              style=Style(color="white"))
                continue
            drawn = False
            for layer, color in topic_layers:
                if layer[cy][cx]:
                    result.append(chr(_BRAILLE_BASE | layer[cy][cx]),
                                  style=Style(color=color))
                    drawn = True
                    break
            if not drawn:
                result.append(chr(_BRAILLE_BASE))
        result.append("\n")

    return result


# ---------------------------------------------------------------------------
# Topic search dropdown — mounted at app level to float above all widgets
# ---------------------------------------------------------------------------


class TopicDropdown(OptionList):
    """Floating OptionList at app level. Uses callback to avoid message routing issues."""

    DEFAULT_CSS = """
    TopicDropdown {
        display: none;
        height: auto;
        max-height: 12;
        width: 60;
        border: tall $accent;
        background: $surface;
    }
    """

    def __init__(self, on_chosen: Callable[[str], None], **kwargs):
        super().__init__(**kwargs)
        self._on_chosen = on_chosen

    def on_option_list_option_selected(
            self, event: OptionList.OptionSelected) -> None:
        self.display = False
        self._on_chosen(str(event.option.prompt))
        event.stop()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.display = False
            event.stop()


class TopicSearchBar(Widget):
    """Input + floating OptionList. Emits TopicSearchBar.Selected(topic)."""

    DEFAULT_CSS = """
    TopicSearchBar {
        height: 3;
        width: 1fr;
    }
    #search_input {
        height: 3;
        width: 1fr;
    }
    """

    class Selected(Message):

        def __init__(self, topic: str) -> None:
            super().__init__()
            self.topic = topic

    def __init__(self, store: DataStore, **kwargs):
        super().__init__(**kwargs)
        self._store = store
        self._all_topics: List[str] = []
        self._dropdown: Optional[TopicDropdown] = None

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search & select topic to pin…",
                    id="search_input")

    def on_mount(self) -> None:
        self._dropdown = TopicDropdown(on_chosen=self._on_topic_chosen,
                                       id="topic_dropdown")
        self.app.mount(self._dropdown)
        self.set_interval(2.0, self._refresh_topic_list)

    def _on_topic_chosen(self, topic: str) -> None:
        try:
            self.query_one("#search_input", Input).value = ""
        except NoMatches:
            pass
        self.post_message(self.Selected(topic))

    def _reposition(self) -> None:
        if self._dropdown is None:
            return
        try:
            inp = self.query_one("#search_input", Input)
            off = inp.screen_offset
            self._dropdown.styles.offset = (off.x, off.y + 3)
            self._dropdown.styles.width = max(inp.size.width, 40)
        except Exception:
            pass

    def _refresh_topic_list(self) -> None:
        topics = [t.name for t in self._store.snapshot_topics()]
        pinned = self._store.snapshot_plot_topics()
        self._all_topics = sorted(set(topics + pinned))

    def _show_dropdown(self, query: str) -> None:
        if self._dropdown is None:
            return
        matches = ([t for t in self._all_topics if query.lower() in t.lower()]
                   if query else self._all_topics)
        self._dropdown.clear_options()
        if matches:
            self._dropdown.add_options(
                [Option(t, id=t.replace("/", "__")) for t in matches])
            self._reposition()
            self._dropdown.display = True
        else:
            self._dropdown.display = False

    def _hide_dropdown(self) -> None:
        if self._dropdown:
            self._dropdown.display = False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search_input":
            self._show_dropdown(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search_input":
            val = event.value.strip()
            if val:
                self._hide_dropdown()
                self.post_message(self.Selected(val))
                event.input.value = ""

    def on_key(self, event) -> None:
        if self._dropdown is None or not self._dropdown.display:
            return
        if event.key == "escape":
            self._hide_dropdown()
            event.stop()
        elif event.key == "down":
            self._dropdown.focus()
            event.stop()

    def clear_input(self) -> None:
        try:
            self.query_one("#search_input", Input).value = ""
        except NoMatches:
            pass
        self._hide_dropdown()


# ---------------------------------------------------------------------------
# Pinned topic chip — focusable, shows [×] when focused, Del/x to unpin
# ---------------------------------------------------------------------------


class TopicChip(Widget):
    """Single focusable chip for a pinned topic."""

    DEFAULT_CSS = """
    TopicChip {
        width: auto;
        height: 1;
        padding: 0 1;
        margin-right: 1;
        background: $surface-darken-1;
    }
    TopicChip:focus {
        background: $accent-darken-2;
    }
    """

    class UnpinRequested(Message):

        def __init__(self, topic: str) -> None:
            super().__init__()
            self.topic = topic

    def __init__(self, topic: str, color: str, **kwargs):
        super().__init__(**kwargs)
        self.can_focus = True
        self._topic = topic
        self._color = color

    def render(self) -> Text:
        t = Text(no_wrap=True)
        t.append("━ ", style=Style(color=self._color))
        label = self._topic.split("/")[-1] or self._topic
        t.append(label, style=Style(color=self._color, bold=self.has_focus))
        t.append(
            " [×]",
            style=Style(color="bright_red" if self.has_focus else "grey42"))
        return t

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    def on_key(self, event) -> None:
        if event.key in ("delete", "x", "backspace"):
            self.post_message(self.UnpinRequested(self._topic))
            event.stop()

    def on_click(self) -> None:
        if self.has_focus:
            self.post_message(self.UnpinRequested(self._topic))
        else:
            self.focus()


# ---------------------------------------------------------------------------
# Chip bar — horizontal row of TopicChips, keyboard-navigable
# ---------------------------------------------------------------------------


class PinnedTopicsBar(Widget):
    """
    Horizontal row of TopicChips. Plain Widget — ScrollableContainer swallows
    key events in Textual 0.82 before chips see them.
    Surgical DOM updates preserve focus across 1Hz refresh ticks.
    """

    DEFAULT_CSS = """
    PinnedTopicsBar {
        height: 1;
        layout: horizontal;
        overflow-x: auto;
        overflow-y: hidden;
        background: $surface-darken-1;
        padding: 0 1;
    }
    """

    class UnpinRequested(Message):

        def __init__(self, topic: str) -> None:
            super().__init__()
            self.topic = topic

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._current_topics: List[str] = []

    def set_chips(self, items: List[Tuple[str, str]]) -> None:
        new_topics = [t for t, _ in items]
        if new_topics == self._current_topics:
            return  # Nothing changed — skip entirely, focus preserved

        focused = self.app.focused
        focused_topic: Optional[str] = (focused._topic if isinstance(
            focused, TopicChip) else None)

        self._current_topics = new_topics
        new_set = set(new_topics)

        # Remove stale chips
        for chip in list(self.query(TopicChip)):
            if chip._topic not in new_set:
                chip.remove()

        # Remove empty hint if present
        try:
            self.query_one("#no_topics_hint").remove()
        except NoMatches:
            pass

        if not items:
            self.mount(
                Static(
                    "[dim]No topics pinned — search above to pin[/dim]",
                    id="no_topics_hint",
                ))
            return

        # Add only newly pinned chips
        existing = {c._topic for c in self.query(TopicChip)}
        for topic, color in items:
            if topic not in existing:
                chip_id = "chip_" + topic.replace("/", "__").replace(".", "_")
                self.mount(TopicChip(topic, color, id=chip_id))

        # Restore focus
        if focused_topic and focused_topic in new_set:
            chip_id = "#chip_" + focused_topic.replace("/", "__").replace(
                ".", "_")
            try:
                self.query_one(chip_id, TopicChip).focus()
            except NoMatches:
                pass

    def on_topic_chip_unpin_requested(self,
                                      event: TopicChip.UnpinRequested) -> None:
        self.post_message(self.UnpinRequested(event.topic))
        event.stop()

    def on_key(self, event) -> None:
        chips = list(self.query(TopicChip))
        if not chips:
            return
        focused = self.app.focused
        if focused not in chips:
            return
        idx = chips.index(focused)
        if event.key == "left" and idx > 0:
            chips[idx - 1].focus()
            event.stop()
        elif event.key == "right" and idx < len(chips) - 1:
            chips[idx + 1].focus()
            event.stop()


# ---------------------------------------------------------------------------
# PlotCanvas
# ---------------------------------------------------------------------------


class PlotCanvas(Static):
    DEFAULT_CSS = """
    PlotCanvas {
        height: 1fr;
        border: solid $accent;
        background: $surface-darken-2;
        overflow: hidden;
    }
    """

    def __init__(self, store: DataStore, **kwargs):
        super().__init__("", **kwargs)
        self._store = store
        self._window = 30

    def set_window(self, w: int) -> None:
        self._window = w

    def refresh_plot(self) -> None:
        series = self._store.snapshot_plot(window_seconds=self._window)
        markers = self._store.snapshot_param_changes()
        cw = max(self.size.width, 10)
        ch = max(self.size.height, 4)
        self.update(render_plot_text(series, markers, self._window, cw, ch))

    def get_pinned_with_colors(self) -> List[Tuple[str, str]]:
        series = self._store.snapshot_plot(window_seconds=self._window)
        return [(t, _COLORS[i % len(_COLORS)])
                for i, t in enumerate(series.keys())]


# ---------------------------------------------------------------------------
# VarianceTable
# ---------------------------------------------------------------------------


class VarianceTable(Static):
    DEFAULT_CSS = """
    VarianceTable { height: 3; padding: 0 1; background: $surface-darken-1; }
    """

    def refresh_variance(self, series, markers) -> None:
        if not markers or not series:
            self.update(
                Text(
                    "  Param change markers will appear as vertical lines on the plot",
                    style="dim",
                ))
            return
        last = markers[-1]
        mt = last.timestamp
        parts = Text()
        parts.append(f"Δ {last.param}", style="bold cyan")
        parts.append(f"  {last.old_value} → {last.new_value}   ",
                     style="white")
        for topic, points in series.items():
            before = [p.value for p in points if p.timestamp < mt]
            after = [p.value for p in points if p.timestamp >= mt]

            def var(v):
                return (sum((x - sum(v) / len(v))**2
                            for x in v) / len(v) if len(v) > 1 else 0.0)

            vb, va = var(before), var(after)
            d = va - vb
            col = "green" if d < 0 else "red"
            parts.append(f"  {topic.split('/')[-1] or topic}: ", style="dim")
            parts.append(f"{'↓' if d < 0 else '↑'}{abs(d):.3f}", style=col)
        self.update(parts)


# ---------------------------------------------------------------------------
# PlotPanel
# ---------------------------------------------------------------------------


class PlotPanel(Widget):
    BINDINGS = [
        Binding("ctrl+w", "cycle_window", "Cycle window"),
        Binding("ctrl+u", "focus_chips", "Go to chips"),
    ]

    DEFAULT_CSS = """
    PlotPanel { height: 1fr; }
    #controls_row {
        height: 3;
        padding: 0 1;
    }
    #controls_row Button { width: auto; margin-left: 1; }
    #window_btn { width: 20; }
    """

    def __init__(self, store: DataStore, bridge, **kwargs):
        super().__init__(**kwargs)
        self._store = store
        self._bridge = bridge
        self._window_idx = 1
        self._window = _WINDOW_OPTIONS[self._window_idx]

    def compose(self) -> ComposeResult:
        with Horizontal(id="controls_row"):
            yield TopicSearchBar(self._store, id="topic_search")
            yield Button(f"Window {self._window}s Ctrl+W",
                         id="window_btn",
                         variant="default")
        yield PinnedTopicsBar(id="pinned_bar")
        yield PlotCanvas(self._store, id="plot_canvas")
        yield VarianceTable(id="variance_table")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh)

    def on_key(self, event) -> None:
        import sys

        print(
            f"[PlotPanel] key={event.key} focused={type(self.app.focused).__name__}",
            file=sys.stderr,
            flush=True,
        )

    # -----------------------------------------------------------------------
    # Events
    # -----------------------------------------------------------------------

    def on_topic_search_bar_selected(self,
                                     event: TopicSearchBar.Selected) -> None:
        if self._bridge:
            self._bridge.pin_plot_topic(event.topic)
        else:
            self._store.add_plot_topic(event.topic)
        self.query_one("#topic_search", TopicSearchBar).clear_input()

    def on_pinned_topics_bar_unpin_requested(
            self, event: PinnedTopicsBar.UnpinRequested) -> None:
        if self._bridge:
            self._bridge.unpin_plot_topic(event.topic)
        else:
            self._store.remove_plot_topic(event.topic)

    # -----------------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------------

    def action_cycle_window(self) -> None:
        self._cycle_window()

    def action_focus_chips(self) -> None:
        chips = list(self.query(TopicChip))
        if chips:
            chips[0].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "window_btn":
            self._cycle_window()

    def _cycle_window(self) -> None:
        self._window_idx = (self._window_idx + 1) % len(_WINDOW_OPTIONS)
        self._window = _WINDOW_OPTIONS[self._window_idx]
        self.query_one("#window_btn",
                       Button).label = f"Window {self._window}s Ctrl+W"
        self.query_one("#plot_canvas", PlotCanvas).set_window(self._window)

    # -----------------------------------------------------------------------
    # Refresh loop
    # -----------------------------------------------------------------------

    def _refresh(self) -> None:
        canvas = self.query_one("#plot_canvas", PlotCanvas)
        canvas.set_window(self._window)
        canvas.refresh_plot()

        pinned = canvas.get_pinned_with_colors()
        self.query_one("#pinned_bar", PinnedTopicsBar).set_chips(pinned)

        series = self._store.snapshot_plot(window_seconds=self._window)
        markers = self._store.snapshot_param_changes()
        self.query_one("#variance_table",
                       VarianceTable).refresh_variance(series, markers)
