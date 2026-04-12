"""
panels/terminal.py — Embedded terminal (Tab 7).

Spawns a real shell in a pty so interactive commands work.
Output is streamed into a scrolling Static widget.
Input bar sends lines to the shell's stdin.

Features:
  - Full pty — interactive commands, colours, tab completion
  - Ctrl+C sends SIGINT to the shell process
  - Ctrl+L clears the display buffer (doesn't affect shell state)
  - Auto-scroll with End key to re-tail
  - Shell history via ↑/↓ in the input bar
"""

import fcntl
import os
import pty
import select
import signal
import struct
import termios
import threading
from collections import deque
from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Input, Static, RichLog
from rich.text import Text


_SHELL = os.environ.get("SHELL", "/bin/bash")
_MAX_LINES = 1000
_READ_SIZE  = 4096


# ---------------------------------------------------------------------------
# ANSI strip — Rich handles most ANSI but some sequences confuse Static
# ---------------------------------------------------------------------------

import re as _re
_ANSI_ESCAPE = _re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilnprsu]'
                            r'|\x1b\][^\x07]*\x07'
                            r'|\x1b[()][AB012]'
                            r'|\r')


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub('', text)


# ---------------------------------------------------------------------------
# PtyProcess — manages the shell subprocess
# ---------------------------------------------------------------------------

class PtyProcess:
    def __init__(self, cols: int = 220, rows: int = 50):
        self._cols = cols
        self._rows = rows
        self._master_fd: Optional[int] = None
        self._pid: Optional[int] = None
        self._alive = False

    def start(self) -> None:
        self._pid, self._master_fd = pty.fork()
        if self._pid == 0:
            # Child — set terminal size then exec shell
            self._set_winsize(self._master_fd if self._master_fd else 0,
                              self._cols, self._rows)
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["COLUMNS"] = str(self._cols)
            env["LINES"] = str(self._rows)
            os.execvpe(_SHELL, [_SHELL], env)
        else:
            # Parent
            self._alive = True
            self._set_winsize(self._master_fd, self._cols, self._rows)

    def _set_winsize(self, fd: int, cols: int, rows: int) -> None:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def write(self, data: str) -> None:
        if self._master_fd and self._alive:
            try:
                os.write(self._master_fd, data.encode())
            except OSError:
                self._alive = False

    def send_signal(self, sig: int) -> None:
        if self._pid and self._alive:
            try:
                os.kill(self._pid, sig)
            except ProcessLookupError:
                self._alive = False

    def read_available(self, timeout: float = 0.05) -> str:
        if not self._master_fd or not self._alive:
            return ""
        try:
            r, _, _ = select.select([self._master_fd], [], [], timeout)
            if r:
                data = os.read(self._master_fd, _READ_SIZE)
                return data.decode("utf-8", errors="replace")
        except OSError:
            self._alive = False
        return ""

    def stop(self) -> None:
        self._alive = False
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGTERM)
                os.waitpid(self._pid, os.WNOHANG)
            except (ProcessLookupError, ChildProcessError):
                pass
        if self._master_fd:
            try:
                os.close(self._master_fd)
            except OSError:
                pass

    @property
    def alive(self) -> bool:
        if not self._alive or self._pid is None:
            return False
        try:
            result = os.waitpid(self._pid, os.WNOHANG)
            if result[0] != 0:
                self._alive = False
        except ChildProcessError:
            self._alive = False
        return self._alive


# ---------------------------------------------------------------------------
# TerminalOutput — scrollable output buffer
# ---------------------------------------------------------------------------

