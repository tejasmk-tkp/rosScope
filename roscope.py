"""
roscope.py — rosScope main application.

python3 roscope.py [--no-ros]

Layout: plot top 55%, tabbed info panels bottom 45%.

Keys:
  1-3     Switch bottom tabs (Nodes / Topics / Params)
  r       Force refresh
  q       Quit
"""

import argparse
import threading
import time

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, TabbedContent, TabPane, Static
from textual.containers import Vertical
from textual.binding import Binding

from core.data_store import DataStore
from core.ros_bridge import RosBridge
from panels.node_overview import NodeOverviewPanel
from panels.topic_monitor import TopicMonitorPanel
from panels.param_tuner import ParamTunerPanel
from panels.plot_panel import PlotPanel


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

class SystemStatusBar(Static):
    DEFAULT_CSS = """
    SystemStatusBar {
        height: 1;
        dock: top;
        padding: 0 1;
        background: $surface-darken-2;
    }
    """

    def __init__(self, store: DataStore, **kwargs):
        super().__init__(**kwargs)
        self._store = store

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        connected = self._store.snapshot_ros_connected()
        s = self._store.snapshot_system()
        if not connected:
            self.update("[bold red]● ROS DISCONNECTED[/bold red]  [dim]waiting…[/dim]")
            return
        if s is None:
            self.update("[yellow]● Connecting…[/yellow]")
            return
        cc = "red" if s.cpu_percent > 80 else "yellow" if s.cpu_percent > 50 else "green"
        mc = "red" if s.memory_percent > 85 else "yellow" if s.memory_percent > 60 else "green"
        self.update(
            f"[bold green]● ROS[/bold green]  "
            f"[{cc}]CPU {s.cpu_percent:4.1f}%[/{cc}]  "
            f"[{mc}]RAM {s.memory_used_mb:.0f}/{s.memory_total_mb:.0f} MB "
            f"({s.memory_percent:.0f}%)[/{mc}]  "
            f"[cyan]Nodes: {s.ros_node_count}[/cyan]"
        )


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class RosScope(App):

    TITLE = "rosScope"

    CSS = """
    RosScope     { layout: vertical; }
    #plot_pane   { height: 55%; border-bottom: solid $accent; }
    #lower_tabs  { height: 1fr; }
    TabbedContent { height: 1fr; }
    TabPane       { padding: 0; }
    """

    BINDINGS = [
        Binding("q", "quit",           "Quit"),
        Binding("r", "force_refresh",  "Refresh"),
        Binding("tab", "focus_toggle", "Plot↕Panels"),
        Binding("1", "switch_tab('tab_nodes')",  "Nodes",  show=True),
        Binding("2", "switch_tab('tab_topics')", "Topics", show=True),
        Binding("3", "switch_tab('tab_params')", "Params", show=True),
    ]

    def __init__(self, store: DataStore, bridge: RosBridge, **kwargs):
        super().__init__(**kwargs)
        self._store  = store
        self._bridge = bridge

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield SystemStatusBar(self._store)
        yield PlotPanel(self._store, self._bridge, id="plot_pane")
        with TabbedContent(initial="tab_nodes", id="lower_tabs"):
            with TabPane("Nodes [1]",  id="tab_nodes"):
                yield NodeOverviewPanel(self._store)
            with TabPane("Topics [2]", id="tab_topics"):
                yield TopicMonitorPanel(self._store)
            with TabPane("Params [3]", id="tab_params"):
                yield ParamTunerPanel(self._store, self._bridge)
        yield Footer()

    def action_focus_toggle(self) -> None:
        """Toggle focus between plot pane (top) and lower tabs (bottom)."""
        plot = self.query_one("#plot_pane")
        lower = self.query_one("#lower_tabs")
        # Find which area currently has focus
        focused = self.focused
        # Walk up the focus tree to see if we're inside plot or lower
        node = focused
        in_plot = False
        while node is not None:
            if node is plot:
                in_plot = True
                break
            node = node.parent
        if in_plot:
            # Move focus to the active tab's first focusable widget
            lower.focus()
        else:
            # Move focus to the topic input in the plot pane
            try:
                self.query_one("#topic_input").focus()
            except Exception:
                plot.focus()

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#lower_tabs", TabbedContent).active = tab_id

    def action_force_refresh(self) -> None:
        nodes = self._store.snapshot_param_nodes()
        if nodes and self._bridge:
            self._bridge.fetch_params(nodes[0])

    async def on_unmount(self) -> None:
        self._bridge.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="rosScope — ROS2 System Inspector")
    parser.add_argument("--no-ros",  action="store_true", help="Demo mode without ROS")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--config",  default=None)
    args = parser.parse_args()

    store  = DataStore(plot_window_seconds=300)
    bridge = RosBridge(store)

    if not args.no_ros:
        bridge.start()
    else:
        _inject_demo_data(store)

    RosScope(store, bridge).run()


