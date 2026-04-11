"""
proc_utils.py — /proc-based process inspection utilities.

Adapted from system_supervisor.py (v4.3).
Key changes vs supervisor:
  - No kill/signal logic (TUI is read-only observer)
  - Dynamic discovery from ros2 node list (not a fixed registry)
  - Component container support via ros2 component list
  - Rolling sample buffer + linear regression for trend classification
  - 1Hz sampling rate (supervisor used 0.5Hz)
"""

import os
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Constants (cached at module load, same as supervisor)
# ---------------------------------------------------------------------------

_CLK_TCK: int = os.sysconf("SC_CLK_TCK")
_MY_PID: int = os.getpid()

# Handles process names with spaces/parens in /proc/PID/stat
_PROC_STAT_COMM_END = re.compile(rb"\) [A-Za-z] ")

# How many samples to keep for trend analysis (~60s at 1Hz)
_TREND_WINDOW = 60

# Thresholds
_CPU_HIGH_THRESHOLD = 50.0       # % — above this = HIGH
_MEM_HIGH_THRESHOLD_MB = 512.0   # MB — above this = HIGH
_MEM_GROW_SLOPE_THRESHOLD = 0.5  # MB/s — above this = GROWING (leak suspect)

# Processes we never match, same safety list as supervisor
_SAFE_PROCESS_NAMES = (
    b"sshd", b"ssh", b"containerd", b"dockerd",
    b"bash", b"/bin/sh", b"tmux", b"screen",
    b"rostui",  # Don't match ourselves
)


# ---------------------------------------------------------------------------
# Trend classification
# ---------------------------------------------------------------------------

class MemTrend(Enum):
    STABLE  = "stable"
    HIGH    = "high"
    GROWING = "growing"   # Linear regression slope exceeds threshold — leak suspect


def _linear_slope(samples: deque) -> float:
    """
    Compute slope (MB/sample) of a deque of float values via least-squares.
    Returns 0.0 if fewer than 3 samples.
    """
    n = len(samples)
    if n < 3:
        return 0.0

    xs = list(range(n))
    ys = list(samples)

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)

    return (num / den) if den != 0 else 0.0


def classify_mem_trend(samples: deque, current_mb: float) -> MemTrend:
    """
    Classify memory trend from a rolling sample buffer.

    Priority: GROWING > HIGH > STABLE
    GROWING is flagged even if absolute value is low (early leak detection).
    """
    slope = _linear_slope(samples)
    if slope > _MEM_GROW_SLOPE_THRESHOLD:
        return MemTrend.GROWING
    if current_mb > _MEM_HIGH_THRESHOLD_MB:
        return MemTrend.HIGH
    return MemTrend.STABLE


# ---------------------------------------------------------------------------
# Per-node resource state (replaces MonitoredNode's resource fields)
# ---------------------------------------------------------------------------

@dataclass
class NodeResources:
    """
    Runtime resource state for a single discovered ROS2 node.
    One instance per node, updated in-place each sample tick.
    """
    ros_node_name: str          # e.g. /controller_server
    pid: Optional[int] = None

    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    mem_trend: MemTrend = MemTrend.STABLE

    # Rolling history for trend + sparkline
    cpu_history:  deque = field(default_factory=lambda: deque(maxlen=_TREND_WINDOW))
    mem_history:  deque = field(default_factory=lambda: deque(maxlen=_TREND_WINDOW))

    # Internal: delta tracking (same pattern as supervisor)
    _prev_cpu_ticks: int  = 0
    _prev_cpu_time:  float = 0.0


# ---------------------------------------------------------------------------
# /proc utilities (read-only, no kill logic)
# ---------------------------------------------------------------------------

