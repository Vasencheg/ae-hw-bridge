"""IPC Client connecting to AE-HW-BRIDGE daemon over Unix domain socket.

Adheres to Interface Segregation Principle (ISP) and Dependency Inversion Principle (DIP).
Implements IReplClient, IConsoleReader, and IScenarioRegistry.
"""

import os
import sys
import time
import socket
import fcntl
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional, Dict
import threading

from ae_hw_bridge.core.interfaces import (
    IReplClient,
    IConsoleReader,
    ExecutionResult,
)
from ae_hw_bridge.core.port_utils import (
    get_default_control_port,
    get_default_uart_port,
    check_hw_puppet_connection,
)
from ae_hw_bridge.daemon.protocol import (
    DEFAULT_SOCKET_PATH,
    DEFAULT_SPAWN_LOCK_PATH,
    get_socket_path,
    get_spawn_lock_path,
    Request,
    Response,
    encode_request,
    decode_response,
    get_daemon_signature,
)

_logger = logging.getLogger(__name__)


class DaemonIpcClient(IReplClient, IConsoleReader):
    """Unified client communicating with the shared AE-HW-BRIDGE daemon."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH) -> None:
        self.socket_path = socket_path
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._buffer = bytearray()

    def _ensure_connected(self) -> None:
        """Establish connection to daemon socket if not already connected."""
        if self._sock is not None:
            return

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(60.0)
        sock.connect(self.socket_path)
        self._sock = sock
        self._buffer.clear()
        _logger.debug("Connected to daemon at %s", self.socket_path)

    def _close_sock(self) -> None:
        """Close connection socket and clear buffer."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._buffer.clear()

    def call(self, method: str, params: Optional[dict[str, Any]] = None, timeout: float = 60.0) -> Any:
        """Send an IPC request and wait for the response."""
        _logger.debug("Daemon IPC call: %s", method)
        req = Request(method=method, params=params or {})
        raw_req = encode_request(req)

        with self._lock:
            # Attempt send/recv with one retry on disconnection
            for attempt in range(2):
                try:
                    self._ensure_connected()
                    assert self._sock is not None
                    self._sock.settimeout(timeout)
                    self._sock.sendall(raw_req)

                    # Read until newline
                    while b"\n" not in self._buffer:
                        chunk = self._sock.recv(4096)
                        if not chunk:
                            raise ConnectionResetError("Daemon closed connection")
                        self._buffer.extend(chunk)

                    line, _, rest = self._buffer.partition(b"\n")
                    self._buffer = bytearray(rest)
                    resp = decode_response(line.decode("utf-8", errors="replace"))

                    if not resp.ok:
                        raise RuntimeError(f"Daemon error in '{method}': {resp.error}")
                    return resp.result
                except (ConnectionError, OSError):
                    self._close_sock()
                    if attempt == 1:
                        raise

    def ping(self) -> bool:
        """Check if daemon is reachable and responding."""
        try:
            res = self.call("ping", timeout=2.0)
            return isinstance(res, dict) and res.get("status") == "ok"
        except Exception:
            return False

    def get_version(self) -> Optional[str]:
        """Get version / code signature reported by the running daemon."""
        try:
            res = self.call("ping", timeout=2.0)
            if isinstance(res, dict) and res.get("status") == "ok":
                return res.get("version")
        except Exception:
            pass
        return None

    def is_compatible(self) -> bool:
        """Check if running daemon matches the current client version and code signature."""
        remote_sig = self.get_version()
        local_sig = get_daemon_signature()
        if remote_sig is None:
            # Old daemon running without version reporting
            return False
        return remote_sig == local_sig

    def shutdown(self) -> bool:
        """Request running daemon to gracefully terminate."""
        try:
            self.call("shutdown", timeout=2.0)
            self._close_sock()
            return True
        except Exception:
            self._close_sock()
            return False

    # --- IReplClient implementation ---

    def connect(self) -> None:
        self.call("repl_connect")

    def close(self) -> None:
        self._close_sock()

    def is_connected(self) -> bool:
        try:
            return bool(self.call("repl_is_connected", timeout=2.0))
        except Exception:
            return False

    def exec_code(self, code: str, timeout: float = 10.0) -> ExecutionResult:
        res = self.call("run_custom_code", {"code": code, "timeout": timeout}, timeout=timeout + 5.0)
        return ExecutionResult(
            ok=bool(res.get("ok", False)),
            stdout=str(res.get("stdout", "")),
            stderr=str(res.get("stderr", "")),
            result=res.get("result"),
        )

    # --- IConsoleReader implementation ---

    def start(self) -> None:
        """No-op on client; daemon manages background hardware monitoring."""
        pass

    def stop(self) -> None:
        """No-op on client."""
        pass

    def get_tail(self, lines: int = 50, grep: Optional[str] = None) -> list[str]:
        return self.get_lines(tail_lines=lines, grep=grep)

    def get_lines(
        self,
        tail_lines: Optional[int] = None,
        head_lines: Optional[int] = None,
        grep: Optional[str] = None,
    ) -> list[str]:
        res = self.call(
            "read_target_console",
            {"tail_lines": tail_lines, "head_lines": head_lines, "grep": grep},
            timeout=10.0,
        )
        return list(res) if isinstance(res, list) else []

    def get_total_lines_count(self) -> int:
        try:
            return int(self.call("get_total_lines_count", timeout=5.0))
        except Exception:
            return 0

    def get_lines_since(self, start_count: int, max_lines: int = 200) -> list[str]:
        try:
            res = self.call(
                "get_lines_since",
                {"start_count": start_count, "max_lines": max_lines},
                timeout=5.0,
            )
            return list(res) if isinstance(res, list) else []
        except Exception:
            return []

    def write(self, data: bytes) -> int:
        res = self.call("console_write", {"data_hex": data.hex()}, timeout=5.0)
        return int(res) if isinstance(res, int) else len(data)

    def clear(self) -> None:
        self.call("clear_target_console", timeout=5.0)

    def wait_for(
        self,
        pattern: str,
        timeout: float = 10.0,
        check_history: bool = True,
        history_lines: int = 100,
    ) -> Optional[str]:
        res = self.call(
            "wait_for_console_pattern",
            {
                "pattern": pattern,
                "timeout": timeout,
                "check_history": check_history,
                "history_lines": history_lines,
            },
            timeout=timeout + 5.0,
        )
        return res.get("line") if isinstance(res, dict) and res.get("matched") else None

    # --- Direct helper methods ---

    def send_target_command(
        self,
        command: str,
        wait_timeout: float = 5.0,
        idle_threshold: float = 0.3,
    ) -> str:
        """Execute command string on target console via daemon and collect response."""
        res = self.call(
            "send_target_command",
            {"command": command, "wait_timeout": wait_timeout, "idle_threshold": idle_threshold},
            timeout=wait_timeout + 5.0,
        )
        return str(res)

    def get_firmware_info(self) -> dict[str, Any]:
        """Query connected hw-puppet firmware info via daemon IPC."""
        res = self.call("get_firmware_info", timeout=5.0)
        return res if isinstance(res, dict) else {}

    def wait_for_console_pattern(
        self,
        pattern: str,
        timeout: float = 30.0,
        check_history: bool = True,
        history_lines: int = 100,
    ) -> dict[str, Any]:
        """Wait for pattern matching regex via daemon."""
        res = self.call(
            "wait_for_console_pattern",
            {
                "pattern": pattern,
                "timeout": timeout,
                "check_history": check_history,
                "history_lines": history_lines,
            },
            timeout=timeout + 5.0,
        )
        return dict(res) if isinstance(res, dict) else {"matched": False, "line": None, "pattern": pattern}


