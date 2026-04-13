"""
panels/param_tuner.py — Parameter Tuner panel.
Full keyboard navigation. Enter=Apply, Ctrl+Z=Undo, Ctrl+E=Export YAML.
Read-only status fetched lazily per-param when row is highlighted.
"""

import subprocess
import threading
import time
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import DataTable, Select, Input, Label, Static
from textual.containers import Horizontal
from textual.binding import Binding
from rich.text import Text

from core.data_store import DataStore, ParamSnapshot


def _coerce_value(raw: str, type_name: str) -> Any:
    try:
        if type_name == "bool":
            return raw.lower() in ("true", "1", "yes")
        if type_name == "int":
            return int(raw)
        if type_name in ("float", "double"):
            return float(raw)
        return raw
    except ValueError:
        return raw


def _describe_one_param(node: str, param: str) -> Optional[bool]:
    """
    Returns True if read-only, False if writable, None on error.
    Uses `ros2 param describe <node> <param>`.
    """
    try:
        result = subprocess.run(
            ["ros2", "param", "describe", node, param],
            capture_output=True,
            text=True,
            timeout=6.0,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Read only:"):
                return "true" in stripped.lower()
    except Exception:
        pass
    return None


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        padding: 0 1;
        background: $surface-darken-1;
        color: $text-muted;
    }
    """


class ParamTunerPanel(Widget):
    BINDINGS = [
        Binding("enter", "apply_param", "Apply", show=True),
        Binding("ctrl+z", "undo_param", "Undo", show=True),
        Binding("ctrl+e", "export_yaml", "Export YAML", show=True),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
    ]

    DEFAULT_CSS = """
    ParamTunerPanel {
        height: 1fr;
        padding: 0 1;
        layout: vertical;
    }
    #node_select { height: 3; }
    #param_table { height: 1fr; }
    #edit_row {
        height: 3;
        padding: 0 1;
        background: $surface;
        border-top: solid $accent;
    }
    #edit_label  { width: 45; content-align: left middle; color: $text-muted; }
    #edit_input  { width: 1fr; }
    #hint_bar    { height: 1; }
    """

    def __init__(self, store, bridge, **kwargs):
        super().__init__(**kwargs)
        self._store = store
        self._bridge = bridge
        self._selected_node: Optional[str] = None
        self._selected_param: Optional[ParamSnapshot] = None
        self._history: List[tuple] = []
        self._ck: dict = {}
        self._suppress_select: bool = False
        self._known_nodes: List[str] = []
        # {node: {param_name: True/False}}  — True = read-only
        self._readonly_cache: Dict[str, Dict[str, bool]] = {}
        # params currently being described
        self._describing: Set[str] = set()

    def compose(self) -> ComposeResult:
        yield Select([], prompt="Select a node… (Tab to reach)", id="node_select")
        yield DataTable(id="param_table", zebra_stripes=True, cursor_type="row")
        with Horizontal(id="edit_row"):
            yield Label("No param selected", id="edit_label")
            yield Input(placeholder="new value… then Enter to apply", id="edit_input")
        yield StatusBar(
            "  Enter: Apply   Ctrl+Z: Undo   Ctrl+E: Export YAML   up/down: Navigate",
            id="hint_bar",
        )

    def on_mount(self) -> None:
        table = self.query_one("#param_table", DataTable)
        labels = ["Parameter", "Value", "Type", "R/W"]
        keys = table.add_columns(*labels)
        self._ck = dict(zip(labels, keys))
        self.set_interval(2.0, self._refresh_node_list)
        self.set_interval(1.0, self._refresh_params)

    # -----------------------------------------------------------------------
    # Keyboard actions
    # -----------------------------------------------------------------------

    def action_apply_param(self) -> None:
        self._apply_param()

    def action_undo_param(self) -> None:
        self._undo_last()

    def action_export_yaml(self) -> None:
        self._export_yaml()

    def action_cursor_up(self) -> None:
        self.query_one("#param_table", DataTable).action_scroll_up()

    def action_cursor_down(self) -> None:
        self.query_one("#param_table", DataTable).action_scroll_down()

    # -----------------------------------------------------------------------
    # Node list
    # -----------------------------------------------------------------------

    def _refresh_node_list(self) -> None:
        live = [n.name for n in self._store.snapshot_nodes()]
        cached = self._store.snapshot_param_nodes()
        all_nodes = sorted(set(live + cached))
        if all_nodes == self._known_nodes:
            return
        self._known_nodes = all_nodes
        sel = self.query_one("#node_select", Select)
        self._suppress_select = True
        try:
            sel.set_options([(n, n) for n in all_nodes])
            if self._selected_node and self._selected_node in all_nodes:
                sel.value = self._selected_node
        finally:
            self._suppress_select = False

    # -----------------------------------------------------------------------
    # Lazy per-param read-only check
    # -----------------------------------------------------------------------

    def _get_readonly(self, node: str, param: str) -> Optional[bool]:
        """Return cached value or None if not yet known."""
        return self._readonly_cache.get(node, {}).get(param)

    def _fetch_readonly_async(self, node: str, param: str) -> None:
        """Kick off background describe for a single param if not cached."""
        key = f"{node}::{param}"
        if key in self._describing:
            return
        if param in self._readonly_cache.get(node, {}):
            return
        self._describing.add(key)

        def _worker():
            ro = _describe_one_param(node, param)
            if ro is not None:
                if node not in self._readonly_cache:
                    self._readonly_cache[node] = {}
                self._readonly_cache[node][param] = ro
            self._describing.discard(key)
            # Refresh table cell and edit row
            self.app.call_from_thread(self._update_rw_cell, node, param, ro)

        threading.Thread(target=_worker, daemon=True, name=f"ro_{param[:20]}").start()

    def _update_rw_cell(self, node: str, param: str, ro: Optional[bool]) -> None:
        """Called from thread after describe completes — update cell and edit row."""
        if ro is None:
            return
        try:
            table = self.query_one("#param_table", DataTable)
            rw_text = (
                Text("RO", style="bold red") if ro else Text("RW", style="bold green")
            )
            table.update_cell(param, self._ck["R/W"], rw_text)
        except Exception:
            pass
        # Also update edit row if this is the currently selected param
        if (
            self._selected_param
            and self._selected_param.name == param
            and self._selected_node == node
        ):
            self._update_edit_row(ro)

    def _update_edit_row(self, ro: bool) -> None:
        if not self._selected_param:
            return
        inp = self.query_one("#edit_input", Input)
        if ro:
            self.query_one("#edit_label", Label).update(
                f"[red]RO  {self._selected_param.name}[/red] "
                f"[dim]({self._selected_param.type_name})  read-only[/dim]"
            )
            inp.disabled = True
        else:
            self.query_one("#edit_label", Label).update(
                f"[cyan]{self._selected_param.name}[/cyan] "
                f"[dim]({self._selected_param.type_name})[/dim]"
            )
            inp.disabled = False

    # -----------------------------------------------------------------------
    # Param table
    # -----------------------------------------------------------------------

    def _refresh_params(self) -> None:
        if not self._selected_node:
            return
        params = self._store.snapshot_params(self._selected_node)
        table = self.query_one("#param_table", DataTable)
        existing_keys = {rk.value for rk in table.rows.keys()}
        new_keys = {p.name for p in params}

        for rk in list(table.rows.keys()):
            if rk.value not in new_keys:
                table.remove_row(rk)

        for p in sorted(params, key=lambda x: x.name):
            ro = self._get_readonly(self._selected_node, p.name)
            if ro is True:
                rw_text = Text("RO", style="bold red")
            elif ro is False:
                rw_text = Text("RW", style="bold green")
            else:
                rw_text = Text("...", style="dim")

            val_text = Text(str(p.value))
            type_text = Text(p.type_name, style="dim")

            if p.name in existing_keys:
                table.update_cell(p.name, self._ck["Value"], val_text)
                table.update_cell(p.name, self._ck["R/W"], rw_text)
            else:
                table.add_row(
                    Text(p.name, style="cyan"),
                    val_text,
                    type_text,
                    rw_text,
                    key=p.name,
                )

    # -----------------------------------------------------------------------
    # Events
    # -----------------------------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._suppress_select:
            return
        if not event.value or event.value is Select.BLANK:
            return
        node = str(event.value)
        if node == self._selected_node:
            return
        self._selected_node = node
        self.query_one("#param_table", DataTable).clear()
        if self._bridge:
            self._bridge.fetch_params(self._selected_node)
        self._set_status(f"Fetching params for {self._selected_node}…")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not event.row_key or not self._selected_node:
            return
        params = {p.name: p for p in self._store.snapshot_params(self._selected_node)}
        self._selected_param = params.get(event.row_key.value)
        if not self._selected_param:
            return

        inp = self.query_one("#edit_input", Input)
        inp.value = str(self._selected_param.value)

        ro = self._get_readonly(self._selected_node, self._selected_param.name)
        if ro is None:
            # Not cached yet — show as editable optimistically, kick off fetch
            self.query_one("#edit_label", Label).update(
                f"[cyan]{self._selected_param.name}[/cyan] "
                f"[dim]({self._selected_param.type_name})  checking…[/dim]"
            )
            inp.disabled = False
            self._fetch_readonly_async(self._selected_node, self._selected_param.name)
        else:
            self._update_edit_row(ro)

    # -----------------------------------------------------------------------
    # Operations
    # -----------------------------------------------------------------------

    def _apply_param(self) -> None:
        if not self._selected_param or not self._selected_node or not self._bridge:
            self._set_status("Select a node and parameter first.")
            return

        ro = self._get_readonly(self._selected_node, self._selected_param.name)
        if ro is True:
            self._set_status(f"  {self._selected_param.name} is read-only.")
            return

        inp = self.query_one("#edit_input", Input)
        if inp.disabled:
            return
        raw = inp.value.strip()
        if not raw:
            return

        new_val = _coerce_value(raw, self._selected_param.type_name)
        old_val = self._selected_param.value
        self._history.append(
            (self._selected_node, self._selected_param.name, old_val, new_val)
        )
        self._set_status(f"Setting {self._selected_param.name} = {new_val}…")

        param_name = self._selected_param.name

        def on_done(success: bool, error: str) -> None:
            if success:
                msg = f"  {param_name} = {new_val}"
            else:
                msg = f"  Failed: {error}"
                # Runtime revealed it's read-only — cache and update UI
                if "read-only" in error.lower() or "cannot be set" in error.lower():
                    node = self._selected_node
                    if node not in self._readonly_cache:
                        self._readonly_cache[node] = {}
                    self._readonly_cache[node][param_name] = True
                    self.app.call_from_thread(
                        self._update_rw_cell, node, param_name, True
                    )
            self.app.call_from_thread(self._set_status, msg)

        self._bridge.set_param(
            self._selected_node, self._selected_param.name, new_val, on_done=on_done
        )

    def _undo_last(self) -> None:
        if not self._history or not self._bridge:
            self._set_status("Nothing to undo.")
            return
        node, param, old_val, _ = self._history.pop()
        self._set_status(f"Undoing {param} -> {old_val}…")

        def on_done(success: bool, error: str) -> None:
            msg = (
                f"  Undone: {param} = {old_val}"
                if success
                else f"  Undo failed: {error}"
            )
            self.app.call_from_thread(self._set_status, msg)

        self._bridge.set_param(node, param, old_val, on_done=on_done)

    def _export_yaml(self) -> None:
        if not self._selected_node:
            self._set_status("No node selected.")
            return
        params = self._store.snapshot_params(self._selected_node)
        if not params:
            self._set_status("No params to export.")
            return
        node_bare = self._selected_node.lstrip("/").replace("/", "_")
        data = {node_bare: {"ros__parameters": {p.name: p.value for p in params}}}
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"params_{node_bare}_{ts}.yaml"
        Path(filename).write_text(yaml.dump(data, default_flow_style=False))
        self._set_status(f"  Exported -> {filename}")

    def _set_status(self, msg: str) -> None:
        self.query_one("#hint_bar", StatusBar).update(
            f"{msg}   [dim]Enter: Apply  Ctrl+Z: Undo  Ctrl+E: Export[/dim]"
        )
