"""
panels/topic_monitor.py — Topic Monitor panel.
"""

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import DataTable, Static, Input
from textual.containers import Vertical
from rich.text import Text

from core.data_store import DataStore


def _freq_cell(hz: float) -> Text:
    if hz == 0:
        return Text("  0.0 Hz", style="dim")
    if hz < 1:
        return Text(f"{hz:5.1f} Hz", style="yellow")
    return Text(f"{hz:5.1f} Hz", style="green")


def _qos_cell(reliability: str, mismatch: bool) -> Text:
    label = reliability.upper()[:4]
    if mismatch:
        return Text(f"{label} ⚠", style="bold red")
    return Text(label, style="dim")


class LastMsgView(Static):
    DEFAULT_CSS = """
    LastMsgView {
        height: 8;
        border: solid $accent;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    def set_content(self, topic: str, msg_repr: str) -> None:
        if not msg_repr:
            self.update(f"[dim]No messages received on {topic}[/dim]")
        else:
            self.update(
                f"[bold cyan]{topic}[/bold cyan]\n[dim]{msg_repr}[/dim]")


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
        yield DataTable(id="topic_table",
                        zebra_stripes=True,
                        cursor_type="row")
        yield LastMsgView(id="last_msg")

    def on_mount(self) -> None:
        table = self.query_one("#topic_table", DataTable)
        labels = ["Topic", "Type", "Hz", "Pubs", "Subs", "QoS", "Durability"]
        keys = table.add_columns(*labels)
        self._ck = dict(zip(labels, keys))
        self.set_interval(1.0, self._refresh_table)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._filter = event.value.lower()
        self._refresh_table()

    def on_data_table_row_highlighted(self,
                                      event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            topic = event.row_key.value
            topics = {t.name: t for t in self._store.snapshot_topics()}
            t = topics.get(topic)
            if t:
                self.query_one("#last_msg", LastMsgView).set_content(
                    topic, t.last_msg_repr)

    def _refresh_table(self) -> None:
        topics = self._store.snapshot_topics()
        if self._filter:
            topics = [
                t for t in topics if self._filter in t.name.lower()
                or self._filter in t.msg_type.lower()
            ]

        table = self.query_one("#topic_table", DataTable)
        new_keys = {t.name for t in topics}
        existing_values = {rk.value for rk in table.rows.keys()}

        for rk in list(table.rows.keys()):
            if rk.value not in new_keys:
                table.remove_row(rk)

        for t in topics:
            short_type = t.msg_type.split(
                "/")[-1] if "/" in t.msg_type else t.msg_type
            row = (
                Text(t.name,
                     style="cyan" if not t.qos_mismatch else "bold red"),
                Text(short_type, style="dim"),
                _freq_cell(t.frequency_hz),
                Text(str(t.pub_count), style="green"),
                Text(str(t.sub_count), style="blue"),
                _qos_cell(t.qos_reliability, t.qos_mismatch),
                Text(t.qos_durability[:4].upper(), style="dim"),
            )

            if t.name in existing_values:
                table.update_cell(t.name, self._ck["Hz"], row[2])
                table.update_cell(t.name, self._ck["Pubs"], row[3])
                table.update_cell(t.name, self._ck["Subs"], row[4])
                table.update_cell(t.name, self._ck["QoS"], row[5])
            else:
                table.add_row(*row, key=t.name)