def ensure_daemon_running(
    control_port: Optional[str] = None,
    uart_port: Optional[str] = None,
    name: Optional[str] = None,
    socket_path: Optional[str] = None,
    spawn_lock_path: Optional[str] = None,
    idle_timeout: float = 30.0,
) -> DaemonIpcClient:
    """Check if daemon is running; if not or if outdated, spawn it in background and wait until responsive."""
    from ae_hw_bridge.daemon.server import stop_daemon

    eff_name = name or "base"
    eff_socket_path = socket_path or get_socket_path(eff_name)
    eff_spawn_lock = spawn_lock_path or get_spawn_lock_path(eff_name)
    eff_ctrl = control_port or get_default_control_port()
    eff_uart = uart_port or get_default_uart_port()

    client = DaemonIpcClient(socket_path=eff_socket_path)
    if client.ping():
        if client.is_compatible():
            return client
        _logger.warning(
            "Running daemon signature mismatch (remote: %s, local: %s). Restarting daemon...",
            client.get_version(),
            get_daemon_signature(),
        )
        client.shutdown()
        stop_daemon(socket_path=eff_socket_path)

    # Daemon not answering or was stopped; serialize spawn with flock
    lock_fd = os.open(eff_spawn_lock, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Re-check under lock
        if client.ping():
            if client.is_compatible():
                return client
            _logger.warning("Terminating incompatible daemon under lock...")
            client.shutdown()
            stop_daemon(socket_path=eff_socket_path)

        # Remove stale socket file if any
        if os.path.exists(eff_socket_path):
            try:
                os.unlink(eff_socket_path)
            except OSError:
                pass

        # Validate hardware presence before attempting spawn
        check_hw_puppet_connection(eff_ctrl, eff_uart)

        # Build spawn command
        daemon_cmd = [
            sys.executable,
            "-m",
            "ae_hw_bridge.daemon.server",
            "--name",
            eff_name,
            "--control-port",
            eff_ctrl,
            "--uart-port",
            eff_uart,
            "--socket-path",
            eff_socket_path,
            "--idle-timeout",
            str(idle_timeout),
        ]

        log_path = f"/tmp/ae-hw-bridge-{eff_name}-daemon.log"
        log_file = open(log_path, "a", encoding="utf-8")
        _logger.info("Spawning background hardware daemon: %s", " ".join(daemon_cmd))

        subprocess.Popen(
            daemon_cmd,
            start_new_session=True,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
        )
        log_file.close()

        # Wait up to 5 seconds for daemon to become ready
        deadline = time.time() + 5.0
        while time.time() < deadline:
            time.sleep(0.1)
            if client.ping():
                _logger.info("Daemon spawned and ready at %s", eff_socket_path)
                return client

        _logger.error("Daemon failed to respond within 5.0s")
        raise RuntimeError(f"AE-HW-BRIDGE daemon failed to start within 5.0s (check {log_path})")
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except Exception:
            pass
