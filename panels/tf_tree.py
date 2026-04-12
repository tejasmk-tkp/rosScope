"""
panels/tf_tree.py — TF Tree panel.

Shows the live TF tree as an ASCII hierarchy with per-edge staleness colouring.
Dynamic (/tf) and static (/tf_static) transforms displayed separately.

Staleness thresholds are configurable inline (warn_ms / error_ms inputs).

Colour coding (dynamic transforms only):
  green      age < warn_ms
  yellow     warn_ms <= age < error_ms
  bold red   age >= error_ms   ← likely causing navigation failures
  dim        static transforms (no age — they don't republish)

Layout:
  ┌─────────────────────────────────────────────────────┐
  │ Thresholds:  warn [100] ms   error [500] ms  [Apply]│
  ├───────────────────────────┬─────────────────────────┤
  │ Dynamic TF tree           │ Static TF tree (greyed) │
  │ (live staleness)          │ (/tf_static, no age)    │
  └───────────────────────────┴─────────────────────────┘
"""

import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Input, Button, Label
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.binding import Binding
from textual.css.query import NoMatches
from rich.text import Text

from core.data_store import DataStore, TFTransform

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_WARN_MS  = 100
_DEFAULT_ERROR_MS = 500


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------

def _build_tree(transforms: List[TFTransform]) -> Dict[str, List[str]]:
    """Build parent→[children] adjacency from a flat list of transforms."""
    children: Dict[str, List[str]] = defaultdict(list)
    all_children: Set[str] = set()
    for tf in transforms:
        children[tf.parent].append(tf.child)
        all_children.add(tf.child)
    return children, all_children


def _render_dynamic_tree(
    transforms: List[TFTransform],
    warn_ms: float,
    error_ms: float,
    now: float,
) -> Text:
    if not transforms:
        return Text("  No dynamic transforms received yet", style="dim")

    children, all_children = _build_tree(transforms)
    all_parents = set(children.keys())
    roots = sorted(all_parents - all_children)
    if not roots:
        counts = {p: len(c) for p, c in children.items()}
        roots = [max(counts, key=counts.get)]

    # Map child -> transform for quick lookup
    child_tf: Dict[str, TFTransform] = {t.child: t for t in transforms}

    result = Text()

    def _get_age_s(tf: TFTransform) -> Tuple[float, bool]:
        """Returns (age_seconds, is_stamp_based).
        Uses ROS stamp age if available, falls back to wall-clock last_received age."""
        if tf.stamp_age_s >= 0:
            return tf.stamp_age_s, True
        return now - tf.last_received, False

    def _age_style(age_s: float) -> Tuple[str, str]:
        age_ms = age_s * 1000
        label = f"{age_ms:6.1f}ms"
        if age_ms < warn_ms:
            return "bright_green", label
        elif age_ms < error_ms:
            return "yellow", label
        else:
            return "bright_red", label

    def _walk(frame: str, prefix: str, is_last: bool) -> None:
        connector = "└── " if is_last else "├── "
        extension = "    " if is_last else "│   "
        result.append(prefix + connector, style="bright_black")

        tf = child_tf.get(frame)
        if tf is not None:
            age_s, stamp_based = _get_age_s(tf)
            color, age_label = _age_style(age_s)
            is_stale = age_s * 1000 >= error_ms
            frame_style = ("bold " + color) if is_stale else color
            result.append(frame, style=frame_style)
            age_source = "stamp" if stamp_based else "recv"
            result.append(f"  [{age_label} {age_source}]", style=color)
            if is_stale:
                result.append(" ✗ STALE", style="bold bright_red")
            elif age_s * 1000 >= warn_ms:
                result.append(" ⚠", style="yellow")
        else:
            result.append(frame, style="cyan")  # root frame

        result.append("\n")
        kids = sorted(children.get(frame, []))
        for i, kid in enumerate(kids):
            _walk(kid, prefix + extension, i == len(kids) - 1)

    for i, root in enumerate(roots):
        result.append(f"  {root}\n", style="bold cyan")
        kids = sorted(children.get(root, []))
        for j, kid in enumerate(kids):
            _walk(kid, "  ", j == len(kids) - 1)
        if i < len(roots) - 1:
            result.append("\n")

    return result


def _render_static_tree(transforms: List[TFTransform]) -> Text:
    if not transforms:
        return Text("  No static transforms", style="dim")

    children, all_children = _build_tree(transforms)
    all_parents = set(children.keys())
    roots = sorted(all_parents - all_children) or sorted(all_parents)[:1]

    result = Text()

    def _walk(frame: str, prefix: str, is_last: bool) -> None:
        connector = "└── " if is_last else "├── "
        extension = "    " if is_last else "│   "
        result.append(prefix + connector, style="bright_black")
        result.append(f"{frame}  [static]\n",
                      style="grey62 italic")
        kids = sorted(children.get(frame, []))
        for i, kid in enumerate(kids):
            _walk(kid, prefix + extension, i == len(kids) - 1)

    for i, root in enumerate(roots):
        result.append(f"  {root}  [static root]\n",
                      style="grey62 italic")
        kids = sorted(children.get(root, []))
        for j, kid in enumerate(kids):
            _walk(kid, "  ", j == len(kids) - 1)

    return result


# ---------------------------------------------------------------------------
# Threshold bar
# ---------------------------------------------------------------------------

