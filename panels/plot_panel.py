"""
panels/plot_panel.py — Time-series plot panel.

Pin topics → see them as overlaid time-series graphs.
Parameter change markers dropped as vertical lines.
Before/after variance shown in a summary table.
Uses plotext rendered into a Textual Static widget.
"""

import time
from typing import Dict, List, Optional

import plotext as plt
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Input, Label, Button, DataTable
from textual.containers import Horizontal, Vertical
from rich.text import Text

from core.data_store import DataStore, PlotPoint, ParamChangeMarker


# Colours cycled per topic (plotext colour names)
_TOPIC_COLORS = ["cyan", "red", "yellow", "green", "magenta", "blue", "white"]

# Window options (seconds)
_WINDOW_OPTIONS = [10, 30, 60, 120, 300]


class PlotCanvas(Static):
    """Renders plotext output into a Textual Static."""

    DEFAULT_CSS = """
    PlotCanvas {
        height: 1fr;
        border: solid $accent;
    }
    """

    def render_plot(self,
                    series: Dict[str, List[PlotPoint]],
                    markers: List[ParamChangeMarker],
                    window: int) -> None:
        """Re-render the plot with current data."""
        if not series:
            self.update("[dim]No topics pinned. Use the input below to add one.[/dim]")
            return

        # Get terminal size for plotext
        w, h = self.size.width, self.size.height
        if w < 10 or h < 5:
            return

        plt.clf()
        plt.plotsize(w, h - 1)
        plt.theme("dark")

        now = time.monotonic()
        t0 = now - window

        has_data = False
        for idx, (topic, points) in enumerate(series.items()):
            color = _TOPIC_COLORS[idx % len(_TOPIC_COLORS)]
            if not points:
                continue

            xs = [p.timestamp - t0 for p in points]
            ys = [p.value for p in points]
            short_label = topic.split("/")[-1] or topic
            plt.plot(xs, ys, label=short_label, color=color)
            has_data = True

        if not has_data:
            self.update("[dim]Waiting for data…[/dim]")
            return

        # Param change markers as vertical lines
        for m in markers:
            x = m.timestamp - t0
            if 0 <= x <= window:
                label = f"{m.param.split('.')[-1]}={m.new_value}"
                plt.vline(x, color="white")

        plt.xlabel(f"seconds (last {window}s)")
        plt.ylabel("value")
        plt.title("Topic Plot")
        plt.yfrequency(5)
        plt.xfrequency(10)

        try:
            rendered = plt.build()
            self.update(rendered)
        except Exception as e:
            self.update(f"[red]Plot error: {e}[/red]")


class VarianceTable(Static):
    """Before/after variance summary when a param change marker exists."""

    DEFAULT_CSS = """
    VarianceTable {
        height: 5;
        border: solid $panel;
        padding: 0 1;
        overflow-x: auto;
    }
    """

    def refresh_variance(self,
                         series: Dict[str, List[PlotPoint]],
                         markers: List[ParamChangeMarker]) -> None:
        if not markers or not series:
            self.update("[dim]Param change markers will show before/after variance here.[/dim]")
            return

        last_marker = markers[-1]
        mt = last_marker.timestamp

        rows = []
        for topic, points in series.items():
            before = [p.value for p in points if p.timestamp < mt]
            after  = [p.value for p in points if p.timestamp >= mt]

            def variance(vals):
                if len(vals) < 2:
                    return 0.0
                mean = sum(vals) / len(vals)
                return sum((v - mean) ** 2 for v in vals) / len(vals)

            vb = variance(before)
            va = variance(after)
            delta = va - vb
            arrow = "↓" if delta < 0 else "↑"
            color = "green" if delta < 0 else "red"
            short = topic.split("/")[-1] or topic
            rows.append(
                f"[cyan]{short:20s}[/cyan]  "
                f"var_before={vb:8.4f}  var_after={va:8.4f}  "
                f"[{color}]{arrow} {abs(delta):.4f}[/{color}]"
            )

        marker_label = (
            f"[bold]Last change:[/bold] {last_marker.node} "
            f"[cyan]{last_marker.param}[/cyan] "
            f"{last_marker.old_value} → {last_marker.new_value}"
        )
        self.update(marker_label + "\n" + "\n".join(rows))


class PlotPanel(Widget):

    DEFAULT_CSS = """
    PlotPanel {
        height: 1fr;
        padding: 0 1;
    }
    #plot_controls {
        height: 3;
        dock: top;
    }
    #topic_input  { width: 1fr; }
    #pin_btn      { width: 8; }
    #unpin_btn    { width: 10; }
    #window_btn   { width: 16; }
    """

    def __init__(self, store: DataStore, bridge, **kwargs):
        super().__init__(**kwargs)
        self._store = store
        self._bridge = bridge
        self._window_idx = 1          # default 30s
        self._window = _WINDOW_OPTIONS[self._window_idx]

    def compose(self) -> ComposeResult:
        with Horizontal(id="plot_controls"):
            yield Input(placeholder="topic to pin e.g. /cmd_vel", id="topic_input")
            yield Button("Pin",   id="pin_btn",    variant="primary")
            yield Button("Unpin", id="unpin_btn",  variant="warning")
            yield Button(f"Window: {self._window}s", id="window_btn", variant="default")
        yield PlotCanvas(id="plot_canvas")
        yield VarianceTable(id="variance_table")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pin_btn":
            self._pin_topic()
        elif event.button.id == "unpin_btn":
            self._unpin_topic()
        elif event.button.id == "window_btn":
            self._cycle_window()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "topic_input":
            self._pin_topic()

    def _pin_topic(self) -> None:
        topic = self.query_one("#topic_input", Input).value.strip()
        if topic and self._bridge:
            self._bridge.pin_plot_topic(topic)

    def _unpin_topic(self) -> None:
        topic = self.query_one("#topic_input", Input).value.strip()
        if topic and self._bridge:
            self._bridge.unpin_plot_topic(topic)

    def _cycle_window(self) -> None:
        self._window_idx = (self._window_idx + 1) % len(_WINDOW_OPTIONS)
        self._window = _WINDOW_OPTIONS[self._window_idx]
        btn = self.query_one("#window_btn", Button)
        btn.label = f"Window: {self._window}s"

    def _refresh(self) -> None:
        series  = self._store.snapshot_plot(window_seconds=self._window)
        markers = self._store.snapshot_param_changes()

        canvas = self.query_one("#plot_canvas", PlotCanvas)
        canvas.render_plot(series, markers, self._window)

        vtable = self.query_one("#variance_table", VarianceTable)
        vtable.refresh_variance(series, markers)
