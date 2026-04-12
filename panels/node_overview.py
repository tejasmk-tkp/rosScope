"""
panels/node_overview.py — Node Overview panel.
"""

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import DataTable
from rich.text import Text

from core.data_store import DataStore
from core.proc_utils import MemTrend

_SPARK_CHARS = " ▁▂▃▄▅▆▇█"

def _sparkline(values, width: int = 10, max_val: float = 100.0) -> str:
    if not values:
        return " " * width
    vals = list(values)[-width:]
    vals = [0.0] * (width - len(vals)) + vals
    top = max(max_val, max(vals) or 1.0)
    chars = []
    for v in vals:
        idx = int((v / top) * (len(_SPARK_CHARS) - 1))
        idx = max(0, min(idx, len(_SPARK_CHARS) - 1))
        chars.append(_SPARK_CHARS[idx])
    return "".join(chars)

def _trend_cell(trend: MemTrend) -> Text:
    if trend == MemTrend.GROWING:
        return Text("↑ GROWING", style="bold red")
    if trend == MemTrend.HIGH:
        return Text("● HIGH", style="yellow")
    return Text("✓ stable", style="dim green")

def _cpu_cell(pct: float) -> Text:
    if pct >= 80:
        return Text(f"{pct:5.1f}%", style="bold red")
    if pct >= 50:
        return Text(f"{pct:5.1f}%", style="yellow")
    return Text(f"{pct:5.1f}%", style="green")

def _mem_cell(mb: float) -> Text:
    if mb >= 512:
        return Text(f"{mb:7.1f} MB", style="yellow")
    return Text(f"{mb:7.1f} MB")


class NodeOverviewPanel(Widget):

    DEFAULT_CSS = """
    NodeOverviewPanel {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, store: DataStore, **kwargs):
        super().__init__(**kwargs)
        self._store = store
        self._ck: dict = {}   # label -> ColumnKey

    def compose(self) -> ComposeResult:
        yield DataTable(id="node_table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#node_table", DataTable)
        labels = ["Node", "PID", "CPU%", "CPU spark", "Memory", "Mem spark", "Trend"]
        keys = table.add_columns(*labels)
        self._ck = dict(zip(labels, keys))
        self.set_interval(1.0, self._refresh_table)

    def _refresh_table(self) -> None:
        nodes = self._store.snapshot_nodes()
        table = self.query_one("#node_table", DataTable)

        new_keys = {n.name for n in nodes}
        existing_values = {rk.value for rk in table.rows.keys()}

        # Remove stale rows
        for rk in list(table.rows.keys()):
            if rk.value not in new_keys:
                table.remove_row(rk)

        for node in nodes:
            cpu_spark = _sparkline(node.cpu_sparkline, width=12, max_val=100.0)
            mem_spark = _sparkline(node.mem_sparkline, width=12,
                                   max_val=max(512.0, node.memory_mb or 1.0))
            pid_str = str(node.pid) if node.pid else Text("?", style="dim")

            row = (
                Text(node.name, style="cyan"),
                pid_str,
                _cpu_cell(node.cpu_percent),
                Text(cpu_spark, style="blue"),
                _mem_cell(node.memory_mb),
                Text(mem_spark, style="magenta"),
                _trend_cell(node.mem_trend),
            )

            if node.name in existing_values:
                table.update_cell(node.name, self._ck["CPU%"],      row[2])
                table.update_cell(node.name, self._ck["CPU spark"], row[3])
                table.update_cell(node.name, self._ck["Memory"],    row[4])
                table.update_cell(node.name, self._ck["Mem spark"], row[5])
                table.update_cell(node.name, self._ck["Trend"],     row[6])
            else:
                table.add_row(*row, key=node.name)
