"""
panels/plot_panel.py — Time-series plot panel.
Braille dot renderer via Static.update() — works reliably in all Textual versions.
"""

import math
import time
from typing import Dict, List, Optional, Tuple

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Input, Button, Label
from textual.containers import Horizontal, Vertical
from textual.binding import Binding
from rich.text import Text
from rich.style import Style

from core.data_store import DataStore, PlotPoint, ParamChangeMarker

# ---------------------------------------------------------------------------
# Braille renderer
# ---------------------------------------------------------------------------
_BRAILLE_BASE = 0x2800
# dot_map[col][row] — col in {0,1}, row in {0,1,2,3}
_DOT_MAP = [
    [0x01, 0x02, 0x04, 0x40],
    [0x08, 0x10, 0x20, 0x80],
]
_COLORS = [
    "bright_cyan", "bright_red", "bright_yellow",
    "bright_green", "bright_magenta", "bright_blue", "white",
]
_WINDOW_OPTIONS = [10, 30, 60, 120, 300]


def _make_canvas(w: int, h: int) -> List[List[int]]:
    return [[0] * w for _ in range(h)]


def _plot_dot(canvas, dot_x: int, dot_y: int, cw: int, ch: int):
    cx, bit_col = divmod(max(0, min(dot_x, cw * 2 - 1)), 2)
    cy, bit_row = divmod(max(0, min(dot_y, ch * 4 - 1)), 4)
    if 0 <= cx < cw and 0 <= cy < ch:
        canvas[cy][cx] |= _DOT_MAP[bit_col][bit_row]


def _draw_line_on_canvas(canvas, x0n: float, y0: float, x1n: float, y1: float,
                          cw: int, ch: int, ymin: float, ymax: float):
    """Draw a line between two normalised-x, data-y points."""
    dw, dh = cw * 2, ch * 4
    yr = ymax - ymin if ymax != ymin else 1.0

    def to_dots(xn, yv):
        dx = int(xn * (dw - 1))
        dy = int((1.0 - (yv - ymin) / yr) * (dh - 1))
        return dx, dy

    dx0, dy0 = to_dots(x0n, y0)
    dx1, dy1 = to_dots(x1n, y1)
    steps = max(abs(dx1 - dx0), abs(dy1 - dy0), 1)
    for i in range(steps + 1):
        t = i / steps
        _plot_dot(canvas, int(dx0 + t*(dx1-dx0)), int(dy0 + t*(dy1-dy0)), cw, ch)


def _draw_vline(canvas, xn: float, cw: int, ch: int):
    dw = cw * 2
    dx = int(xn * (dw - 1))
    for dy in range(ch * 4):
        _plot_dot(canvas, dx, dy, cw, ch)