class ThresholdBar(Horizontal):
    DEFAULT_CSS = """
    ThresholdBar {
        height: 3;
        padding: 0 1;
        background: $surface-darken-1;
        border-bottom: solid $accent;
    }
    ThresholdBar Label  { width: auto; content-align: left middle; padding: 0 1; }
    ThresholdBar Input  { width: 8; height: 3; }
    ThresholdBar Button { width: 10; margin-left: 1; }
    """

    class ThresholdsChanged(object):
        def __init__(self, warn_ms: float, error_ms: float):
            self.warn_ms  = warn_ms
            self.error_ms = error_ms

    def compose(self) -> ComposeResult:
        yield Label("Warn:")
        yield Input(str(_DEFAULT_WARN_MS),  id="warn_input",  type="integer")
        yield Label("ms   Error:")
        yield Input(str(_DEFAULT_ERROR_MS), id="error_input", type="integer")
        yield Label("ms")
        yield Button("Apply", id="apply_btn", variant="primary")

    def get_thresholds(self) -> Tuple[float, float]:
        try:
            warn  = float(self.query_one("#warn_input",  Input).value)
            error = float(self.query_one("#error_input", Input).value)
            return warn, error
        except (ValueError, NoMatches):
            return _DEFAULT_WARN_MS, _DEFAULT_ERROR_MS


# ---------------------------------------------------------------------------
# Tree view widgets
# ---------------------------------------------------------------------------

class DynamicTreeView(ScrollableContainer):
    DEFAULT_CSS = """
    DynamicTreeView {
        width: 1fr;
        height: 1fr;
        border: solid $accent;
        overflow-y: auto;
        overflow-x: auto;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="dynamic_content")

    def update(self, text: Text) -> None:
        try:
            self.query_one("#dynamic_content", Static).update(text)
        except NoMatches:
            pass


class StaticTreeView(ScrollableContainer):
    DEFAULT_CSS = """
    StaticTreeView {
        width: 1fr;
        height: 1fr;
        border: solid $accent;
        overflow-y: auto;
        overflow-x: auto;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="static_content")

    def update(self, text: Text) -> None:
        try:
            self.query_one("#static_content", Static).update(text)
        except NoMatches:
            pass


# ---------------------------------------------------------------------------
# TFTreePanel
# ---------------------------------------------------------------------------

class TFTreePanel(Widget):
    DEFAULT_CSS = """
    TFTreePanel {
        height: 1fr;
    }
    #tf_trees {
        height: 1fr;
        layout: horizontal;
    }
    #dynamic_header {
        height: 1;
        background: $surface-darken-2;
        padding: 0 1;
        color: cyan;
    }
    #static_header {
        height: 1;
        background: $surface-darken-2;
        padding: 0 1;
        color: grey;
    }
    #dynamic_col { width: 1fr; height: 1fr; }
    #static_col  { width: 1fr; height: 1fr; border-left: solid $accent; }
    """

    BINDINGS = [
        Binding("r", "refresh_now", "Refresh TF", show=False),
    ]

    def __init__(self, store: DataStore, **kwargs):
        super().__init__(**kwargs)
        self._store    = store
        self._warn_ms  = float(_DEFAULT_WARN_MS)
        self._error_ms = float(_DEFAULT_ERROR_MS)

    def compose(self) -> ComposeResult:
        yield ThresholdBar(id="threshold_bar")
        with Horizontal(id="tf_trees"):
            with Vertical(id="dynamic_col"):
                yield Static(
                    " ⟳ Dynamic TF  (/tf)  — live staleness",
                    id="dynamic_header"
                )
                yield DynamicTreeView(id="dynamic_tree")
            with Vertical(id="static_col"):
                yield Static(
                    " ◈ Static TF  (/tf_static)",
                    id="static_header"
                )
                yield StaticTreeView(id="static_tree")

    def on_mount(self) -> None:
        self.set_interval(0.25, self._refresh)   # 4Hz — fast enough to see staleness

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply_btn":
            bar = self.query_one("#threshold_bar", ThresholdBar)
            self._warn_ms, self._error_ms = bar.get_thresholds()

    def action_refresh_now(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        dynamic, static = self._store.snapshot_tf()
        now = time.monotonic()

        dyn_text = _render_dynamic_tree(dynamic, self._warn_ms, self._error_ms, now)
        sta_text = _render_static_tree(static)

        self.query_one("#dynamic_tree", DynamicTreeView).update(dyn_text)
        self.query_one("#static_tree",  StaticTreeView).update(sta_text)

        # Update dynamic header with summary counts
        def _age_ms(t: "TFTransform") -> float:
            if t.stamp_age_s >= 0:
                return t.stamp_age_s * 1000
            return (now - t.last_received) * 1000

        total = len(dynamic)
        stale = sum(1 for t in dynamic if _age_ms(t) >= self._error_ms)
        warn  = sum(1 for t in dynamic if self._warn_ms <= _age_ms(t) < self._error_ms)

        if stale:
            status = f"[bold bright_red] {stale} STALE[/bold bright_red]"
        elif warn:
            status = f"[yellow] {warn} slow[/yellow]"
        else:
            status = f"[bright_green] all ok[/bright_green]"

        self.query_one("#dynamic_header", Static).update(
            f" ⟳ Dynamic TF  ({total} edges){status}   "
            f"[dim]warn={self._warn_ms:.0f}ms  error={self._error_ms:.0f}ms[/dim]"
        )