def find_pid_for_node(ros_node_name: str,
                      exclude_pids: Optional[Set[int]] = None) -> Optional[int]:
    """
    Find the PID of a ROS2 node by matching its node name in /proc/*/cmdline.

    Tries two patterns:
      1. The node name itself (e.g. controller_server)
      2. __node:=<name> (remapped nodes)

    Falls back to ros2 component list for nodes running inside a container.
    Returns the first match or None.
    """
    # Strip leading slash for cmdline matching
    bare_name = ros_node_name.lstrip("/").split("/")[-1]

    patterns = [
        bare_name.encode(),
        f"__node:={bare_name}".encode(),
    ]

    if exclude_pids is None:
        exclude_pids = {_MY_PID, 1}
    else:
        exclude_pids = exclude_pids | {_MY_PID, 1}

    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue

            pid = int(entry.name)
            if pid in exclude_pids:
                continue

            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read()
                if not cmdline:
                    continue

                exe = cmdline.split(b"\x00", 1)[0].lower()
                if any(s in exe for s in _SAFE_PROCESS_NAMES):
                    continue

                if any(p in cmdline for p in patterns):
                    return pid

            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue

    except OSError:
        pass

    # Fallback: check component containers
    return _find_pid_in_component_container(bare_name)


def _find_pid_in_component_container(bare_node_name: str) -> Optional[int]:
    """
    For nodes loaded into a component container, the process name won't match.
    We find the container PID by calling 'ros2 component list' and matching.

    Returns the container PID if the node is found inside one, else None.
    """
    try:
        result = subprocess.run(
            ["ros2", "component", "list"],
            capture_output=True, text=True, timeout=2.0
        )
        lines = result.stdout.splitlines()

        # Output format:
        #   /component_container
        #     1  /controller_server
        #     2  /planner_server
        container_name = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("/") and not stripped[1:].isdigit():
                container_name = stripped.lstrip("/")
            elif bare_node_name in stripped and container_name:
                # Found our node — now find the container's PID
                return find_pid_for_node(f"/{container_name}")

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return None


def read_proc_resources(pid: int) -> Tuple[int, float]:
    """
    Read CPU ticks and RSS memory for a PID directly from /proc.

    Identical logic to system_supervisor.read_proc_resources().
    Uses /proc/PID/stat for CPU and /proc/PID/status for VmRSS
    (VmRSS is more reliable than stat's rss field).

    Returns:
        (cpu_ticks: int, rss_kb: float) or (0, 0.0) on any error.
    """
    cpu_ticks = 0
    rss_kb = 0.0

    # --- CPU from /proc/PID/stat ---
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()

        match = _PROC_STAT_COMM_END.search(data)
        if match:
            fields = data[match.end():].split()
            # utime(11) + stime(12) + cutime(13) + cstime(14) after state field
            if len(fields) > 14:
                cpu_ticks = sum(int(fields[i]) for i in (11, 12, 13, 14))

    except (FileNotFoundError, PermissionError, ProcessLookupError,
            IndexError, ValueError):
        pass

    # --- RSS from /proc/PID/status ---
    try:
        with open(f"/proc/{pid}/status", "rb") as f:
            for line in f:
                if line.startswith(b"VmRSS:"):
                    rss_kb = float(line.split()[1])
                    break

    except (FileNotFoundError, PermissionError, ProcessLookupError,
            IndexError, ValueError):
        pass

    return cpu_ticks, rss_kb


def pid_exists(pid: int) -> bool:
    """Check if PID exists via /proc (no signal, no subprocess)."""
    return os.path.isdir(f"/proc/{pid}")


# ---------------------------------------------------------------------------
# System-level CPU and memory (for the top status bar)
# ---------------------------------------------------------------------------

@dataclass
class SystemResources:
    cpu_percent: float = 0.0
    memory_used_mb: float = 0.0
    memory_total_mb: float = 0.0

    _prev_total_cpu: int = 0
    _prev_idle_cpu:  int = 0

    @property
    def memory_percent(self) -> float:
        if self.memory_total_mb == 0:
            return 0.0
        return (self.memory_used_mb / self.memory_total_mb) * 100.0


