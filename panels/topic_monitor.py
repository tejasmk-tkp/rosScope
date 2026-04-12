"""
panels/topic_monitor.py — Topic Monitor panel.
QoS mismatch now shows which publisher and subscriber profiles conflict.
"""

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import DataTable, Static, Input
from rich.text import Text

from core.data_store import DataStore


def _freq_cell(hz: float) -> Text:
    if hz == 0:   return Text("  0.0 Hz", style="dim")
    if hz < 1:    return Text(f"{hz:5.1f} Hz", style="yellow")
    return             Text(f"{hz:5.1f} Hz", style="green")

def _qos_cell(reliability: str, mismatch: bool) -> Text:
    label = reliability.upper()[:4]
    return Text(f"{label} ⚠ MISMATCH", style="bold red") if mismatch else Text(label, style="dim")


class MismatchDetail(Static):
    """Shows full QoS mismatch detail for the selected topic."""
    DEFAULT_CSS = """
    MismatchDetail {
        height: 5;
        border: solid $accent;
        padding: 0 1;
        overflow-y: auto;
    }
    """
    def set_topic(self, t) -> None:
        lines = Text()
        lines.append(f"{t.name}\n", style="bold cyan")
        lines.append(f"  Publisher  reliability={t.qos_reliability.upper():<12} durability={t.qos_durability.upper()}\n")

        if t.qos_mismatch:
            lines.append("  ⚠ QoS MISMATCH  ", style="bold red")
            lines.append("Publisher is BEST_EFFORT but a subscriber expects RELIABLE.\n",
                         style="yellow")
            lines.append("  Effect: subscriber silently receives NO messages.\n",
                         style="dim red")
            lines.append("  Fix: align QoS profiles in publisher or subscriber node.\n",
                         style="dim")
        else:
            lines.append("  QoS: compatible ✓\n", style="dim green")

        lines.append(f"  Last msg: {t.last_msg_repr or '(none yet)'}", style="dim")
        self.update(lines)


class TopicMonitorPanel(Widget):

    DEFAULT_CSS = """
    TopicMonitorPanel {
        height: 1fr;
        padding: 0 1;
    }
    #topic_filter { height: 3; dock: top; }
    #topic_table  { height: 1fr; }
    """

    def __init__(self, store: DataStore, **kwargs):
        super().__init__(**kwargs)
        self._store = store
        self._filter = ""
        self._ck: dict = {}

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Filter topics…", id="topic_filter")
        yield DataTable(id="topic_table", zebra_stripes=True, cursor_type="row")
        yield MismatchDetail(id="mismatch_detail")

    def on_mount(self) -> None:
        table = self.query_one("#topic_table", DataTable)
        labels = ["Topic", "Type", "Hz", "Pubs", "Subs", "QoS", "Durability"]
        keys = table.add_columns(*labels)
        self._ck = dict(zip(labels, keys))
        self.set_interval(1.0, self._refresh_table)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._filter = event.value.lower()
        self._refresh_table()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not event.row_key or not event.row_key.value:
            return
        topics = {t.name: t for t in self._store.snapshot_topics()}
        t = topics.get(event.row_key.value)
        if t:
            self.query_one("#mismatch_detail", MismatchDetail).set_topic(t)

    def _refresh_table(self) -> None:
        topics = self._store.snapshot_topics()
        if self._filter:
            topics = [t for t in topics
                      if self._filter in t.name.lower() or self._filter in t.msg_type.lower()]

        table = self.query_one("#topic_table", DataTable)
        existing_values = {rk.value for rk in table.rows.keys()}
        new_keys = {t.name for t in topics}

        for rk in list(table.rows.keys()):
            if rk.value not in new_keys:
                table.remove_row(rk)

        for t in topics:
            short_type = t.msg_type.split("/")[-1] if "/" in t.msg_type else t.msg_type
            row = (
                Text(t.name, style="cyan" if not t.qos_mismatch else "bold red"),
                Text(short_type, style="dim"),
                _freq_cell(t.frequency_hz),
                Text(str(t.pub_count), style="green"),
                Text(str(t.sub_count), style="blue"),
                _qos_cell(t.qos_reliability, t.qos_mismatch),
                Text(t.qos_durability[:4].upper(), style="dim"),
            )
            if t.name in existing_values:
                table.update_cell(t.name, self._ck["Hz"],    row[2])
                table.update_cell(t.name, self._ck["Pubs"],  row[3])
                table.update_cell(t.name, self._ck["Subs"],  row[4])
                table.update_cell(t.name, self._ck["QoS"],   row[5])
            else:
                table.add_row(*row, key=t.name)
