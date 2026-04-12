"""
ros_bridge.py — ROS2 interface layer. Runs in its own thread.

Responsibilities:
  - Node discovery (ros2 node list equivalent via rclpy)
  - Topic discovery + frequency measurement + QoS inspection
  - Parameter get/set via rclpy async services
  - /rosout subscription for log capture
  - Numeric topic subscriptions for Plot panel
  - Driving ResourceMonitor at 1Hz
  - Writing all results into DataStore

The TUI never calls rclpy directly — it reads from DataStore and calls
RosBridge.set_param() / .pin_plot_topic() etc.

Threading model:
  - RosBridge.start() spawns a daemon thread
  - That thread runs rclpy.spin() in an executor
  - Timers drive periodic discovery and resource sampling
  - set_param() uses call_async() from within the spin thread via a queue
"""

import logging
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger("ros_bridge")


# ---------------------------------------------------------------------------
# Lazy rclpy import — lets the module load even without ROS installed,
# so we can unit-test DataStore and proc_utils independently.
# ---------------------------------------------------------------------------

def _try_import_rclpy():
    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import QoSProfile
        from rcl_interfaces.msg import Log as RosoutMsg
        from rcl_interfaces.srv import GetParameters, SetParameters, ListParameters
        from tf2_msgs.msg import TFMessage
        return rclpy, Node, SingleThreadedExecutor, QoSProfile, RosoutMsg,                GetParameters, SetParameters, ListParameters, TFMessage
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# QoS mismatch detection (best-effort heuristic from topic info)
# ---------------------------------------------------------------------------

_INCOMPATIBLE_PAIRS = {
    # (publisher_reliability, subscriber_reliability) → mismatch
    ("best_effort", "reliable"),
}


def _detect_qos_mismatch(pub_reliability: str, sub_reliability: str) -> bool:
    return (pub_reliability.lower(), sub_reliability.lower()) in _INCOMPATIBLE_PAIRS


# ---------------------------------------------------------------------------
# RosBridge
# ---------------------------------------------------------------------------