class TerminalOutput(Widget):
    DEFAULT_CSS = """
    TerminalOutput {
        height: 1fr;
        background: $surface-darken-2;
        padding: 0;
    }
    #term_log {
        height: 1fr;
        background: $surface-darken-2;
        padding: 0 1;
        scrollbar-gutter: stable;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._auto_scroll = True
        self._pending = ""

    def compose(self) -> ComposeResult:
        yield RichLog(id="term_log", markup=False, highlight=False,
                      auto_scroll=True, wrap=False)

    def feed(self, raw: str) -> None:
        """Append raw pty output, split on newlines."""
        combined = self._pending + raw
        parts = combined.split("\n")
        self._pending = parts[-1]
        log = self.query_one("#term_log", RichLog)
        for line in parts[:-1]:
            cleaned = _strip_ansi(line)
            log.write(cleaned)

    def clear(self) -> None:
        self._pending = ""
        try:
            self.query_one("#term_log", RichLog).clear()
        except NoMatches:
            pass

    def resume_scroll(self) -> None:
        try:
            log = self.query_one("#term_log", RichLog)
            log.auto_scroll = True
            log.scroll_end(animate=False)
        except NoMatches:
            pass


# ---------------------------------------------------------------------------
# TerminalPanel
# ---------------------------------------------------------------------------

class TerminalPanel(Widget):

    BINDINGS = [
        Binding("ctrl+c",      "send_interrupt", "Interrupt",   show=True),
        Binding("ctrl+l",      "clear_screen",   "Clear",       show=True),
        Binding("end",         "scroll_end",      "Tail",        show=False),
    ]

    DEFAULT_CSS = """
    TerminalPanel { height: 1fr; }
    #term_header {
        height: 1;
        padding: 0 1;
        background: $surface-darken-2;
        color: green;
    }
    #term_input_row {
        height: 3;
        border-top: solid $accent;
        padding: 0 1;
        background: $surface-darken-1;
    }
    #term_prompt {
        width: auto;
        content-align: left middle;
        color: green;
        padding: 0 1 0 0;
    }
    #term_input { width: 1fr; height: 3; }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pty: Optional[PtyProcess] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._history: List[str] = []
        self._history_idx = -1
        self._running = False

    def compose(self) -> ComposeResult:
        yield Static(f" ⌨  Terminal  {_SHELL}", id="term_header", markup=False)
        yield TerminalOutput(id="term_output")
        from textual.containers import Horizontal
        with Horizontal(id="term_input_row"):
            yield Static("❯", id="term_prompt")
            yield Input(placeholder="enter command…", id="term_input")

    def on_mount(self) -> None:
        self._start_shell()

    def on_unmount(self) -> None:
        self._running = False  # signal thread to exit before killing pty
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        self._stop_shell()

    # -----------------------------------------------------------------------
    # Shell lifecycle
    # -----------------------------------------------------------------------

    def _start_shell(self) -> None:
        self._pty = PtyProcess(cols=200, rows=40)
        try:
            self._pty.start()
        except Exception as e:
            self._feed(f"[failed to start shell: {e}]\n")
            return
        self._running = True
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True
        )
        self._reader_thread.start()
        self._update_header("running")

    def _stop_shell(self) -> None:
        if self._pty:
            self._pty.stop()

    def _read_loop(self) -> None:
        """Background thread — reads pty output and schedules UI update."""
        while self._running and self._pty and self._pty.alive:
            data = self._pty.read_available(timeout=0.05)
            if data:
                try:
                    self.app.call_from_thread(self._feed, data)
                except Exception:
                    break  # app is shutting down — exit quietly
        # Only call back into the app if we're still running (not a shutdown exit)
        if self._running:
            try:
                self.app.call_from_thread(self._update_header, "exited")
            except Exception:
                pass

    def _feed(self, data: str) -> None:
        try:
            self.query_one("#term_output", TerminalOutput).feed(data)
        except NoMatches:
            pass

    def _update_header(self, status: str) -> None:
        icon = "●" if status == "running" else "✗"
        try:
            self.query_one("#term_header", Static).update(
                f" ⌨  Terminal  {_SHELL}  {icon} {status}"
            )
        except NoMatches:
            pass

    # -----------------------------------------------------------------------
    # Input handling
    # -----------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "term_input":
            return
        cmd = event.value
        event.input.value = ""
        self._history_idx = -1

        if cmd:
            self._history.append(cmd)

        if not self._pty or not self._pty.alive:
            # Shell died — restart
            self._feed("\n[shell exited — restarting…]\n")
            self._start_shell()
            return

        self._pty.write(cmd + "\n")

    def on_key(self, event) -> None:
        # Only handle history nav when the input bar is focused
        try:
            inp = self.query_one("#term_input", Input)
            if self.app.focused is not inp:
                return
        except NoMatches:
            return

        if event.key == "up" and self._history:
            self._history_idx = min(self._history_idx + 1, len(self._history) - 1)
            self._set_input(self._history[-(self._history_idx + 1)])
            event.stop()
        elif event.key == "down":
            if self._history_idx > 0:
                self._history_idx -= 1
                self._set_input(self._history[-(self._history_idx + 1)])
            else:
                self._history_idx = -1
                self._set_input("")
            event.stop()

    def _set_input(self, value: str) -> None:
        try:
            inp = self.query_one("#term_input", Input)
            inp.value = value
            inp.cursor_position = len(value)
        except NoMatches:
            pass

    # -----------------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------------

    def action_send_interrupt(self) -> None:
        if self._pty and self._pty.alive:
            self._pty.send_signal(signal.SIGINT)
            self._feed("^C\n")

    def action_clear_screen(self) -> None:
        try:
            self.query_one("#term_output", TerminalOutput).clear()
        except NoMatches:
            pass

    def action_scroll_end(self) -> None:
        try:
            self.query_one("#term_output", TerminalOutput).resume_scroll()
        except NoMatches:
            pass
