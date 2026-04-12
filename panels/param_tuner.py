"""
panels/param_tuner.py — Parameter Tuner panel.
Full keyboard navigation. Enter=Apply, Ctrl+Z=Undo, Ctrl+E=Export YAML.
"""

import time
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import DataTable, Select, Input, Label, Static, Button
from textual.containers import Vertical, Horizontal
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
        Binding("enter",   "apply_param", "Apply",       show=True),
        Binding("ctrl+z",  "undo_param",  "Undo",        show=True),
        Binding("ctrl+e",  "export_yaml", "Export YAML", show=True),
        Binding("up",      "cursor_up",   "Up",          show=False),
        Binding("down",    "cursor_down", "Down",        show=False),
    ]

    DEFAULT_CSS = """
    ParamTunerPanel {
        height: 1fr;
        padding: 0 1;
    }
    #node_select { height: 3; dock: top; }
    #param_table { height: 1fr; }
    #edit_row    { height: 3; dock: bottom; padding: 0 1; background: $surface; border-top: solid $accent; }
    #edit_label  { width: 35; content-align: left middle; color: $text-muted; }
    #edit_input  { width: 1fr; }
    #hint_bar    { height: 1; dock: bottom; }
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
        self._suppress_select: bool = False  # guard against spurious select events

    def compose(self) -> ComposeResult:
        yield Select([], prompt="Select a node… (Tab to reach)", id="node_select")
        yield DataTable(id="param_table", zebra_stripes=True, cursor_type="row")
        with Horizontal(id="edit_row"):
            yield Label("No param selected", id="edit_label")
            yield Input(placeholder="new value…  then Enter to apply", id="edit_input")
        yield StatusBar(
            "  Enter: Apply   Ctrl+Z: Undo   Ctrl+E: Export YAML   ↑↓: Navigate params",
            id="hint_bar"
        )

    def on_mount(self) -> None:
        table = self.query_one("#param_table", DataTable)
        labels = ["Parameter", "Value", "Type"]
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
        sel = self.query_one("#node_select", Select)
        current = self._selected_node
        self._suppress_select = True
        try:
            sel.set_options([(n, n) for n in all_nodes])
            if current and current in all_nodes:
                sel.value = current
        finally:
            self._suppress_select = False

    # -----------------------------------------------------------------------
    # Param table
    # -----------------------------------------------------------------------

    def _refresh_params(self) -> None:
        if not self._selected_node:
            return
        params = self._store.snapshot_params(self._selected_node)
        table = self.query_one("#param_table", DataTable)
        existing_values = {rk.value for rk in table.rows.keys()}
        new_keys = {p.name for p in params}

        for rk in list(table.rows.keys()):
            if rk.value not in new_keys:
                table.remove_row(rk)

        for p in sorted(params, key=lambda x: x.name):
            val_text  = Text(str(p.value))
            type_text = Text(p.type_name, style="dim")
            if p.name in existing_values:
                table.update_cell(p.name, self._ck["Value"], val_text)
            else:
                table.add_row(
                    Text(p.name, style="cyan"), val_text, type_text,
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
        if self._bridge:
            self._bridge.fetch_params(self._selected_node)
        self._set_status(f"Fetching params for {self._selected_node}…")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not event.row_key or not self._selected_node:
            return
        params = {p.name: p for p in self._store.snapshot_params(self._selected_node)}
        self._selected_param = params.get(event.row_key.value)
        if self._selected_param:
            self.query_one("#edit_label", Label).update(
                f"[cyan]{self._selected_param.name}[/cyan] "
                f"[dim]({self._selected_param.type_name})[/dim]"
            )
            self.query_one("#edit_input", Input).value = str(self._selected_param.value)

    def on_button_pressed(self, event) -> None:
        pass  # No buttons — keyboard only

    # -----------------------------------------------------------------------
    # Operations
    # -----------------------------------------------------------------------

    def _apply_param(self) -> None:
        if not self._selected_param or not self._selected_node or not self._bridge:
            self._set_status("Select a node and parameter first.")
            return
        raw = self.query_one("#edit_input", Input).value.strip()
        if not raw:
            return
        new_val = _coerce_value(raw, self._selected_param.type_name)
        old_val = self._selected_param.value
        self._history.append((self._selected_node, self._selected_param.name, old_val, new_val))
        self._set_status(f"Setting {self._selected_param.name} = {new_val}…")

        def on_done(success: bool, error: str) -> None:
            msg = (f"✓ {self._selected_param.name} = {new_val}" if success
                   else f"✗ Failed: {error}")
            self.app.call_from_thread(self._set_status, msg)

        self._bridge.set_param(self._selected_node, self._selected_param.name,
                               new_val, on_done=on_done)

    def _undo_last(self) -> None:
        if not self._history or not self._bridge:
            self._set_status("Nothing to undo.")
            return
        node, param, old_val, _ = self._history.pop()
        self._set_status(f"Undoing {param} → {old_val}…")

        def on_done(success: bool, error: str) -> None:
            msg = f"✓ Undone: {param} = {old_val}" if success else f"✗ Undo failed: {error}"
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
        node_bare = self._selected_node.lstrip("/")
        data = {node_bare: {"ros__parameters": {p.name: p.value for p in params}}}
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"params_{node_bare}_{ts}.yaml"
        Path(filename).write_text(yaml.dump(data, default_flow_style=False))
        self._set_status(f"✓ Exported → {filename}")

    def _set_status(self, msg: str) -> None:
        self.query_one("#hint_bar", StatusBar).update(
            f"{msg}   [dim]Enter: Apply  Ctrl+Z: Undo  Ctrl+E: Export[/dim]"
        )
