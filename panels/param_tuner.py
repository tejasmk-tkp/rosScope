"""
panels/param_tuner.py — Parameter Tuner panel.

Select a node → see all params → edit inline → apply via ros2 param set.
Change history with undo. Export to YAML.
"""

import time
import yaml
from pathlib import Path
from typing import Any, List, Optional

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import DataTable, Select, Input, Label, Static, Button
from textual.containers import Vertical, Horizontal, ScrollableContainer
from textual.reactive import reactive
from textual import work
from rich.text import Text

from core.data_store import DataStore, ParamSnapshot


def _coerce_value(raw: str, type_name: str) -> Any:
    """Parse string input back to the param's native type."""
    try:
        if type_name == "bool":
            return raw.lower() in ("true", "1", "yes")
        if type_name == "int":
            return int(raw)
        if type_name in ("float", "double"):
            return float(raw)
        return raw  # string
    except ValueError:
        return raw


class ParamEditBar(Horizontal):
    """Input bar shown when a param row is selected."""

    DEFAULT_CSS = """
    ParamEditBar {
        height: 3;
        dock: bottom;
        padding: 0 1;
        background: $surface;
        border-top: solid $accent;
    }
    #edit_label { width: 30; content-align: left middle; }
    #edit_input { width: 1fr; }
    #edit_apply { width: 10; }
    #edit_undo  { width: 10; }
    #edit_export { width: 12; }
    """

    def compose(self) -> ComposeResult:
        yield Label("", id="edit_label")
        yield Input(placeholder="new value…", id="edit_input")
        yield Button("Apply", id="edit_apply", variant="primary")
        yield Button("Undo",  id="edit_undo",  variant="default")
        yield Button("Export YAML", id="edit_export", variant="success")


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        dock: bottom;
        padding: 0 1;
        background: $surface-darken-1;
    }
    """


class ParamTunerPanel(Widget):

    DEFAULT_CSS = """
    ParamTunerPanel {
        height: 1fr;
        padding: 0 1;
    }
    #node_select {
        height: 3;
        dock: top;
    }
    #param_table {
        height: 1fr;
    }
    """

    def __init__(self, store, bridge, **kwargs):
        super().__init__(**kwargs)
        self._store = store
        self._bridge = bridge
        self._selected_node: Optional[str] = None
        self._selected_param: Optional[ParamSnapshot] = None
        # Change history: list of (node, param, old_val, new_val)
        self._history: List[tuple] = []

    def compose(self) -> ComposeResult:
        yield Select([], prompt="Select a node…", id="node_select")
        yield DataTable(id="param_table", zebra_stripes=True, cursor_type="row")
        yield ParamEditBar(id="edit_bar")
        yield StatusBar("Ready.", id="status_bar")

    def on_mount(self) -> None:
        table = self.query_one("#param_table", DataTable)
        table.add_columns("Parameter", "Value", "Type")
        self.set_interval(2.0, self._refresh_node_list)
        self.set_interval(1.0, self._refresh_params)

    # -----------------------------------------------------------------------
    # Node list refresh
    # -----------------------------------------------------------------------

    def _refresh_node_list(self) -> None:
        nodes = self._store.snapshot_param_nodes()
        # Also pull from the node resources (nodes that haven't had params fetched yet)
        live_nodes = [n.name for n in self._store.snapshot_nodes()]
        all_nodes = sorted(set(nodes + live_nodes))

        sel = self.query_one("#node_select", Select)
        options = [(n, n) for n in all_nodes]
        sel.set_options(options)

    # -----------------------------------------------------------------------
    # Param table refresh
    # -----------------------------------------------------------------------

    def _refresh_params(self) -> None:
        if not self._selected_node:
            return
        params = self._store.snapshot_params(self._selected_node)
        table = self.query_one("#param_table", DataTable)

        new_keys = {p.name for p in params}
        existing_keys = set(table.rows.keys())

        for key in existing_keys - new_keys:
            table.remove_row(key)

        for p in sorted(params, key=lambda x: x.name):
            val_text = Text(str(p.value))
            type_text = Text(p.type_name, style="dim")

            if p.name in existing_keys:
                table.update_cell(p.name, "Value", val_text)
            else:
                table.add_row(
                    Text(p.name, style="cyan"),
                    val_text,
                    type_text,
                    key=p.name,
                )

    # -----------------------------------------------------------------------
    # Events
    # -----------------------------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value and event.value != Select.BLANK:
            self._selected_node = event.value
            # Request param fetch from bridge
            if self._bridge:
                self._bridge.fetch_params(self._selected_node)
            self._set_status(f"Fetching params for {self._selected_node}…")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not event.row_key or not self._selected_node:
            return
        param_name = event.row_key.value
        params = {p.name: p for p in self._store.snapshot_params(self._selected_node)}
        self._selected_param = params.get(param_name)

        if self._selected_param:
            label = self.query_one("#edit_label", Label)
            label.update(f"{param_name} ({self._selected_param.type_name})")
            inp = self.query_one("#edit_input", Input)
            inp.value = str(self._selected_param.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "edit_apply":
            self._apply_param()
        elif event.button.id == "edit_undo":
            self._undo_last()
        elif event.button.id == "edit_export":
            self._export_yaml()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "edit_input":
            self._apply_param()

    # -----------------------------------------------------------------------
    # Param set / undo / export
    # -----------------------------------------------------------------------

    def _apply_param(self) -> None:
        if not self._selected_param or not self._selected_node or not self._bridge:
            return

        raw = self.query_one("#edit_input", Input).value.strip()
        if not raw:
            return

        new_val = _coerce_value(raw, self._selected_param.type_name)
        old_val = self._selected_param.value

        self._history.append((
            self._selected_node,
            self._selected_param.name,
            old_val,
            new_val,
        ))

        self._set_status(f"Setting {self._selected_param.name} = {new_val}…")

        def on_done(success: bool, error: str) -> None:
            if success:
                self.app.call_from_thread(
                    self._set_status,
                    f"✓ {self._selected_param.name} = {new_val}"
                )
            else:
                self.app.call_from_thread(
                    self._set_status,
                    f"✗ Failed: {error}"
                )

        self._bridge.set_param(
            self._selected_node,
            self._selected_param.name,
            new_val,
            on_done=on_done,
        )

    def _undo_last(self) -> None:
        if not self._history or not self._bridge:
            self._set_status("Nothing to undo.")
            return

        node, param, old_val, new_val = self._history.pop()
        self._set_status(f"Undoing {param}: {new_val} → {old_val}…")

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

        # ROS2 standard param file format
        node_bare = self._selected_node.lstrip("/")
        data = {
            node_bare: {
                "ros__parameters": {p.name: p.value for p in params}
            }
        }

        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"params_{node_bare}_{ts}.yaml"
        path = Path(filename)
        path.write_text(yaml.dump(data, default_flow_style=False))
        self._set_status(f"✓ Exported → {filename}")

    def _set_status(self, msg: str) -> None:
        self.query_one("#status_bar", StatusBar).update(msg)
