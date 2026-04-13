"""
data_store.py — Thread-safe shared state between the ROS bridge and the TUI.

Architecture:
    ROS bridge thread  →  writes via DataStore.update_*()
    Textual TUI thread →  reads via DataStore.snapshot_*()

All public methods are lock-protected. The TUI never touches rclpy directly.
Snapshots return plain dataclasses/dicts — no shared mutable objects.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .proc_utils import NodeResources, SystemResources, MemTrend

# ---------------------------------------------------------------------------
# Snapshot types (immutable views handed to the TUI)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeSnapshot:
    name: str
    pid: Optional[int]
    cpu_percent: float
    memory_mb: float
    mem_trend: MemTrend
    cpu_sparkline: Tuple[float, ...]  # last N samples for rendering
    mem_sparkline: Tuple[float, ...]


@dataclass(frozen=True)
class TopicSnapshot:
    name: str
    msg_type: str
    pub_count: int
    sub_count: int
    frequency_hz: float
    qos_reliability: str
    qos_durability: str
    qos_mismatch: bool  # True if pub/sub QoS are incompatible
    last_msg_repr: str  # str of last message (truncated)


@dataclass(frozen=True)
class ServiceSnapshot:
    name: str
    srv_type: str


@dataclass(frozen=True)
class ParamSnapshot:
    node: str
    name: str
    value: Any
    type_name: str  # 'bool', 'int', 'double', 'string', 'list'


@dataclass(frozen=True)
class PlotPoint:
    timestamp: float  # monotonic
    value: float


@dataclass(frozen=True)
class ParamChangeMarker:
    timestamp: float
    node: str
    param: str
    old_value: Any
    new_value: Any


@dataclass(frozen=True)
class TFTransform:
    parent: str
    child: str
    stamp_age_s: float  # age = ros_now - header.stamp (s); -1 if unknown/static
    last_received: float  # time.monotonic() of last received msg
    is_static: bool  # from /tf_static
    publisher_node: str  # best-effort, may be empty


@dataclass(frozen=True)
class SystemSnapshot:
    cpu_percent: float
    memory_used_mb: float
    memory_total_mb: float
    memory_percent: float
    ros_node_count: int
    timestamp: float


# ---------------------------------------------------------------------------
# Plot series state (mutable, lock-protected inside DataStore)
# ---------------------------------------------------------------------------


@dataclass
class _PlotSeries:
    topic: str
    points: deque = field(default_factory=lambda: deque(maxlen=600))  # 10min @1Hz
    unit: str = ""


# ---------------------------------------------------------------------------
# DataStore
# ---------------------------------------------------------------------------


class DataStore:
    """
    Central shared state. All mutation goes through update_* methods.
    All reads go through snapshot_* methods.
    Both sets are fully lock-protected.
    """

    def __init__(self, plot_window_seconds: int = 30):
        self._lock = (
            threading.RLock()
        )  # Reentrant so update methods can call each other
        self._plot_window = plot_window_seconds

        # Node resources (from proc_utils.ResourceMonitor)
        self._node_resources: Dict[str, NodeResources] = {}
        self._system_resources: Optional[SystemResources] = None

        # Topics
        self._topics: Dict[str, TopicSnapshot] = {}

        # Known numeric fields per topic: {topic_name: [field_path, ...]}
        # Populated by the ROS bridge when a message is first received.
        self._topic_fields: Dict[str, List[str]] = {}

        # Services
        self._services: Dict[str, ServiceSnapshot] = {}

        # Node resource history for plot panel
        self._node_plot_history: Dict[str, Any] = {}

        # Pinned nodes for CPU/Memory plot (mirrors _plot_series for topics)
        self._pinned_nodes: set = set()

        # Parameters: {node_name: {param_name: ParamSnapshot}}
        self._params: Dict[str, Dict[str, ParamSnapshot]] = {}
        self._param_change_history: deque = deque(maxlen=200)

        # Plot series: {topic_name: _PlotSeries}
        self._plot_series: Dict[str, _PlotSeries] = {}

        # TF transforms: {(parent, child): TFTransform}
        self._tf_transforms: Dict[Tuple[str, str], TFTransform] = {}
        self._tf_static: Dict[Tuple[str, str], TFTransform] = {}

        # Log lines from /rosout — 2000 entries before oldest messages are evicted
        self._log_lines: deque = deque(maxlen=2000)

        # Connection state
        self._ros_connected: bool = False
        self._last_update: float = 0.0

    # -----------------------------------------------------------------------
    # Write API (called from ROS bridge thread)
    # -----------------------------------------------------------------------

    def update_node_resources(
        self, resources: Dict[str, NodeResources], system: SystemResources
    ) -> None:
        with self._lock:
            self._node_resources = dict(resources)
            self._system_resources = system
            self._last_update = time.monotonic()

    def update_topics(self, topics: Dict[str, TopicSnapshot]) -> None:
        with self._lock:
            self._topics = dict(topics)

    def update_topic_fields(self, topic: str, fields: List[str]) -> None:
        """Record the list of plottable (numeric) field paths for a topic.

        Called by the ROS bridge the first time a message arrives on *topic*.
        ``fields`` is an ordered list of dot-separated paths, e.g.
        ``['position.x', 'position.y', 'velocity']``.
        """
        with self._lock:
            self._topic_fields[topic] = list(fields)

    def update_services(self, services: Dict[str, "ServiceSnapshot"]) -> None:
        with self._lock:
            self._services = dict(services)

    def append_node_resource_point(
        self, node: str, timestamp: float, cpu: float, mem_mb: float
    ) -> None:
        from collections import deque as _deque

        with self._lock:
            if node not in self._node_plot_history:
                self._node_plot_history[node] = _deque(maxlen=600)
            self._node_plot_history[node].append((timestamp, cpu, mem_mb))

    def pin_node(self, node: str) -> None:
        with self._lock:
            self._pinned_nodes.add(node)

    def unpin_node(self, node: str) -> None:
        with self._lock:
            self._pinned_nodes.discard(node)

    def snapshot_pinned_nodes(self) -> List[str]:
        with self._lock:
            return list(self._pinned_nodes)

    def update_params(self, node: str, params: Dict[str, "ParamSnapshot"]) -> None:
        with self._lock:
            self._params[node] = dict(params)

    def record_param_change(self, marker: ParamChangeMarker) -> None:
        with self._lock:
            self._param_change_history.append(marker)

    def append_plot_point(self, topic: str, timestamp: float, value: float) -> None:
        with self._lock:
            if topic not in self._plot_series:
                self._plot_series[topic] = _PlotSeries(topic=topic)
            self._plot_series[topic].points.append(
                PlotPoint(timestamp=timestamp, value=value)
            )

    def append_log_line(self, line: str, level: str = "INFO") -> None:
        with self._lock:
            self._log_lines.append((time.monotonic(), level, line))

    def set_ros_connected(self, connected: bool) -> None:
        with self._lock:
            self._ros_connected = connected

    def add_plot_topic(self, topic: str, field: str = "data") -> None:
        """Pin a topic+field combination to the plot panel.

        The key stored internally is ``"topic::field"`` so that the same topic
        can be pinned multiple times with different fields.
        """
        key = f"{topic}::{field}"
        with self._lock:
            if key not in self._plot_series:
                self._plot_series[key] = _PlotSeries(topic=key)

    def remove_plot_topic(self, topic: str, field: Optional[str] = None) -> None:
        """Unpin a topic (and optional field) from the plot panel."""
        key = f"{topic}::{field}" if field is not None else topic
        with self._lock:
            self._plot_series.pop(key, None)

    # -----------------------------------------------------------------------
    # Read API (called from TUI thread — returns copies, never live objects)
    # -----------------------------------------------------------------------

    def snapshot_system(self) -> Optional[SystemSnapshot]:
        with self._lock:
            if self._system_resources is None:
                return None
            s = self._system_resources
            return SystemSnapshot(
                cpu_percent=s.cpu_percent,
                memory_used_mb=s.memory_used_mb,
                memory_total_mb=s.memory_total_mb,
                memory_percent=s.memory_percent,
                ros_node_count=len(self._node_resources),
                timestamp=self._last_update,
            )

    def snapshot_nodes(self) -> List[NodeSnapshot]:
        with self._lock:
            snapshots = []
            for name, res in self._node_resources.items():
                snapshots.append(
                    NodeSnapshot(
                        name=name,
                        pid=res.pid,
                        cpu_percent=res.cpu_percent,
                        memory_mb=res.memory_mb,
                        mem_trend=res.mem_trend,
                        cpu_sparkline=tuple(res.cpu_history),
                        mem_sparkline=tuple(res.mem_history),
                    )
                )
            return sorted(snapshots, key=lambda n: n.name)

    def snapshot_topics(self) -> List[TopicSnapshot]:
        with self._lock:
            return sorted(self._topics.values(), key=lambda t: t.name)

    def snapshot_services(self) -> List["ServiceSnapshot"]:
        with self._lock:
            return sorted(self._services.values(), key=lambda s: s.name)

    def snapshot_node_plot(self, mode: str, window_seconds: float = 30.0):
        cutoff = time.monotonic() - window_seconds
        with self._lock:
            pinned = (
                self._pinned_nodes
                if self._pinned_nodes
                else set(self._node_plot_history.keys())
            )
            result = {}
            for node, history in self._node_plot_history.items():
                if node not in pinned:
                    continue
                pts = []
                for ts, cpu, mem in history:
                    if ts >= cutoff:
                        val = cpu if mode == "cpu" else mem
                        pts.append(PlotPoint(timestamp=ts, value=val))
                result[node] = pts
            return result

    def snapshot_params(self, node: str) -> List[ParamSnapshot]:
        with self._lock:
            return list(self._params.get(node, {}).values())

    def snapshot_param_nodes(self) -> List[str]:
        """List of nodes for which we have cached params."""
        with self._lock:
            return sorted(self._params.keys())

    def snapshot_plot(
        self, window_seconds: Optional[float] = None
    ) -> Dict[str, List[PlotPoint]]:
        """
        Returns plot data trimmed to the current time window.
        Keys are topic names, values are time-ordered lists of PlotPoints.
        """
        window = window_seconds or self._plot_window
        cutoff = time.monotonic() - window

        with self._lock:
            result = {}
            for topic, series in self._plot_series.items():
                trimmed = [p for p in series.points if p.timestamp >= cutoff]
                result[topic] = trimmed
            return result

    def snapshot_plot_topics(self) -> List[str]:
        """Currently pinned plot topics."""
        with self._lock:
            return list(self._plot_series.keys())

    def snapshot_topic_fields(self, topic: str) -> List[str]:
        """Return the known plottable field paths for *topic*.

        Returns an empty list if the topic has never been subscribed to or if
        no field metadata has been recorded yet (bridge hasn't seen a message).
        The TUI uses this to populate the field-picker dropdown; when the list
        is empty it falls back to the ``"data"`` default.
        """
        with self._lock:
            return list(self._topic_fields.get(topic, []))

    def snapshot_param_changes(self) -> List[ParamChangeMarker]:
        """All parameter change markers (for plot overlay)."""
        with self._lock:
            return list(self._param_change_history)

    def update_tf(
        self,
        parent: str,
        child: str,
        stamp_age_s: float = -1.0,
        last_received: Optional[float] = None,
        is_static: bool = False,
        publisher_node: str = "",
    ) -> None:
        key = (parent, child)
        tf = TFTransform(
            parent=parent,
            child=child,
            stamp_age_s=stamp_age_s,
            last_received=last_received
            if last_received is not None
            else time.monotonic(),
            is_static=is_static,
            publisher_node=publisher_node,
        )
        with self._lock:
            if is_static:
                self._tf_static[key] = tf
            else:
                self._tf_transforms[key] = tf

    def snapshot_tf(self) -> Tuple[List["TFTransform"], List["TFTransform"]]:
        """Returns (dynamic_transforms, static_transforms)."""
        with self._lock:
            return list(self._tf_transforms.values()), list(self._tf_static.values())

    def total_log_count(self) -> int:
        """Return total number of stored log lines (no filtering, no copying)."""
        with self._lock:
            return len(self._log_lines)

    def snapshot_logs(
        self,
        node_filter: Optional[str] = None,
        level_filter: Optional[str] = None,
        keyword_filter: Optional[str] = None,
        max_lines: int = 500,
    ) -> List[Tuple[float, str, str]]:
        """Returns list of (timestamp, level, line)."""
        with self._lock:
            lines = list(self._log_lines)

        # Normalise old 2-tuple entries from before the level field was added
        normalised = []
        for entry in lines:
            if len(entry) == 2:
                normalised.append((entry[0], "INFO", entry[1]))
            else:
                normalised.append(entry)
        lines = normalised

        if node_filter:
            lines = [(t, lv, l) for t, lv, l in lines if node_filter in l]
        if level_filter and level_filter != "ALL":
            lines = [(t, lv, l) for t, lv, l in lines if lv == level_filter]
        if keyword_filter:
            kw = keyword_filter.lower()
            lines = [(t, lv, l) for t, lv, l in lines if kw in l.lower()]

        return lines[-max_lines:]

    def snapshot_ros_connected(self) -> bool:
        with self._lock:
            return self._ros_connected

    def is_plot_topic_pinned(self, topic: str, field: Optional[str] = None) -> bool:
        """Return True if *topic* (optionally with *field*) is currently pinned.

        If *field* is given, checks for the exact ``"topic::field"`` key.
        If *field* is omitted, returns True if *any* field of *topic* is pinned.
        """
        with self._lock:
            if field is not None:
                return f"{topic}::{field}" in self._plot_series
            prefix = f"{topic}::"
            return any(k == topic or k.startswith(prefix) for k in self._plot_series)
