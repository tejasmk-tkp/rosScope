"""
rostui.py — rosTUI main application entry point.

Usage:
    python3 rostui.py
    python3 rostui.py --profile nav_tuning
    python3 rostui.py --config my_config.yaml
"""

import argparse
import sys
import time
from typing import Optional

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, TabbedContent, TabPane, Static
from textual.containers import Horizontal
from textual.reactive import reactive
from rich.text import Text

from core.data_store import DataStore
from core.ros_bridge import RosBridge
from panels.node_overview import NodeOverviewPanel
from panels.topic_monitor import TopicMonitorPanel
from panels.param_tuner import ParamTunerPanel
from panels.plot_panel import PlotPanel


# ---------------------------------------------------------------------------
# Top status bar
# ---------------------------------------------------------------------------

class SystemStatusBar(Static):
    """One-line summary bar: ROS status, CPU, RAM, node count."""

    DEFAULT_CSS = """
    SystemStatusBar {
        height: 1;
        dock: top;
        padding: 0 1;
        background: $surface-darken-2;
        color: $text;
    }
    """

    def __init__(self, store: DataStore, **kwargs):
        super().__init__(**kwargs)
        self._store = store

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        connected = self._store.snapshot_ros_connected()
        sys_snap  = self._store.snapshot_system()

        if not connected:
            self.update("[bold red]● ROS DISCONNECTED[/bold red]")
            return

        if sys_snap is None:
            self.update("[yellow]● Connecting…[/yellow]")
            return

        cpu_color = "red" if sys_snap.cpu_percent > 80 else \
                    "yellow" if sys_snap.cpu_percent > 50 else "green"
        mem_color = "red" if sys_snap.memory_percent > 85 else \
                    "yellow" if sys_snap.memory_percent > 60 else "green"

        self.update(
            f"[bold green]● ROS[/bold green]  "
            f"[{cpu_color}]CPU {sys_snap.cpu_percent:4.1f}%[/{cpu_color}]  "
            f"[{mem_color}]RAM {sys_snap.memory_used_mb:.0f}/{sys_snap.memory_total_mb:.0f} MB "
            f"({sys_snap.memory_percent:.0f}%)[/{mem_color}]  "
            f"[cyan]Nodes: {sys_snap.ros_node_count}[/cyan]"
        )


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class RosTUI(App):

    TITLE = "rosTUI"
    SUB_TITLE = "ROS2 System Inspector"

    CSS = """
    TabbedContent {
        height: 1fr;
    }
    TabPane {
        padding: 0;
    }
    """

    BINDINGS = [
        ("q",   "quit",          "Quit"),
        ("1",   "switch_tab('nodes')",   "Nodes"),
        ("2",   "switch_tab('topics')",  "Topics"),
        ("3",   "switch_tab('params')",  "Params"),
        ("4",   "switch_tab('plot')",    "Plot"),
        ("r",   "force_refresh",         "Refresh"),
    ]

    def __init__(self, store: DataStore, bridge: RosBridge, **kwargs):
        super().__init__(**kwargs)
        self._store  = store
        self._bridge = bridge

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield SystemStatusBar(self._store, id="status_bar")

        with TabbedContent(initial="nodes"):
            with TabPane("Nodes [1]", id="nodes"):
                yield NodeOverviewPanel(self._store, id="node_overview")

            with TabPane("Topics [2]", id="topics"):
                yield TopicMonitorPanel(self._store, id="topic_monitor")

            with TabPane("Params [3]", id="params"):
                yield ParamTunerPanel(self._store, self._bridge, id="param_tuner")

            with TabPane("Plot [4]", id="plot"):
                yield PlotPanel(self._store, self._bridge, id="plot_panel")

        yield Footer()

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_force_refresh(self) -> None:
        """Force immediate rediscovery — useful after launching new nodes."""
        if self._bridge:
            self._bridge.fetch_params(
                self._store.snapshot_param_nodes()[0]
                if self._store.snapshot_param_nodes() else ""
            )

    def on_mount(self) -> None:
        self.title = "rosTUI"

    async def on_unmount(self) -> None:
        self._bridge.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="rosTUI — ROS2 System Inspector")
    parser.add_argument("--profile", default=None,
                        help="Profile name from config file")
    parser.add_argument("--config",  default=None,
                        help="Path to config YAML")
    parser.add_argument("--no-ros",  action="store_true",
                        help="Run without ROS (UI demo mode)")
    args = parser.parse_args()

    store  = DataStore(plot_window_seconds=300)   # 5min max buffer
    bridge = RosBridge(store)

    if not args.no_ros:
        bridge.start()
    else:
        # Demo mode: inject some fake data so the UI is visible
        _inject_demo_data(store)

    app = RosTUI(store, bridge)
    app.run()