def read_system_resources(state: SystemResources) -> None:
    """
    Update a SystemResources instance in-place.
    Reads /proc/stat (CPU) and /proc/meminfo (RAM).
    Identical logic to supervisor's _read_system_cpu/_read_system_memory.
    """
    # CPU
    try:
        with open("/proc/stat", "r") as f:
            parts = f.readline().split()
        total = sum(int(p) for p in parts[1:9])
        idle  = int(parts[4]) + int(parts[5])

        dt = total - state._prev_total_cpu
        di = idle  - state._prev_idle_cpu
        if dt > 0:
            state.cpu_percent = ((dt - di) / dt) * 100.0

        state._prev_total_cpu = total
        state._prev_idle_cpu  = idle

    except Exception:
        pass

    # Memory
    try:
        mem: Dict[str, int] = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1])
                if "MemTotal" in mem and "MemAvailable" in mem:
                    break

        total_mb = mem.get("MemTotal", 0) / 1024.0
        avail_mb = mem.get("MemAvailable", 0) / 1024.0
        state.memory_total_mb = total_mb
        state.memory_used_mb  = total_mb - avail_mb

    except Exception:
        pass


# ---------------------------------------------------------------------------
# ResourceMonitor — drives all per-node sampling at 1Hz
# ---------------------------------------------------------------------------

class ResourceMonitor:
    """
    Maintains a dict of NodeResources keyed by ROS node name.
    Called by the ROS bridge thread at ~1Hz.

    Usage:
        monitor = ResourceMonitor()
        monitor.update(["node_a", "node_b"])   # pass current ros2 node list
        res = monitor.get("/node_a")
    """

    def __init__(self):
        self._nodes: Dict[str, NodeResources] = {}
        self._system = SystemResources()

    # -- Public API ----------------------------------------------------------

    def update(self, active_ros_nodes: List[str]) -> None:
        """
        Sync node registry with the current live node list, then sample all.
        Call this at ~1Hz from the ROS bridge thread.
        """
        self._sync_registry(active_ros_nodes)
        self._sample_all()
        read_system_resources(self._system)

    def get(self, ros_node_name: str) -> Optional[NodeResources]:
        return self._nodes.get(ros_node_name)

    def all_nodes(self) -> Dict[str, NodeResources]:
        return dict(self._nodes)

    @property
    def system(self) -> SystemResources:
        return self._system

    # -- Internal ------------------------------------------------------------

    def _sync_registry(self, active_ros_nodes: List[str]) -> None:
        """Add new nodes, remove stale ones."""
        current = set(active_ros_nodes)
        existing = set(self._nodes.keys())

        for name in current - existing:
            self._nodes[name] = NodeResources(ros_node_name=name)

        for name in existing - current:
            del self._nodes[name]

    def _sample_all(self) -> None:
        now = time.monotonic()

        for name, res in self._nodes.items():

            # Re-resolve PID if we don't have one or if it disappeared
            if res.pid is None or not pid_exists(res.pid):
                res.pid = find_pid_for_node(name)

            if res.pid is None:
                # Node is in ROS graph but we can't find its process
                # (pure Python node? intra-process?) — record zeros
                res.cpu_percent = 0.0
                res.memory_mb   = 0.0
                res.cpu_history.append(0.0)
                res.mem_history.append(0.0)
                res.mem_trend = MemTrend.STABLE
                continue

            cpu_ticks, rss_kb = read_proc_resources(res.pid)

            # CPU% — delta ticks / elapsed time (same formula as supervisor)
            if res._prev_cpu_time > 0:
                dt = now - res._prev_cpu_time
                if dt > 0:
                    res.cpu_percent = (
                        (cpu_ticks - res._prev_cpu_ticks) / _CLK_TCK / dt
                    ) * 100.0

            res._prev_cpu_ticks = cpu_ticks
            res._prev_cpu_time  = now

            res.memory_mb = rss_kb / 1024.0

            # Rolling history
            res.cpu_history.append(res.cpu_percent)
            res.mem_history.append(res.memory_mb)

            # Trend classification
            res.mem_trend = classify_mem_trend(res.mem_history, res.memory_mb)