def render_plot_text(series: Dict[str, List[PlotPoint]],
                     markers: List[ParamChangeMarker],
                     window: int,
                     cw: int, ch: int) -> Text:
    """
    Render the full plot as a Rich Text object (braille chars + colours).
    cw/ch = character cell width/height of the canvas area.
    """
    if cw < 4 or ch < 4:
        return Text("(too small)")

    now = time.monotonic()
    t0  = now - window

    # Y range
    all_vals = [p.value for pts in series.values() for p in pts]
    if not all_vals:
        msg = Text(" No data — pin a topic above", style="dim")
        return msg

    ymin, ymax = min(all_vals), max(all_vals)
    if ymin == ymax:
        ymin -= 1.0; ymax += 1.0

    # One canvas per topic
    topic_layers: List[Tuple[List[List[int]], str]] = []
    for idx, (topic, points) in enumerate(series.items()):
        color = _COLORS[idx % len(_COLORS)]
        layer = _make_canvas(cw, ch)
        pts = [p for p in points if p.timestamp >= t0]
        for i in range(len(pts) - 1):
            xn0 = (pts[i].timestamp   - t0) / window
            xn1 = (pts[i+1].timestamp - t0) / window
            _draw_line_on_canvas(layer, xn0, pts[i].value,
                                         xn1, pts[i+1].value,
                                 cw, ch, ymin, ymax)
        topic_layers.append((layer, color))

    # Marker canvas (white)
    mcanvas = _make_canvas(cw, ch)
    for m in markers:
        if t0 <= m.timestamp <= now:
            _draw_vline(mcanvas, (m.timestamp - t0) / window, cw, ch)

    # Y-axis tick positions
    tick_rows = {0, ch//4, ch//2, 3*ch//4, ch-1}

    result = Text()
    for cy in range(ch):
        # Y axis label (6 chars wide)
        if cy in tick_rows:
            frac = 1.0 - cy / max(ch - 1, 1)
            label = f"{ymin + frac*(ymax-ymin):6.2f} "
        else:
            label = "       "
        result.append(label, style=Style(color="bright_black"))

        for cx in range(cw - 7):   # subtract y-axis width
            # Marker takes priority
            if mcanvas[cy][cx]:
                char = chr(_BRAILLE_BASE | mcanvas[cy][cx])
                result.append(char, style=Style(color="white"))
                continue
            # Topic layers
            drawn = False
            for layer, color in topic_layers:
                if layer[cy][cx]:
                    char = chr(_BRAILLE_BASE | layer[cy][cx])
                    result.append(char, style=Style(color=color))
                    drawn = True
                    break
            if not drawn:
                result.append(chr(_BRAILLE_BASE))   # empty braille = space-like

        result.append("\n")

    return result


# ---------------------------------------------------------------------------
# Widgets
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
        self._legend_items: List[Tuple[str, str]] = []

    def set_window(self, w: int) -> None:
        self._window = w

    def refresh_plot(self) -> None:
        series  = self._store.snapshot_plot(window_seconds=self._window)
        markers = self._store.snapshot_param_changes()
        # Size available: subtract y-axis label width from cw
        cw = max(self.size.width,  10)
        ch = max(self.size.height, 4)
        txt = render_plot_text(series, markers, self._window, cw, ch)
        self.update(txt)
        # Update legend
        self._legend_items = [
            (t.split("/")[-1] or t, _COLORS[i % len(_COLORS)])
            for i, t in enumerate(series.keys())
        ]

    def get_legend(self) -> List[Tuple[str, str]]:
        return self._legend_items


class LegendBar(Static):
    DEFAULT_CSS = """
    LegendBar {
        height: 1;
        padding: 0 1;
        background: $surface-darken-1;
    }
    """
    def set_legend(self, items: List[Tuple[str, str]]) -> None:
        t = Text()
        for i, (name, color) in enumerate(items):
            if i: t.append("   ")
            t.append("━ ", style=Style(color=color))
            t.append(name, style=Style(color=color))
        if not items:
            t.append("No topics pinned — type a topic name above and press Enter or Ctrl+P",
                     style="dim")
        self.update(t)


class VarianceTable(Static):
    DEFAULT_CSS = """
    VarianceTable {
        height: 3;
        padding: 0 1;
        background: $surface-darken-1;
    }
    """
    def refresh_variance(self, series, markers) -> None:
        if not markers or not series:
            self.update(Text("  Param change markers will appear here as vertical lines on the plot", style="dim"))
            return
        last = markers[-1]
        mt   = last.timestamp
        parts = Text()
        parts.append(f"Δ {last.param}", style="bold cyan")
        parts.append(f"  {last.old_value} → {last.new_value}   ", style="white")
        for topic, points in series.items():
            before = [p.value for p in points if p.timestamp < mt]
            after  = [p.value for p in points if p.timestamp >= mt]
            def var(v): return sum((x - sum(v)/len(v))**2 for x in v)/len(v) if len(v)>1 else 0.0
            vb, va = var(before), var(after)
            d = va - vb
            col = "green" if d < 0 else "red"
            short = topic.split("/")[-1] or topic
            parts.append(f"  {short}: ", style="dim")
            parts.append(f"{'↓' if d<0 else '↑'}{abs(d):.3f}", style=col)
        self.update(parts)


# ---------------------------------------------------------------------------
# PlotPanel
# ---------------------------------------------------------------------------

class PlotPanel(Widget):

    BINDINGS = [
        Binding("ctrl+n", "pin",          "Pin topic"),
        Binding("ctrl+d", "unpin",        "Unpin topic"),
        Binding("ctrl+w", "cycle_window", "Cycle window"),
    ]

    DEFAULT_CSS = """
    PlotPanel {
        height: 1fr;
    }
    #plot_controls { height: 3; padding: 0 1; }
    #topic_input   { width: 1fr; }
    #pin_btn       { width: 14; }
    #unpin_btn     { width: 14; }
    #window_btn    { width: 20; }
    """

    def __init__(self, store: DataStore, bridge, **kwargs):
        super().__init__(**kwargs)
        self._store = store
        self._bridge = bridge
        self._window_idx = 1
        self._window = _WINDOW_OPTIONS[self._window_idx]

    def compose(self) -> ComposeResult:
        with Horizontal(id="plot_controls"):
            yield Input(placeholder="topic e.g. /cmd_vel  →  Enter or Ctrl+N to pin",
                        id="topic_input")
            yield Button("Pin  Ctrl+N",    id="pin_btn",   variant="primary")
            yield Button("Unpin Ctrl+D",   id="unpin_btn", variant="warning")
            yield Button(f"Window {self._window}s Ctrl+W", id="window_btn", variant="default")
        yield LegendBar(id="legend_bar")
        yield PlotCanvas(self._store, id="plot_canvas")
        yield VarianceTable(id="variance_table")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh)

    def action_pin(self)          -> None: self._pin_topic()
    def action_unpin(self)        -> None: self._unpin_topic()
    def action_cycle_window(self) -> None: self._cycle_window()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pin_btn":    self._pin_topic()
        elif event.button.id == "unpin_btn": self._unpin_topic()
        elif event.button.id == "window_btn": self._cycle_window()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "topic_input":
            self._pin_topic()

    def _pin_topic(self) -> None:
        t = self.query_one("#topic_input", Input).value.strip()
        if t and self._bridge:
            self._bridge.pin_plot_topic(t)

    def _unpin_topic(self) -> None:
        t = self.query_one("#topic_input", Input).value.strip()
        if t and self._bridge:
            self._bridge.unpin_plot_topic(t)

    def _cycle_window(self) -> None:
        self._window_idx = (self._window_idx + 1) % len(_WINDOW_OPTIONS)
        self._window = _WINDOW_OPTIONS[self._window_idx]
        self.query_one("#window_btn", Button).label = f"Window {self._window}s Ctrl+W"
        self.query_one("#plot_canvas", PlotCanvas).set_window(self._window)

    def _refresh(self) -> None:
        canvas = self.query_one("#plot_canvas", PlotCanvas)
        canvas.set_window(self._window)
        canvas.refresh_plot()
        self.query_one("#legend_bar", LegendBar).set_legend(canvas.get_legend())
        series  = self._store.snapshot_plot(window_seconds=self._window)
        markers = self._store.snapshot_param_changes()
        self.query_one("#variance_table", VarianceTable).refresh_variance(series, markers)
