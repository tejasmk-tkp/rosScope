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
    cpu_sparkline: Tuple[float, ...]   # last N samples for rendering
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
    qos_mismatch: bool          # True if pub/sub QoS are incompatible
    last_msg_repr: str          # str of last message (truncated)


@dataclass(frozen=True)
class ParamSnapshot:
    node: str
    name: str
    value: Any
    type_name: str              # 'bool', 'int', 'double', 'string', 'list'


@dataclass(frozen=True)
class PlotPoint:
    timestamp: float            # monotonic
    value: float


@dataclass(frozen=True)
class ParamChangeMarker:
    timestamp: float
    node: str
    param: str
    old_value: Any
    new_value: Any


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
        self._lock = threading.RLock()   # Reentrant so update methods can call each other
        self._plot_window = plot_window_seconds

        # Node resources (from proc_utils.ResourceMonitor)
        self._node_resources: Dict[str, NodeResources] = {}
        self._system_resources: Optional[SystemResources] = None

        # Topics
        self._topics: Dict[str, TopicSnapshot] = {}

        # Parameters: {node_name: {param_name: ParamSnapshot}}
        self._params: Dict[str, Dict[str, ParamSnapshot]] = {}
        self._param_change_history: deque = deque(maxlen=200)

        # Plot series: {"topic::field": _PlotSeries}
        self._plot_series: Dict[str, _PlotSeries] = {}
        # Available fields per topic: {topic: [field_path, ...]}
        self._topic_fields: Dict[str, List[str]] = {}

        # Log lines from /rosout
        self._log_lines: deque = deque(maxlen=500)

        # Connection state
        self._ros_connected: bool = False
        self._last_update: float = 0.0

    # -----------------------------------------------------------------------
    # Write API (called from ROS bridge thread)
    # -----------------------------------------------------------------------

    def update_node_resources(self,
                               resources: Dict[str, NodeResources],
                               system: SystemResources) -> None:
        with self._lock:
            self._node_resources = dict(resources)
            self._system_resources = system
            self._last_update = time.monotonic()

    def update_topics(self, topics: Dict[str, TopicSnapshot]) -> None:
        with self._lock:
            self._topics = dict(topics)

    def update_params(self, node: str, params: Dict[str, ParamSnapshot]) -> None:
        with self._lock:
            self._params[node] = dict(params)

    def record_param_change(self, marker: ParamChangeMarker) -> None:
        with self._lock:
            self._param_change_history.append(marker)

    def append_plot_point(self, topic: str, field: str, timestamp: float, value: float) -> None:
        key = f"{topic}::{field}"
        with self._lock:
            if key not in self._plot_series:
                self._plot_series[key] = _PlotSeries(topic=topic)
            self._plot_series[key].points.append(
                PlotPoint(timestamp=timestamp, value=value)
            )

    def update_topic_fields(self, topic: str, fields: List[str]) -> None:
        """Register available numeric field paths for a topic."""
        with self._lock:
            self._topic_fields[topic] = list(fields)

    def append_log_line(self, line: str) -> None:
        with self._lock:
            self._log_lines.append((time.monotonic(), line))

    def set_ros_connected(self, connected: bool) -> None:
        with self._lock:
            self._ros_connected = connected

    def add_plot_topic(self, topic: str, field: str = "data") -> None:
        """Pin a topic+field to the plot panel."""
        key = f"{topic}::{field}"
        with self._lock:
            if key not in self._plot_series:
                self._plot_series[key] = _PlotSeries(topic=topic)

    def remove_plot_topic(self, topic: str, field: Optional[str] = None) -> None:
        with self._lock:
            if field is not None:
                self._plot_series.pop(f"{topic}::{field}", None)
            else:
                # Remove all fields for this topic
                keys = [k for k in self._plot_series if k.startswith(f"{topic}::")]
                for k in keys:
                    del self._plot_series[k]

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
                snapshots.append(NodeSnapshot(
                    name=name,
                    pid=res.pid,
                    cpu_percent=res.cpu_percent,
                    memory_mb=res.memory_mb,
                    mem_trend=res.mem_trend,
                    cpu_sparkline=tuple(res.cpu_history),
                    mem_sparkline=tuple(res.mem_history),
                ))
            return sorted(snapshots, key=lambda n: n.name)

    def snapshot_topics(self) -> List[TopicSnapshot]:
        with self._lock:
            return sorted(self._topics.values(), key=lambda t: t.name)

    def snapshot_params(self, node: str) -> List[ParamSnapshot]:
        with self._lock:
            return list(self._params.get(node, {}).values())

    def snapshot_param_nodes(self) -> List[str]:
        """List of nodes for which we have cached params."""
        with self._lock:
            return sorted(self._params.keys())

    def snapshot_plot(self, window_seconds: Optional[float] = None) -> Dict[str, List[PlotPoint]]:
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
        """Currently pinned plot topic keys ("topic::field" format)."""
        with self._lock:
            return list(self._plot_series.keys())

    def snapshot_topic_fields(self, topic: str) -> List[str]:
        """Available numeric field paths for a topic."""
        with self._lock:
            return list(self._topic_fields.get(topic, []))

    def snapshot_all_topic_fields(self) -> Dict[str, List[str]]:
        """All known topic -> fields mappings."""
        with self._lock:
            return {t: list(f) for t, f in self._topic_fields.items()}

    def snapshot_param_changes(self) -> List[ParamChangeMarker]:
        """All parameter change markers (for plot overlay)."""
        with self._lock:
            return list(self._param_change_history)

    def snapshot_logs(self,
                      node_filter: Optional[str] = None,
                      keyword_filter: Optional[str] = None,
                      max_lines: int = 100) -> List[Tuple[float, str]]:
        with self._lock:
            lines = list(self._log_lines)

        if node_filter:
            lines = [(t, l) for t, l in lines if node_filter in l]
        if keyword_filter:
            kw = keyword_filter.lower()
            lines = [(t, l) for t, l in lines if kw in l.lower()]

        return lines[-max_lines:]

    def snapshot_ros_connected(self) -> bool:
        with self._lock:
            return self._ros_connected

    def is_plot_topic_pinned(self, topic: str, field: Optional[str] = None) -> bool:
        with self._lock:
            if field is not None:
                return f"{topic}::{field}" in self._plot_series
            return any(k.startswith(f"{topic}::") for k in self._plot_series)