def _inject_demo_data(store: DataStore) -> None:
    """Populate store with fake data for UI development/demo without ROS."""
    import math
    import threading
    from core.proc_utils import NodeResources, SystemResources, MemTrend
    from core.data_store import TopicSnapshot, ParamSnapshot

    def _run():
        import math as _math
        t = 0.0
        while True:
            # System
            sr = SystemResources(
                cpu_percent=30.0 + 20 * abs(_math.sin(t / 10)),
                memory_used_mb=2048.0 + t * 0.5,
                memory_total_mb=8192.0,
            )
            store.set_ros_connected(True)

            # Nodes
            nodes = {
                "/controller_server": NodeResources(
                    ros_node_name="/controller_server",
                    pid=1234,
                    cpu_percent=12.0 + 5 * _math.sin(t),
                    memory_mb=180.0 + t * 0.1,
                    mem_trend=MemTrend.STABLE,
                ),
                "/planner_server": NodeResources(
                    ros_node_name="/planner_server",
                    pid=1235,
                    cpu_percent=8.0,
                    memory_mb=95.0,
                    mem_trend=MemTrend.STABLE,
                ),
                "/zed_node": NodeResources(
                    ros_node_name="/zed_node",
                    pid=1236,
                    cpu_percent=45.0,
                    memory_mb=620.0 + t * 0.8,  # slowly growing
                    mem_trend=MemTrend.GROWING if t > 10 else MemTrend.HIGH,
                ),
                "/pid_controller": NodeResources(
                    ros_node_name="/pid_controller",
                    pid=1237,
                    cpu_percent=3.0,
                    memory_mb=42.0,
                    mem_trend=MemTrend.STABLE,
                ),
            }
            # Build fake history
            from collections import deque
            for name, nr in nodes.items():
                nr.cpu_history = deque(
                    [nr.cpu_percent + 2 * _math.sin(t + i) for i in range(60)], maxlen=60
                )
                nr.mem_history = deque(
                    [nr.memory_mb - i * 0.1 for i in range(60)], maxlen=60
                )

            store.update_node_resources(nodes, sr)

            # Topics
            topics = {
                "/cmd_vel": TopicSnapshot(
                    name="/cmd_vel", msg_type="geometry_msgs/msg/Twist",
                    pub_count=1, sub_count=2,
                    frequency_hz=10.0, qos_reliability="reliable",
                    qos_durability="volatile", qos_mismatch=False,
                    last_msg_repr="linear.x=0.5 angular.z=0.0",
                ),
                "/odom": TopicSnapshot(
                    name="/odom", msg_type="nav_msgs/msg/Odometry",
                    pub_count=1, sub_count=3,
                    frequency_hz=50.0, qos_reliability="best_effort",
                    qos_durability="volatile", qos_mismatch=False,
                    last_msg_repr="pose.x=1.23 pose.y=0.45",
                ),
                "/scan": TopicSnapshot(
                    name="/scan", msg_type="sensor_msgs/msg/LaserScan",
                    pub_count=1, sub_count=1,
                    frequency_hz=15.0, qos_reliability="best_effort",
                    qos_durability="volatile", qos_mismatch=True,  # mismatch demo
                    last_msg_repr="ranges[360]",
                ),
            }
            store.update_topics(topics)

            # Params
            store.update_params("/controller_server", {
                "max_vel_x":  ParamSnapshot("/controller_server", "max_vel_x",  0.5,  "float"),
                "max_vel_theta": ParamSnapshot("/controller_server", "max_vel_theta", 1.0, "float"),
                "acc_lim_x":  ParamSnapshot("/controller_server", "acc_lim_x",  2.5,  "float"),
                "xy_goal_tolerance": ParamSnapshot("/controller_server", "xy_goal_tolerance", 0.05, "float"),
            })

            # Plot data
            store.add_plot_topic("/cmd_vel")
            store.add_plot_topic("/odom")
            store.append_plot_point("/cmd_vel", time.monotonic(),
                                    0.5 * _math.sin(t * 0.5))
            store.append_plot_point("/odom",    time.monotonic(),
                                    0.3 * _math.cos(t * 0.3))

            t += 1.0
            time.sleep(1.0)

    threading.Thread(target=_run, daemon=True).start()


if __name__ == "__main__":
    main()