class RosBridge:
    """
    Manages all ROS2 interaction in a background thread.
    Safe to construct without ROS — it will mark itself disconnected.
    """

    def __init__(self, data_store, node_name: str = "rostui_bridge"):
        self._store = data_store
        self._node_name = node_name
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Queue for param set requests from TUI thread
        # Items: (node_name, param_name, value, result_callback)
        self._param_set_queue: queue.Queue = queue.Queue()

        # Tracked subscriptions for plot topics {topic: subscription}
        self._plot_subs: Dict[str, Any] = {}

        # Frequency tracking: {topic: deque of timestamps}
        self._topic_recv_times: Dict[str, list] = {}
        self._topic_last_msg: Dict[str, str] = {}

        # Topic info cache from last discovery
        self._known_topics: Set[str] = set()

        # rclpy objects — set in _spin_thread
        self._rclpy = None
        self._node = None
        self._executor = None

    # -----------------------------------------------------------------------
    # Public API (called from TUI thread)
    # -----------------------------------------------------------------------

    def start(self) -> None:
        """Start the ROS bridge in a background daemon thread."""
        self._thread = threading.Thread(
            target=self._spin_thread,
            name="ros_bridge",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the bridge to shut down and wait for thread exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def set_param(self,
                  node: str,
                  param: str,
                  value: Any,
                  on_done: Optional[callable] = None) -> None:
        """
        Request a parameter change. Non-blocking — queued for the spin thread.
        on_done(success: bool, error: str) called on completion (from spin thread).
        """
        self._param_set_queue.put((node, param, value, on_done))

    def pin_plot_topic(self, topic: str, field: str = "data") -> None:
        """Add a topic+field to the plot panel — bridge will subscribe to it."""
        self._store.add_plot_topic(topic, field)
        # Actual subscription happens in _refresh_plot_subscriptions()
        # which runs on the next discovery cycle inside the spin thread.

    def unpin_plot_topic(self, topic: str, field: Optional[str] = None) -> None:
        self._store.remove_plot_topic(topic, field)

    def list_services(self) -> None:
        """Refresh service list — queued for spin thread."""
        self._param_set_queue.put(("__list_services__", None, None, None))

    def publish_topic(self, topic: str, msg_type_str: str,
                      field_values: dict, on_done=None) -> None:
        """Publish a single message to a topic."""
        self._param_set_queue.put(
            ("__pub__", topic, (msg_type_str, field_values), on_done)
        )

    def call_service(self, service: str, srv_type_str: str,
                     field_values: dict, on_done=None) -> None:
        """Call a service with given request fields."""
        self._param_set_queue.put(
            ("__srv__", service, (srv_type_str, field_values), on_done)
        )

    def get_msg_fields(self, msg_type_str: str) -> List[dict]:
        """Synchronously introspect message fields. Returns list of field dicts."""
        msg_class = self._import_msg_type(msg_type_str)
        if msg_class is None:
            return []
        return self._describe_fields(msg_class())

    def get_srv_fields(self, srv_type_str: str) -> List[dict]:
        """Synchronously introspect service request fields."""
        srv_class = self._import_srv_type(srv_type_str)
        if srv_class is None:
            return []
        try:
            return self._describe_fields(srv_class.Request())
        except Exception:
            return []

    def fetch_params(self, ros_node: str) -> None:
        """
        Request a param refresh for a node.
        Queued so it runs in the spin thread safely.
        """
        self._param_set_queue.put(("__fetch__", ros_node, None, None))

    # -----------------------------------------------------------------------
    # Spin thread — everything below runs in the background thread only
    # -----------------------------------------------------------------------

    def _spin_thread(self) -> None:
        imports = _try_import_rclpy()
        if imports is None:
            log.error("rclpy not available — running in disconnected mode")
            self._store.set_ros_connected(False)
            self._run_disconnected_loop()
            return

        (rclpy, Node, SingleThreadedExecutor,
         QoSProfile, RosoutMsg,
         GetParameters, SetParameters, ListParameters,
         TFMessage) = imports

        self._rclpy = rclpy

        try:
            rclpy.init()
            self._node = rclpy.create_node(self._node_name)
            self._executor = SingleThreadedExecutor()
            self._executor.add_node(self._node)
            self._store.set_ros_connected(True)
            log.info("ROS bridge connected")
        except Exception as e:
            log.error(f"rclpy init failed: {e}")
            self._store.set_ros_connected(False)
            self._run_disconnected_loop()
            return

        # Subscribe to /rosout
        try:
            self._node.create_subscription(
                RosoutMsg,
                "/rosout",
                self._rosout_cb,
                10,
            )
        except Exception as e:
            log.warning(f"Could not subscribe to /rosout: {e}")

        # Subscribe to /tf and /tf_static
        try:
            self._node.create_subscription(
                TFMessage, "/tf",
                lambda msg: self._on_tf_msg(msg, is_static=False), 100)
            self._node.create_subscription(
                TFMessage, "/tf_static",
                lambda msg: self._on_tf_msg(msg, is_static=True), 100)
        except Exception as e:
            log.warning(f"Could not subscribe to /tf: {e}")

        # Timers
        self._node.create_timer(1.0,  self._tick_resources)
        self._node.create_timer(2.0,  self._tick_discovery)
        self._node.create_timer(0.1,  self._tick_param_queue)

        # Lazy import ResourceMonitor here so it's in the right thread
        from .proc_utils import ResourceMonitor
        self._resource_monitor = ResourceMonitor()

        # Initial discovery immediately
        self._tick_discovery()

        # Spin until stop requested
        while not self._stop_event.is_set():
            self._executor.spin_once(timeout_sec=0.05)

        # Cleanup
        try:
            self._node.destroy_node()
            rclpy.shutdown()
        except Exception:
            pass

    def _run_disconnected_loop(self) -> None:
        """Fallback loop when rclpy isn't available — keeps thread alive."""
        while not self._stop_event.is_set():
            time.sleep(0.5)

    # -----------------------------------------------------------------------
    # Timer callbacks (run in spin thread)
    # -----------------------------------------------------------------------

    def _tick_resources(self) -> None:
        """1Hz — sample CPU/memory for all known nodes."""
        try:
            node_names = self._get_node_names()
            self._resource_monitor.update(node_names)
            all_nodes = self._resource_monitor.all_nodes()
            self._store.update_node_resources(all_nodes, self._resource_monitor.system)
            # Feed node plot history
            now = time.monotonic()
            for name, res in all_nodes.items():
                self._store.append_node_resource_point(
                    name, now, res.cpu_percent, res.memory_mb
                )
        except Exception as e:
            log.debug(f"Resource tick error: {e}")

    def _tick_discovery(self) -> None:
        """2Hz — discover topics, measure frequencies, detect QoS issues."""
        try:
            self._discover_topics()
            self._do_list_services()
            self._refresh_plot_subscriptions()
        except Exception as e:
            log.debug(f"Discovery tick error: {e}")

    def _tick_param_queue(self) -> None:
        """10Hz — drain the param set/fetch queue."""
        try:
            while True:
                item = self._param_set_queue.get_nowait()
                action, node, value, cb = item

                if action == "__fetch__":
                    self._do_fetch_params(node)
                elif action == "__list_services__":
                    self._do_list_services()
                elif action == "__pub__":
                    self._do_publish(node, value[0], value[1], cb)
                elif action == "__srv__":
                    self._do_call_service(node, value[0], value[1], cb)
                else:
                    self._do_set_param(action, node, value, cb)

        except queue.Empty:
            pass
        except Exception as e:
            log.debug(f"Param queue error: {e}")

    # -----------------------------------------------------------------------
    # Node discovery
    # -----------------------------------------------------------------------

    def _get_node_names(self) -> List[str]:
        """
        Get all running node names via rclpy.
        Returns fully-qualified names like /controller_server.
        """
        if self._node is None:
            return []
        try:
            names_and_ns = self._node.get_node_names_and_namespaces()
            result = []
            for name, ns in names_and_ns:
                if ns == "/":
                    result.append(f"/{name}")
                else:
                    result.append(f"{ns}/{name}")
            # Filter out ourselves
            return [n for n in result if self._node_name not in n]
        except Exception:
            return []

    # -----------------------------------------------------------------------
    # Topic discovery
    # -----------------------------------------------------------------------

    def _discover_topics(self) -> None:
        if self._node is None:
            return

        try:
            topic_list = self._node.get_topic_names_and_types()
        except Exception:
            return

        from .data_store import TopicSnapshot

        updated: Dict[str, TopicSnapshot] = {}

        for topic_name, type_list in topic_list:
            msg_type = type_list[0] if type_list else "unknown"

            # Publisher / subscriber counts
            try:
                pub_info = self._node.get_publishers_info_by_topic(topic_name)
                sub_info = self._node.get_subscriptions_info_by_topic(topic_name)
            except Exception:
                pub_info, sub_info = [], []

            pub_count = len(pub_info)
            sub_count = len(sub_info)

            # QoS from first publisher
            pub_reliability = "unknown"
            pub_durability  = "unknown"
            sub_reliability = "unknown"
            qos_mismatch    = False

            if pub_info:
                qos = pub_info[0].qos_profile
                pub_reliability = str(qos.reliability).split(".")[-1].lower()
                pub_durability  = str(qos.durability).split(".")[-1].lower()

            if sub_info:
                qos = sub_info[0].qos_profile
                sub_reliability = str(qos.reliability).split(".")[-1].lower()
                qos_mismatch = _detect_qos_mismatch(pub_reliability, sub_reliability)

            # Frequency from rolling window
            freq = self._compute_frequency(topic_name)

            # Last message repr
            last_msg = self._topic_last_msg.get(topic_name, "")

            updated[topic_name] = TopicSnapshot(
                name=topic_name,
                msg_type=msg_type,
                pub_count=pub_count,
                sub_count=sub_count,
                frequency_hz=freq,
                qos_reliability=pub_reliability,
                qos_durability=pub_durability,
                qos_mismatch=qos_mismatch,
                last_msg_repr=last_msg,
            )

        self._store.update_topics(updated)

    def _compute_frequency(self, topic: str) -> float:
        """
        Estimate publish frequency from rolling receive timestamps.
        Window: last 5 seconds.
        """
        now = time.monotonic()
        times = self._topic_recv_times.get(topic, [])
        # Trim old entries
        recent = [t for t in times if now - t < 5.0]
        self._topic_recv_times[topic] = recent

        if len(recent) < 2:
            return 0.0
        span = recent[-1] - recent[0]
        return (len(recent) - 1) / span if span > 0 else 0.0

    def _on_topic_msg(self, topic: str, msg: Any) -> None:
        """Generic message callback for frequency tracking and plot data."""
        now = time.monotonic()

        # Frequency tracking
        if topic not in self._topic_recv_times:
            self._topic_recv_times[topic] = []
        self._topic_recv_times[topic].append(now)

        # Last message repr (truncated)
        try:
            self._topic_last_msg[topic] = str(msg)[:200]
        except Exception:
            pass

        # Populate plottable field list on first message (for the field picker UI)
        if topic not in self._store._topic_fields:
            numeric_fields = [
                f["path"] for f in self._describe_fields(msg)
                if f["type"] in ("int", "float")
            ]
            if numeric_fields:
                self._store.update_topic_fields(topic, numeric_fields)

        # Fan out to every pinned "topic::field" key for this topic
        pinned_keys = self._store.snapshot_plot_topics()
        for key in pinned_keys:
            if "::" in key:
                t, field = key.split("::", 1)
            else:
                t, field = key, "data"
            if t != topic:
                continue
            value = self._extract_field(msg, field)
            if value is not None:
                self._store.append_plot_point(key, now, value)

    def _extract_numeric(self, msg: Any) -> Optional[float]:
        """
        Best-effort extraction of a float from a ROS message.
        Handles: std_msgs/Float*, geometry_msgs/Twist (linear.x), scalar data fields.
        Returns None if no numeric value found.
        """
        # std_msgs Float32/Float64
        if hasattr(msg, "data") and isinstance(msg.data, (int, float)):
            return float(msg.data)

        # geometry_msgs/Twist — use linear.x as primary signal
        if hasattr(msg, "linear") and hasattr(msg.linear, "x"):
            return float(msg.linear.x)

        # nav_msgs/Odometry — linear velocity
        if hasattr(msg, "twist") and hasattr(msg.twist, "twist"):
            if hasattr(msg.twist.twist, "linear"):
                return float(msg.twist.twist.linear.x)

        # First float field found
        for attr in vars(msg) if hasattr(msg, "__dict__") else []:
            val = getattr(msg, attr, None)
            if isinstance(val, float):
                return val

        return None

    def _extract_field(self, msg: Any, field: str) -> Optional[float]:
        """
        Extract a numeric value from *msg* by dot-separated *field* path.
        Falls back to ``_extract_numeric`` when field is "data" or the path
        cannot be resolved.

        Examples::
            _extract_field(msg, "data")           # std_msgs/Float64
            _extract_field(msg, "linear.x")       # geometry_msgs/Twist
            _extract_field(msg, "position.x")     # geometry_msgs/Point
        """
        if field == "data":
            return self._extract_numeric(msg)
        try:
            obj = msg
            for part in field.split("."):
                obj = getattr(obj, part)
            if isinstance(obj, (int, float)) and not isinstance(obj, bool):
                return float(obj)
        except Exception:
            pass
        # Path didn't resolve — fall back
        return self._extract_numeric(msg)

    # -----------------------------------------------------------------------
    # Plot subscriptions
    # -----------------------------------------------------------------------

    def _refresh_plot_subscriptions(self) -> None:
        """
        Sync rclpy subscriptions with the set of pinned plot topics.
        Keys in the store are "topic::field"; we subscribe once per unique
        topic (the rclpy subscription covers all fields of that topic).
        """
        if self._node is None:
            return

        # Derive the set of unique ROS topic names from pinned keys
        pinned_keys = set(self._store.snapshot_plot_topics())
        pinned_topics = {k.split("::")[0] if "::" in k else k for k in pinned_keys}
        subscribed = set(self._plot_subs.keys())

        # Topics to add
        for topic in pinned_topics - subscribed:
            self._create_plot_subscription(topic)

        # Topics to remove — only if no pinned key references this topic any more
        for topic in subscribed - pinned_topics:
            try:
                self._node.destroy_subscription(self._plot_subs.pop(topic))
            except Exception:
                pass

    def _create_plot_subscription(self, topic: str) -> None:
        """
        Create a subscription for a plot topic.
        We need the message type — look it up from the topic list.
        """
        if self._node is None:
            return

        try:
            topic_types = dict(self._node.get_topic_names_and_types())
            type_list = topic_types.get(topic, [])
            if not type_list:
                log.warning(f"Cannot subscribe to {topic}: unknown type")
                return

            msg_type_str = type_list[0]  # e.g. "std_msgs/msg/Float64"
            msg_class = self._import_msg_type(msg_type_str)
            if msg_class is None:
                log.warning(f"Cannot import message type {msg_type_str}")
                return

            sub = self._node.create_subscription(
                msg_class,
                topic,
                lambda msg, t=topic: self._on_topic_msg(t, msg),
                10,
            )
            self._plot_subs[topic] = sub
            log.info(f"Subscribed to plot topic: {topic}")

        except Exception as e:
            log.warning(f"Failed to subscribe to {topic}: {e}")

    def _import_msg_type(self, type_str: str) -> Optional[Any]:
        """
        Dynamically import a ROS2 message type from its string representation.
        e.g. "std_msgs/msg/Float64" → std_msgs.msg.Float64
        """
        import importlib
        try:
            # type_str format: "package/msg/TypeName"
            parts = type_str.split("/")
            if len(parts) != 3:
                return None
            pkg, _, typename = parts
            module = importlib.import_module(f"{pkg}.msg")
            return getattr(module, typename)
        except (ImportError, AttributeError):
            return None

    # -----------------------------------------------------------------------
    # Parameter get / set
    # -----------------------------------------------------------------------

    def _do_fetch_params(self, ros_node: str) -> None:
        """Fetch all parameters for a node via CLI."""
        import yaml
        from .data_store import ParamSnapshot

        # Try `ros2 param dump` (uses existing daemon — much faster than --no-daemon)
        try:
            result = subprocess.run(
                ["ros2", "param", "dump", ros_node],
                capture_output=True, text=True, timeout=8.0,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = yaml.safe_load(result.stdout)
                if isinstance(data, dict):
                    # yaml key may be "node_name", "/node_name", or "ns/node_name"
                    node_bare = ros_node.lstrip("/")
                    params_dict = {}
                    for key in data:
                        key_bare = str(key).lstrip("/")
                        if key_bare == node_bare or key_bare.endswith("/" + node_bare.split("/")[-1]):
                            params_dict = data[key].get("ros__parameters", {})
                            break
                    if not params_dict and data:
                        # Single-entry yaml — just take the first one
                        first = next(iter(data.values()))
                        if isinstance(first, dict):
                            params_dict = first.get("ros__parameters", {})

                    if params_dict:
                        snapshots: Dict[str, ParamSnapshot] = {}
                        for pname, pval in params_dict.items():
                            snapshots[pname] = ParamSnapshot(
                                node=ros_node,
                                name=pname,
                                value=pval,
                                type_name=type(pval).__name__,
                            )
                        self._store.update_params(ros_node, snapshots)
                        return

        except Exception as e:
            log.debug(f"param dump error for {ros_node}: {e}")

        # Fallback: ros2 param list + get each param individually
        try:
            list_result = subprocess.run(
                ["ros2", "param", "list", ros_node],
                capture_output=True, text=True, timeout=5.0,
            )
            if list_result.returncode != 0:
                log.debug(f"param list failed for {ros_node}: {list_result.stderr}")
                return

            param_names = [l.strip() for l in list_result.stdout.splitlines()
                           if l.strip() and not l.strip().startswith("/")]

            snapshots = {}
            for pname in param_names:
                try:
                    get_result = subprocess.run(
                        ["ros2", "param", "get", ros_node, pname],
                        capture_output=True, text=True, timeout=3.0,
                    )
                    if get_result.returncode == 0:
                        # Output: "Integer value is: 42" / "String value is: foo"
                        line = get_result.stdout.strip()
                        pval: Any = line.split(":", 1)[-1].strip() if ":" in line else line
                        # Try to coerce to native type
                        for coerce in (int, float):
                            try:
                                pval = coerce(pval)
                                break
                            except (ValueError, TypeError):
                                pass
                        if isinstance(pval, str) and pval.lower() in ("true", "false"):
                            pval = pval.lower() == "true"
                        snapshots[pname] = ParamSnapshot(
                            node=ros_node, name=pname,
                            value=pval, type_name=type(pval).__name__,
                        )
                except Exception:
                    pass

            if snapshots:
                self._store.update_params(ros_node, snapshots)

        except Exception as e:
            log.debug(f"fetch_params fallback error for {ros_node}: {e}")

    def _do_set_param(self,
                      ros_node: str,
                      param: str,
                      value: Any,
                      on_done: Optional[callable]) -> None:
        """
        Set a parameter via ros2 param set CLI.
        Records a change marker in the store for plot overlay.
        """
        # Get old value for the marker
        old_val = None
        existing = {p.name: p.value
                    for p in self._store.snapshot_params(ros_node)}
        old_val = existing.get(param)

        try:
            value_str = str(value)
            result = subprocess.run(
                ["ros2", "param", "set", ros_node, param, value_str],
                capture_output=True, text=True, timeout=5.0,
            )
            success = result.returncode == 0
            error_msg = result.stderr.strip() if not success else ""

        except subprocess.TimeoutExpired:
            success = False
            error_msg = "timeout"
        except Exception as e:
            success = False
            error_msg = str(e)

        if success:
            # Record marker for plot overlay
            from .data_store import ParamChangeMarker
            self._store.record_param_change(ParamChangeMarker(
                timestamp=time.monotonic(),
                node=ros_node,
                param=param,
                old_value=old_val,
                new_value=value,
            ))
            # Refresh params for this node
            self._do_fetch_params(ros_node)
            log.info(f"Set {ros_node} {param} = {value}")

        if on_done:
            try:
                on_done(success, error_msg)
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # /rosout callback
    # -----------------------------------------------------------------------

    def _do_list_services(self) -> None:
        if self._node is None:
            return
        try:
            from .data_store import ServiceSnapshot
            svc_list = self._node.get_service_names_and_types()
            services = {
                name: ServiceSnapshot(name=name, srv_type=types[0] if types else "unknown")
                for name, types in svc_list
            }
            self._store.update_services(services)
        except Exception as e:
            log.debug(f"Service discovery error: {e}")

    def _do_publish(self, topic: str, msg_type_str: str,
                    field_values: dict, on_done) -> None:
        try:
            msg_class = self._import_msg_type(msg_type_str)
            if msg_class is None:
                if on_done: on_done(False, f"Unknown type: {msg_type_str}")
                return
            msg = msg_class()
            self._set_msg_fields(msg, field_values)
            pub = self._node.create_publisher(msg_class, topic, 1)
            pub.publish(msg)
            self._node.destroy_publisher(pub)
            if on_done: on_done(True, "")
        except Exception as e:
            if on_done: on_done(False, str(e))

    def _do_call_service(self, service: str, srv_type_str: str,
                         field_values: dict, on_done) -> None:
        try:
            srv_class = self._import_srv_type(srv_type_str)
            if srv_class is None:
                if on_done: on_done(False, f"Unknown service type: {srv_type_str}", None)
                return
            req = srv_class.Request()
            self._set_msg_fields(req, field_values)
            cli = self._node.create_client(srv_class, service)
            if not cli.wait_for_service(timeout_sec=3.0):
                if on_done: on_done(False, "Service not available", None)
                self._node.destroy_client(cli)
                return
            future = cli.call_async(req)
            # Spin until done (blocking but in spin thread so safe)
            import rclpy
            rclpy.spin_until_future_complete(self._node, future, timeout_sec=5.0)
            self._node.destroy_client(cli)
            if future.done():
                result = future.result()
                if on_done: on_done(True, "", str(result))
            else:
                if on_done: on_done(False, "Timeout", None)
        except Exception as e:
            if on_done: on_done(False, str(e), None)

    def _import_srv_type(self, type_str: str) -> Optional[Any]:
        import importlib
        try:
            parts = type_str.split("/")
            if len(parts) != 3:
                return None
            pkg, _, typename = parts
            module = importlib.import_module(f"{pkg}.srv")
            return getattr(module, typename)
        except (ImportError, AttributeError):
            return None

    def _describe_fields(self, obj: Any, prefix: str = "", depth: int = 0) -> List[dict]:
        """Return list of {path, type, default} for all leaf fields of a msg."""
        if depth > 4:
            return []
        fields = []
        slots = getattr(obj, "__slots__", None) or []
        for slot in slots:
            attr = slot.lstrip("_")
            try:
                val = getattr(obj, attr, None)
                if val is None:
                    val = getattr(obj, slot, None)
            except Exception:
                continue
            if val is None:
                continue
            path = f"{prefix}.{attr}" if prefix else attr
            if isinstance(val, bool):
                fields.append({"path": path, "type": "bool", "default": val})
            elif isinstance(val, int):
                fields.append({"path": path, "type": "int", "default": val})
            elif isinstance(val, float):
                fields.append({"path": path, "type": "float", "default": val})
            elif isinstance(val, str):
                fields.append({"path": path, "type": "str", "default": val})
            elif hasattr(val, "__slots__"):
                fields.extend(self._describe_fields(val, path, depth + 1))
        return fields

    def _set_msg_fields(self, msg: Any, field_values: dict) -> None:
        """Set fields on a message by dot-path dict."""
        for path, raw_val in field_values.items():
            parts = path.split(".")
            obj = msg
            try:
                for part in parts[:-1]:
                    obj = getattr(obj, part)
                leaf = parts[-1]
                current = getattr(obj, leaf)
                if isinstance(current, bool):
                    setattr(obj, leaf, str(raw_val).lower() in ("true", "1", "yes"))
                elif isinstance(current, int):
                    setattr(obj, leaf, int(float(raw_val)))
                elif isinstance(current, float):
                    setattr(obj, leaf, float(raw_val))
                else:
                    setattr(obj, leaf, str(raw_val))
            except Exception as e:
                log.debug(f"set_msg_fields error on {path}: {e}")

    def _on_tf_msg(self, msg: Any, is_static: bool) -> None:
        """Handle /tf and /tf_static messages — update staleness tracker."""
        now = time.monotonic()
        try:
            for transform in msg.transforms:
                parent = transform.header.frame_id
                child  = transform.child_frame_id
                self._store.update_tf(parent, child, now, is_static=is_static)
        except Exception as e:
            log.debug(f"TF callback error: {e}")

    def _rosout_cb(self, msg: Any) -> None:
        try:
            level_map = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "FATAL"}
            level = level_map.get(msg.level, "?")
            line = f"[{level}] [{msg.name}] {msg.msg}"
            self._store.append_log_line(line)
        except Exception:
            pass
