"""
panels/interactor.py — Topic Publisher & Service Caller (Tab 6).

Left pane:  Topic Publisher
  - Search/select topic → introspect msg type → show typed input fields
  - Publish button (or Enter)
  - Repeat publish with configurable Hz

Right pane: Service Caller
  - Search/select service → introspect srv type → show request fields
  - Call button → show response inline

Field rendering:
  bool   → [Toggle]  True/False button
  int    → numeric input
  float  → numeric input
  str    → text input
  nested → grouped under a header label
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Input, Button, Label, Select, Switch, OptionList
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.message import Message
from rich.text import Text

from core.data_store import DataStore


# ---------------------------------------------------------------------------
# Field widget — renders a single typed input for one message field
# ---------------------------------------------------------------------------

class FieldInput(Horizontal):
    """One row: label + appropriate input widget for a message field."""

    DEFAULT_CSS = """
    FieldInput {
        height: 3;
        padding: 0 1;
        margin-bottom: 0;
    }
    FieldInput Label {
        width: 28;
        content-align: left middle;
        color: $text-muted;
    }
    FieldInput Input  { width: 1fr; height: 3; }
    FieldInput Switch { margin: 1 0; }
    """

    def __init__(self, field: dict, **kwargs):
        super().__init__(**kwargs)
        self._field  = field   # {path, type, default}
        self._ftype  = field["type"]
        self._path   = field["path"]
        self._default = field["default"]

    def compose(self) -> ComposeResult:
        # Show last part of dot-path as label, full path as tooltip-style dim suffix
        parts = self._path.split(".")
        short = parts[-1]
        prefix = ".".join(parts[:-1])
        label_text = f"[dim]{prefix}.[/dim]{short}" if prefix else short
        yield Label(label_text, id=f"lbl_{self._safe_id()}")

        if self._ftype == "bool":
            yield Switch(value=bool(self._default),
                         id=f"sw_{self._safe_id()}")
        else:
            yield Input(
                value=str(self._default),
                placeholder=self._ftype,
                id=f"inp_{self._safe_id()}",
                type="number" if self._ftype in ("int", "float") else "text",
            )

    def _safe_id(self) -> str:
        return self._path.replace(".", "_").replace("/", "_")

    def get_value(self) -> Any:
        """Return current value as appropriate Python type."""
        try:
            if self._ftype == "bool":
                return self.query_one(f"#sw_{self._safe_id()}", Switch).value
            raw = self.query_one(f"#inp_{self._safe_id()}", Input).value.strip()
            if self._ftype == "int":
                return int(float(raw)) if raw else 0
            if self._ftype == "float":
                return float(raw) if raw else 0.0
            return raw
        except (NoMatches, ValueError):
            return self._default


# ---------------------------------------------------------------------------
# FieldForm — scrollable list of FieldInputs for a message/request
# ---------------------------------------------------------------------------

class FieldForm(ScrollableContainer):
    DEFAULT_CSS = """
    FieldForm {
        height: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
        border: solid $accent;
        background: $surface-darken-1;
    }
    """

    def __init__(self, fields: List[dict], **kwargs):
        super().__init__(**kwargs)
        self._fields = fields

    def compose(self) -> ComposeResult:
        if not self._fields:
            yield Static("  [dim]No fields — select a topic/service above[/dim]")
            return
        # Group by top-level prefix for visual separation
        current_group = None
        for field in self._fields:
            top = field["path"].split(".")[0]
            if top != current_group and "." in field["path"]:
                current_group = top
                yield Static(f" [dim]── {top} ──[/dim]")
            safe = field["path"].replace(".", "_").replace("/", "_")
            yield FieldInput(field, id=f"fi_{safe}")

    def collect_values(self) -> Dict[str, Any]:
        result = {}
        for fi in self.query(FieldInput):
            result[fi._path] = fi.get_value()
        return result


# ---------------------------------------------------------------------------
# SearchBar — reusable dropdown search for topics/services
# ---------------------------------------------------------------------------

class SearchBar(Widget):
    """Simple single-stage dropdown search. Emits SearchBar.Chosen(value)."""

    DEFAULT_CSS = """
    SearchBar { height: 3; width: 1fr; }
    #sb_input { height: 3; width: 1fr; }
    #sb_dropdown {
        display: none;
        height: auto;
        max-height: 10;
        width: 50;
        border: tall $accent;
        background: $surface;
    }
    """

    class Chosen(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, placeholder: str = "search…", **kwargs):
        super().__init__(**kwargs)
        self._placeholder = placeholder
        self._options: List[str] = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder=self._placeholder, id="sb_input")

    def on_mount(self) -> None:
        from textual.widgets import OptionList
        from textual.widgets._option_list import Option

        class _DD(Widget):
            DEFAULT_CSS = """
            _DD {
                display: none;
                height: auto;
                max-height: 10;
                width: 50;
                border: tall $accent;
                background: $surface;
            }
            """
        # Use app-level OptionList like TopicSearchBar
        from textual.widgets import OptionList as OL
        self._dd = OL(id=f"sb_dd_{id(self)}")
        self._dd.display = False
        self._dd.styles.width = 50
        self._dd.styles.max_height = 10
        self.app.mount(self._dd)

    def set_options(self, options: List[str]) -> None:
        self._options = options

    def _reposition(self) -> None:
        try:
            inp = self.query_one("#sb_input", Input)
            off = inp.screen_offset
            self._dd.styles.offset = (off.x, off.y + 3)
            self._dd.styles.width = max(inp.size.width, 40)
        except Exception:
            pass

    def _show(self, query: str) -> None:
        from textual.widgets._option_list import Option
        matches = [o for o in self._options if query.lower() in o.lower()] if query else self._options
        self._dd.clear_options()
        if matches:
            self._dd.add_options([Option(o, id=o.replace("/", "__").replace(".", "_")) for o in matches])
            self._reposition()
            self._dd.display = True
        else:
            self._dd.display = False

    def _hide(self) -> None:
        self._dd.display = False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "sb_input":
            self._show(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "sb_input" and event.value.strip():
            self._hide()
            self.post_message(self.Chosen(event.value.strip()))
            event.input.value = ""

    def on_key(self, event) -> None:
        if not self._dd.display:
            return
        if event.key == "escape":
            self._hide()
            event.stop()
        elif event.key == "down":
            self._dd.focus()
            event.stop()

    def on_option_list_option_selected(self, event) -> None:
        # This bubbles up from the app-level dropdown
        if event.option_list is self._dd:
            self._hide()
            try:
                self.query_one("#sb_input", Input).value = ""
            except NoMatches:
                pass
            self.post_message(self.Chosen(str(event.option.prompt)))
            event.stop()


# ---------------------------------------------------------------------------
# PublisherPane
# ---------------------------------------------------------------------------

class PublisherPane(Vertical):
    DEFAULT_CSS = """
    PublisherPane {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
    }
    #pub_header {
        height: 1;
        background: $surface-darken-2;
        padding: 0 1;
        color: cyan;
        margin-bottom: 1;
    }
    #pub_controls {
        height: 3;
        margin-bottom: 1;
    }
    #pub_status {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #pub_btn    { width: 14; }
    #pub_repeat { width: 16; margin-left: 1; }
    #pub_hz_inp { width: 8;  margin-left: 1; }
    """

    def __init__(self, store: DataStore, bridge, **kwargs):
        super().__init__(**kwargs)
        self._store   = store
        self._bridge  = bridge
        self._topic   = ""
        self._msgtype = ""
        self._repeating = False
        self._hz = 1.0

    def compose(self) -> ComposeResult:
        yield Static(" ▶ Topic Publisher", id="pub_header")
        yield SearchBar(placeholder="search topic to publish on…", id="pub_search")
        yield Static("  [dim]Select a topic above to load fields[/dim]", id="pub_topic_label")
        with Horizontal(id="pub_controls"):
            yield Button("Publish", id="pub_btn", variant="primary")
            yield Button("Repeat OFF", id="pub_repeat", variant="default")
            yield Input("1", id="pub_hz_inp", placeholder="Hz",
                        type="number")
            yield Label("Hz")
        yield FieldForm([], id="pub_form")
        yield Static("", id="pub_status")

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh_topics)

    def _refresh_topics(self) -> None:
        topics = [t.name for t in self._store.snapshot_topics()]
        try:
            self.query_one("#pub_search", SearchBar).set_options(topics)
        except NoMatches:
            pass

    def on_search_bar_chosen(self, event: SearchBar.Chosen) -> None:
        if self.query_one("#pub_search", SearchBar) in self.query(SearchBar):
            self._load_topic(event.value)
            event.stop()

    def _load_topic(self, topic: str) -> None:
        self._topic = topic
        # Find message type
        topics = {t.name: t for t in self._store.snapshot_topics()}
        snap = topics.get(topic)
        if snap is None:
            self._set_status(f"Topic {topic} not found in topic list")
            return
        self._msgtype = snap.msg_type
        self.query_one("#pub_topic_label", Static).update(
            f"  [cyan]{topic}[/cyan]  [dim]{self._msgtype}[/dim]"
        )
        # Introspect fields
        if self._bridge:
            fields = self._bridge.get_msg_fields(self._msgtype)
        else:
            fields = _demo_fields_for(self._msgtype)
        # Replace form
        form = self.query_one("#pub_form", FieldForm)
        form.remove()
        new_form = FieldForm(fields, id="pub_form")
        self.mount(new_form, before=self.query_one("#pub_status", Static))
        self._set_status(f"Loaded {len(fields)} fields")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pub_btn":
            self._do_publish()
        elif event.button.id == "pub_repeat":
            self._toggle_repeat()

    def _do_publish(self) -> None:
        if not self._topic or not self._msgtype:
            self._set_status("Select a topic first")
            return
        try:
            values = self.query_one("#pub_form", FieldForm).collect_values()
        except NoMatches:
            values = {}
        if self._bridge:
            def cb(ok, err):
                msg = "✓ Published" if ok else f"✗ {err}"
                self.app.call_from_thread(self._set_status, msg)
            self._bridge.publish_topic(self._topic, self._msgtype, values, on_done=cb)
        else:
            self._set_status(f"[dim]demo: publish {self._topic} {values}[/dim]")

    def _toggle_repeat(self) -> None:
        self._repeating = not self._repeating
        btn = self.query_one("#pub_repeat", Button)
        if self._repeating:
            try:
                self._hz = float(self.query_one("#pub_hz_inp", Input).value or "1")
            except ValueError:
                self._hz = 1.0
            btn.label = f"Repeat {self._hz:.1f}Hz"
            btn.variant = "warning"
            self._repeat_timer = self.set_interval(1.0 / max(self._hz, 0.1),
                                                    self._do_publish)
        else:
            btn.label = "Repeat OFF"
            btn.variant = "default"
            if hasattr(self, "_repeat_timer"):
                self._repeat_timer.stop()

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#pub_status", Static).update(f"  {msg}")
        except NoMatches:
            pass


# ---------------------------------------------------------------------------
# ServicePane
# ---------------------------------------------------------------------------

class ServicePane(Vertical):
    DEFAULT_CSS = """
    ServicePane {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        border-left: solid $accent;
    }
    #srv_header {
        height: 1;
        background: $surface-darken-2;
        padding: 0 1;
        color: yellow;
        margin-bottom: 1;
    }
    #srv_controls { height: 3; margin-bottom: 1; }
    #srv_btn      { width: 14; }
    #srv_status   { height: 1; padding: 0 1; color: $text-muted; }
    #srv_response {
        height: 6;
        border: solid $accent;
        padding: 0 1;
        overflow-y: auto;
        background: $surface-darken-2;
        color: $text-muted;
    }
    """

    def __init__(self, store: DataStore, bridge, **kwargs):
        super().__init__(**kwargs)
        self._store   = store
        self._bridge  = bridge
        self._service = ""
        self._srvtype = ""

    def compose(self) -> ComposeResult:
        yield Static(" ⚡ Service Caller", id="srv_header")
        yield SearchBar(placeholder="search service to call…", id="srv_search")
        yield Static("  [dim]Select a service above to load request fields[/dim]",
                     id="srv_service_label")
        with Horizontal(id="srv_controls"):
            yield Button("Call", id="srv_btn", variant="warning")
        yield FieldForm([], id="srv_form")
        yield Static("", id="srv_status")
        yield Static("[dim]  Response will appear here[/dim]", id="srv_response")

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh_services)

    def _refresh_services(self) -> None:
        services = [s.name for s in self._store.snapshot_services()]
        try:
            self.query_one("#srv_search", SearchBar).set_options(services)
        except NoMatches:
            pass

    def on_search_bar_chosen(self, event: SearchBar.Chosen) -> None:
        if self.query_one("#srv_search", SearchBar) in self.query(SearchBar):
            self._load_service(event.value)
            event.stop()

    def _load_service(self, service: str) -> None:
        self._service = service
        services = {s.name: s for s in self._store.snapshot_services()}
        snap = services.get(service)
        if snap is None:
            self._set_status(f"Service {service} not in list")
            return
        self._srvtype = snap.srv_type
        self.query_one("#srv_service_label", Static).update(
            f"  [yellow]{service}[/yellow]  [dim]{self._srvtype}[/dim]"
        )
        if self._bridge:
            fields = self._bridge.get_srv_fields(self._srvtype)
        else:
            fields = _demo_srv_fields_for(self._srvtype)
        form = self.query_one("#srv_form", FieldForm)
        form.remove()
        new_form = FieldForm(fields, id="srv_form")
        self.mount(new_form, before=self.query_one("#srv_status", Static))
        self._set_status(f"Loaded {len(fields)} request fields")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "srv_btn":
            self._do_call()

    def _do_call(self) -> None:
        if not self._service or not self._srvtype:
            self._set_status("Select a service first")
            return
        try:
            values = self.query_one("#srv_form", FieldForm).collect_values()
        except NoMatches:
            values = {}
        self._set_status("Calling…")
        self.query_one("#srv_response", Static).update("[dim]waiting…[/dim]")
        if self._bridge:
            def cb(ok, err, resp):
                if ok:
                    self.app.call_from_thread(self._set_status, "✓ Success")
                    self.app.call_from_thread(
                        self.query_one("#srv_response", Static).update,
                        Text(str(resp) or "(empty response)", style="green")
                    )
                else:
                    self.app.call_from_thread(self._set_status, f"✗ {err}")
                    self.app.call_from_thread(
                        self.query_one("#srv_response", Static).update,
                        Text(f"Error: {err}", style="red")
                    )
            self._bridge.call_service(self._service, self._srvtype, values, on_done=cb)
        else:
            self._set_status("[dim]demo mode — no ROS[/dim]")
            self.query_one("#srv_response", Static).update(
                "[dim]success: True\nmessage: 'demo response'[/dim]"
            )

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#srv_status", Static).update(f"  {msg}")
        except NoMatches:
            pass


# ---------------------------------------------------------------------------
# Demo field helpers (--no-ros mode)
# ---------------------------------------------------------------------------

def _demo_fields_for(msg_type: str) -> List[dict]:
    _known = {
        "geometry_msgs/msg/Twist": [
            {"path": "linear.x",  "type": "float", "default": 0.0},
            {"path": "linear.y",  "type": "float", "default": 0.0},
            {"path": "linear.z",  "type": "float", "default": 0.0},
            {"path": "angular.x", "type": "float", "default": 0.0},
            {"path": "angular.y", "type": "float", "default": 0.0},
            {"path": "angular.z", "type": "float", "default": 0.0},
        ],
        "std_msgs/msg/Bool": [
            {"path": "data", "type": "bool", "default": False},
        ],
        "std_msgs/msg/Float64": [
            {"path": "data", "type": "float", "default": 0.0},
        ],
        "std_msgs/msg/String": [
            {"path": "data", "type": "str", "default": ""},
        ],
        "std_msgs/msg/Int32": [
            {"path": "data", "type": "int", "default": 0},
        ],
    }
    return _known.get(msg_type, [{"path": "data", "type": "str", "default": ""}])


def _demo_srv_fields_for(srv_type: str) -> List[dict]:
    _known = {
        "std_srvs/srv/SetBool": [
            {"path": "data", "type": "bool", "default": False},
        ],
        "std_srvs/srv/Trigger": [],
        "rcl_interfaces/srv/SetParameters": [
            {"path": "parameters", "type": "str", "default": "[]"},
        ],
    }
    return _known.get(srv_type, [])


# ---------------------------------------------------------------------------
# InteractorPanel — Tab 6
# ---------------------------------------------------------------------------

class InteractorPanel(Widget):

    BINDINGS = [
        Binding("ctrl+b", "focus_publisher",  "Publisher",  show=True),
        Binding("ctrl+k", "focus_service",    "Service",    show=True),
    ]

    DEFAULT_CSS = """
    InteractorPanel {
        height: 1fr;
        layout: horizontal;
    }
    """

    def __init__(self, store: DataStore, bridge, **kwargs):
        super().__init__(**kwargs)
        self._store  = store
        self._bridge = bridge

    def compose(self) -> ComposeResult:
        yield PublisherPane(self._store, self._bridge, id="pub_pane")
        yield ServicePane(self._store, self._bridge,  id="srv_pane")

    def action_focus_publisher(self) -> None:
        try:
            self.query_one("#pub_search").query_one("#sb_input", Input).focus()
        except NoMatches:
            pass

    def action_focus_service(self) -> None:
        try:
            self.query_one("#srv_search").query_one("#sb_input", Input).focus()
        except NoMatches:
            pass