def _inject_demo_data(store: DataStore) -> None:
    import math as _math
    from collections import deque
    from core.proc_utils import NodeResources, SystemResources, MemTrend
    from core.data_store import TopicSnapshot, ParamSnapshot

    def _run():
        t = 0.0
        while True:
            sr = SystemResources(
                cpu_percent=30.0 + 20 * abs(_math.sin(t / 10)),
                memory_used_mb=2048.0 + t * 0.5,
                memory_total_mb=8192.0,
            )
            store.set_ros_connected(True)
            nodes = {
                "/controller_server": NodeResources(
                    ros_node_name="/controller_server", pid=1234,
                    cpu_percent=12.0 + 5 * _math.sin(t), memory_mb=180.0 + t * 0.1,
                    mem_trend=MemTrend.STABLE),
                "/planner_server": NodeResources(
                    ros_node_name="/planner_server", pid=1235,
                    cpu_percent=8.0, memory_mb=95.0, mem_trend=MemTrend.STABLE),
                "/zed_node": NodeResources(
                    ros_node_name="/zed_node", pid=1236,
                    cpu_percent=45.0, memory_mb=620.0 + t * 0.8,
                    mem_trend=MemTrend.GROWING if t > 10 else MemTrend.HIGH),
                "/pid_controller": NodeResources(
                    ros_node_name="/pid_controller", pid=1237,
                    cpu_percent=3.0, memory_mb=42.0, mem_trend=MemTrend.STABLE),
            }
            for name, nr in nodes.items():
                nr.cpu_history = deque(
                    [nr.cpu_percent + 2 * _math.sin(t + i) for i in range(60)], maxlen=60)
                nr.mem_history = deque(
                    [nr.memory_mb - i * 0.1 for i in range(60)], maxlen=60)
            store.update_node_resources(nodes, sr)
            store.update_topics({
                "/cmd_vel": TopicSnapshot("/cmd_vel", "geometry_msgs/msg/Twist",
                    1, 2, 10.0, "reliable", "volatile", False, "linear.x=0.5 angular.z=0.0"),
                "/odom": TopicSnapshot("/odom", "nav_msgs/msg/Odometry",
                    1, 3, 50.0, "best_effort", "volatile", False, "pose.x=1.23"),
                "/scan": TopicSnapshot("/scan", "sensor_msgs/msg/LaserScan",
                    1, 1, 15.0, "best_effort", "volatile", True, "ranges[360]"),
            })
            store.update_params("/controller_server", {
                "max_vel_x":         ParamSnapshot("/controller_server", "max_vel_x",         0.5,  "float"),
                "max_vel_theta":     ParamSnapshot("/controller_server", "max_vel_theta",     1.0,  "float"),
                "acc_lim_x":         ParamSnapshot("/controller_server", "acc_lim_x",         2.5,  "float"),
                "xy_goal_tolerance": ParamSnapshot("/controller_server", "xy_goal_tolerance", 0.05, "float"),
            })
            store.add_plot_topic("/cmd_vel", "linear.x")
            store.add_plot_topic("/cmd_vel", "angular.z")
            store.add_plot_topic("/odom",    "twist.twist.linear.x")
            store.update_topic_fields("/cmd_vel", ["linear.x", "linear.y", "linear.z", "angular.x", "angular.y", "angular.z"])
            store.update_topic_fields("/odom",    ["twist.twist.linear.x", "twist.twist.angular.z", "pose.pose.position.x", "pose.pose.position.y"])
            store.update_topic_fields("/scan",    ["angle_min", "angle_max", "range_min", "range_max"])
            store.append_plot_point("/cmd_vel", "linear.x",              time.monotonic(), 0.5 * _math.sin(t * 0.5))
            store.append_plot_point("/cmd_vel", "angular.z",             time.monotonic(), 0.3 * _math.cos(t * 0.4))
            store.append_plot_point("/odom",    "twist.twist.linear.x",  time.monotonic(), 0.5 * _math.sin(t * 0.5) * 0.9 + 0.02 * _math.sin(t * 3))
            t += 1.0
            time.sleep(1.0)

    threading.Thread(target=_run, daemon=True).start()


if __name__ == "__main__":
    main()
