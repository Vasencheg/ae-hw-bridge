"""Unix domain socket daemon server managing physical hardware access and multiplexing requests."""

import os
import sys
import time
import socket
import select
import signal
import fcntl
import argparse
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, Dict, Callable

from ae_hw_bridge import __version__
from ae_hw_bridge.core.interfaces import (
    IReplClient,
    IConsoleReader,
)
from ae_hw_bridge.core.repl_client import ReplClient
from ae_hw_bridge.core.console_reader import ConsoleReader
from ae_hw_bridge.core.port_utils import (
    get_default_control_port,
    get_default_uart_port,
    check_hw_puppet_connection,
    check_firmware_handshake,
)
from ae_hw_bridge.daemon.protocol import (
    DEFAULT_SOCKET_PATH,
    DEFAULT_LOCK_PATH,
    get_socket_path,
    get_lock_path,
    Request,
    Response,
    decode_request,
    encode_response,
    get_daemon_signature,
)

_logger = logging.getLogger(__name__)


class DaemonServer:
    """Multi-client Unix domain socket daemon holding singleton hardware connections."""

    def __init__(
        self,
        repl: IReplClient,
        console: IConsoleReader,
        socket_path: str = DEFAULT_SOCKET_PATH,
        lock_path: str = DEFAULT_LOCK_PATH,
        idle_timeout: Optional[float] = 30.0,
        firmware_info: Optional[dict[str, Any]] = None,
    ) -> None:
        self.repl = repl
        self.console = console
        self.socket_path = socket_path
        self.lock_path = lock_path
        self.idle_timeout = idle_timeout
        self._firmware_info = firmware_info
        self._lock_fd: Optional[int] = None
        self._server_sock: Optional[socket.socket] = None
        self._running = False
        self._start_time = time.time()
        self._last_client_disconnect_time: Optional[float] = time.time()
        self._thread: Optional[threading.Thread] = None
        self._clients: set[socket.socket] = set()
        self._client_send_locks: Dict[socket.socket, threading.Lock] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="DaemonWorker")
        self._repl_lock = threading.Lock()
        self._cmd_lock = threading.Lock()

        # Command dispatch map
        self._handlers: Dict[str, Callable[[dict[str, Any]], Any]] = {
            "ping": self._handle_ping,
            "run_custom_code": self._handle_run_custom_code,
            "clear_target_console": self._handle_clear_target_console,
            "read_target_console": self._handle_read_target_console,
            "send_target_command": self._handle_send_target_command,
            "wait_for_console_pattern": self._handle_wait_for_console_pattern,
            "get_total_lines_count": self._handle_get_total_lines_count,
            "get_lines_since": self._handle_get_lines_since,
            "console_write": self._handle_console_write,
            "repl_is_connected": self._handle_repl_is_connected,
            "repl_connect": self._handle_repl_connect,
            "repl_close": self._handle_repl_close,
            "get_firmware_info": self._handle_get_firmware_info,
            "shutdown": self._handle_shutdown,
        }

    def is_running(self) -> bool:
        """Return True if daemon server is actively running."""
        return self._running

    def _acquire_daemon_lock(self) -> None:
        """Acquire exclusive flock for daemon, terminating any stale daemon."""
        self._lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            # Another process holds lock; check PID and terminate if stale
            try:
                with open(self.lock_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content.isdigit():
                    old_pid = int(content)
                    if old_pid != os.getpid():
                        try:
                            os.kill(old_pid, signal.SIGTERM)
                            time.sleep(0.5)
                            # Check if still alive, send SIGKILL
                            try:
                                os.kill(old_pid, 0)
                                os.kill(old_pid, signal.SIGKILL)
                                time.sleep(0.2)
                            except OSError:
                                pass
                        except ProcessLookupError:
                            pass
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception as e:
                raise RuntimeError(f"Cannot acquire daemon lock at {self.lock_path}: {e}") from e

        os.ftruncate(self._lock_fd, 0)
        os.write(self._lock_fd, f"{os.getpid()}\n".encode("utf-8"))

    def start(self) -> None:
        """Start daemon server: acquire lock, start console reader, bind socket and listen."""
        self._acquire_daemon_lock()

        # Start background console reader
        self.console.start()

        # Clean up stale socket file if any
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(self.socket_path)
        os.chmod(self.socket_path, 0o666)
        self._server_sock.listen(16)
        self._server_sock.setblocking(False)

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="DaemonServerThread")
        self._thread.start()
        _logger.info("Daemon started on %s (PID %d)", self.socket_path, os.getpid())

    def stop(self) -> None:
        """Stop server, close connections, stop hardware handles and clean up socket."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        with self._lock:
            for client in list(self._clients):
                try:
                    client.close()
                except Exception:
                    pass
            self._clients.clear()
            self._client_send_locks.clear()

        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None

        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        # Stop hardware reader
        try:
            self.console.stop()
        except Exception:
            pass

        # Close REPL
        try:
            self.repl.close()
        except Exception:
            pass

        # Stop executor
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        # Release lock
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None
        _logger.info("Daemon stopped")

    def _run_loop(self) -> None:
        """Main select loop accepting connections and servicing client requests."""
        client_buffers: Dict[socket.socket, bytearray] = {}

        while self._running:
            try:
                # Check idle timeout if configured
                if self.idle_timeout and self.idle_timeout > 0:
                    with self._lock:
                        num_clients = len(self._clients)
                    if num_clients == 0:
                        if self._last_client_disconnect_time is None:
                            self._last_client_disconnect_time = time.time()
                        elif time.time() - self._last_client_disconnect_time >= self.idle_timeout:
                            # Auto-shutdown: all clients disconnected and idle timeout expired
                            self._running = False
                            break
                    else:
                        self._last_client_disconnect_time = None

                with self._lock:
                    rlist = [self._server_sock] + list(self._clients) if self._server_sock else []

                readable, _, _ = select.select(rlist, [], [], 0.1)
                for s in readable:
                    if s is self._server_sock:
                        try:
                            client_sock, _ = self._server_sock.accept()
                            client_sock.settimeout(10.0)
                            with self._lock:
                                self._clients.add(client_sock)
                                self._client_send_locks[client_sock] = threading.Lock()
                                self._last_client_disconnect_time = None
                                total_clients = len(self._clients)
                            client_buffers[client_sock] = bytearray()
                            _logger.info("Client connected (total clients: %d)", total_clients)
                        except Exception:
                            pass
                    else:
                        try:
                            data = s.recv(4096)
                            if not data:
                                self._disconnect_client(s, client_buffers)
                                continue

                            buf = client_buffers.get(s, bytearray())
                            buf.extend(data)

                            while b"\n" in buf:
                                line, _, rest = buf.partition(b"\n")
                                buf = bytearray(rest)
                                client_buffers[s] = buf
                                line_str = line.decode("utf-8", errors="replace").strip()
                                if line_str:
                                    self._executor.submit(self._dispatch_request, s, line_str)
                        except Exception:
                            self._disconnect_client(s, client_buffers)
            except Exception:
                time.sleep(0.05)

    def _disconnect_client(self, s: socket.socket, client_buffers: Dict[socket.socket, bytearray]) -> None:
        """Close and remove client from active pool."""
        with self._lock:
            if s in self._clients:
                self._clients.remove(s)
            self._client_send_locks.pop(s, None)
            remaining_clients = len(self._clients)
            if remaining_clients == 0:
                self._last_client_disconnect_time = time.time()
        client_buffers.pop(s, None)
        try:
            s.close()
        except Exception:
            pass
        _logger.info("Client disconnected (remaining clients: %d)", remaining_clients)

    def _dispatch_request(self, client_sock: socket.socket, line_str: str) -> None:
        """Decode request, invoke handler, and send response (runs on ThreadPoolExecutor worker)."""
        try:
            req = decode_request(line_str)
        except Exception as e:
            _logger.warning("Malformed JSON request: %s", e)
            resp = Response(id="error", ok=False, error=f"Malformed JSON request: {e}")
            self._send_response(client_sock, resp)
            return

        _logger.debug("Dispatching request %s (method: %s)", req.id, req.method)
        handler = self._handlers.get(req.method)
        if not handler:
            _logger.warning("Unknown method '%s' requested (id: %s)", req.method, req.id)
            resp = Response(id=req.id, ok=False, error=f"Unknown method '{req.method}'")
            self._send_response(client_sock, resp)
            return

        try:
            res = handler(req.params)
            resp = Response(id=req.id, ok=True, result=res)
        except Exception as e:
            resp = Response(id=req.id, ok=False, error=str(e))

        self._send_response(client_sock, resp)

    def _send_response(self, client_sock: socket.socket, resp: Response) -> None:
        """Transmit response bytes back to client (thread-safe without blocking main daemon lock)."""
        try:
            raw = encode_response(resp)
            with self._lock:
                send_lock = self._client_send_locks.get(client_sock)
            if send_lock:
                with send_lock:
                    client_sock.sendall(raw)
            else:
                client_sock.sendall(raw)
        except Exception:
            pass

    # --- Handlers ---

    def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": get_daemon_signature(),
            "uptime": round(time.time() - self._start_time, 2),
            "clients_count": len(self._clients),
        }

    def _handle_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        _logger.info("Shutdown requested via IPC socket")
        threading.Thread(target=self._delayed_stop, daemon=True, name="ShutdownThread").start()
        return {"status": "shutting_down"}

    def _delayed_stop(self) -> None:
        time.sleep(0.1)
        self.stop()

    def _handle_run_custom_code(self, params: dict[str, Any]) -> dict[str, Any]:
        code = params["code"]
        timeout = float(params.get("timeout", 10.0))
        with self._repl_lock:
            res = self.repl.exec_code(code, timeout=timeout)
        return {
            "ok": res.ok,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "result": res.result,
        }

    def _handle_clear_target_console(self, params: dict[str, Any]) -> dict[str, Any]:
        self.console.clear()
        return {"status": "cleared"}

    def _handle_read_target_console(self, params: dict[str, Any]) -> list[str]:
        tail_lines = params.get("tail_lines")
        head_lines = params.get("head_lines")
        grep = params.get("grep")
        return self.console.get_lines(tail_lines=tail_lines, head_lines=head_lines, grep=grep)

    def _handle_send_target_command(self, params: dict[str, Any]) -> str:
        command = params["command"]
        wait_timeout = float(params.get("wait_timeout", 5.0))
        idle_threshold = float(params.get("idle_threshold", 0.3))

        with self._cmd_lock:
            start_count = self.console.get_total_lines_count()
            raw_cmd = command.encode("utf-8")
            if not raw_cmd.endswith(b"\r") and not raw_cmd.endswith(b"\n"):
                raw_cmd += b"\r\n"
            elif raw_cmd.endswith(b"\r") and not raw_cmd.endswith(b"\r\n"):
                raw_cmd += b"\n"

            self.console.write(raw_cmd)

            # Smart wait: poll every 50ms, finish when no new lines
            # arrive for idle_threshold seconds, or hard ceiling reached
            deadline = time.time() + wait_timeout
            last_change_time = time.time()
            prev_count = start_count

            while time.time() < deadline:
                time.sleep(0.05)
                current_count = self.console.get_total_lines_count()
                if current_count != prev_count:
                    prev_count = current_count
                    last_change_time = time.time()
                elif time.time() - last_change_time >= idle_threshold:
                    break

            new_lines = self.console.get_lines_since(start_count)
            return "\n".join(new_lines) if new_lines else "[No output received]"

    def _handle_get_total_lines_count(self, params: dict[str, Any]) -> int:
        return self.console.get_total_lines_count()

    def _handle_get_lines_since(self, params: dict[str, Any]) -> list[str]:
        start_count = int(params["start_count"])
        max_lines = int(params.get("max_lines", 200))
        return self.console.get_lines_since(start_count=start_count, max_lines=max_lines)

    def _handle_wait_for_console_pattern(self, params: dict[str, Any]) -> dict[str, Any]:
        pattern = params["pattern"]
        timeout = float(params.get("timeout", 30.0))
        check_history = bool(params.get("check_history", True))
        history_lines = int(params.get("history_lines", 100))

        match = self.console.wait_for(
            pattern=pattern,
            timeout=timeout,
            check_history=check_history,
            history_lines=history_lines,
        )
        return {
            "matched": match is not None,
            "line": match,
            "pattern": pattern,
        }

    def _handle_console_write(self, params: dict[str, Any]) -> int:
        data_hex = params.get("data_hex", "")
        data = bytes.fromhex(data_hex)
        return self.console.write(data)

    def _handle_repl_is_connected(self, params: dict[str, Any]) -> bool:
        return self.repl.is_connected()

    def _handle_repl_connect(self, params: dict[str, Any]) -> bool:
        self.repl.connect()
        return True

    def _handle_repl_close(self, params: dict[str, Any]) -> bool:
        self.repl.close()
        return True

    def _handle_get_firmware_info(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._firmware_info:
            try:
                self._firmware_info = check_firmware_handshake(self.repl)
            except Exception as e:
                return {"error": str(e)}
        return self._firmware_info


def stop_daemon(lock_path: str = DEFAULT_LOCK_PATH, socket_path: str = DEFAULT_SOCKET_PATH) -> bool:
    """Find and stop a running daemon process, releasing all ports."""
    pid = None
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content.isdigit():
                pid = int(content)
        except Exception:
            pass

    stopped = False
    if pid is not None and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(25):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    stopped = True
                    break
            if not stopped:
                try:
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.1)
                    stopped = True
                except OSError:
                    stopped = True
        except ProcessLookupError:
            stopped = True
        except Exception as e:
            print(f"Error stopping daemon PID {pid}: {e}", file=sys.stderr)

    # Clean up socket and lock files
    if os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass
    if os.path.exists(lock_path):
        try:
            os.unlink(lock_path)
        except OSError:
            pass

    return stopped


def run_daemon(
    control_port: Optional[str] = None,
    uart_port: Optional[str] = None,
    socket_path: str = DEFAULT_SOCKET_PATH,
    lock_path: str = DEFAULT_LOCK_PATH,
    idle_timeout: Optional[float] = 30.0,
) -> None:
    """Run daemon process until SIGINT/SIGTERM or idle timeout."""
    eff_ctrl = control_port or get_default_control_port()
    eff_uart = uart_port or get_default_uart_port()

    check_hw_puppet_connection(eff_ctrl, eff_uart)

    repl = ReplClient(port=eff_ctrl)
    console = ConsoleReader(port=eff_uart)

    # Perform active serial handshake to verify compatible hw-puppet firmware
    fw_info = check_firmware_handshake(repl)

    server = DaemonServer(
        repl=repl,
        console=console,
        socket_path=socket_path,
        lock_path=lock_path,
        idle_timeout=idle_timeout,
        firmware_info=fw_info,
    )

    stop_event = threading.Event()

    def _signal_handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    server.start()
    try:
        while not stop_event.is_set() and server.is_running():
            time.sleep(0.5)
    finally:
        server.stop()


def main() -> None:
    """CLI entrypoint for standalone daemon service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    default_ctrl = get_default_control_port()
    default_uart = get_default_uart_port()
    parser = argparse.ArgumentParser(description="AE-HW-BRIDGE Shared Daemon Service")
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("--name", default="base", help="Target or puppet identifier for scoping socket and lock")
    parser.add_argument("--control-port", default=default_ctrl, help=f"CDC0 MicroPython REPL port (default: {default_ctrl})")
    parser.add_argument("--uart-port", default=default_uart, help=f"CDC1 Target UART port (default: {default_uart})")
    parser.add_argument("--socket-path", default=None, help="Unix domain socket path (default: /tmp/ae-hw-bridge-{name}.sock)")
    parser.add_argument("--lock-path", default=None, help="Lockfile path (default: /tmp/ae-hw-bridge-{name}.lock)")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=30.0,
        help="Seconds of no connected clients before auto-shutdown (0 to disable, default: 30.0)",
    )
    parser.add_argument("--stop", action="store_true", help="Stop running daemon and release hardware ports")

    args = parser.parse_args()

    effective_socket_path = args.socket_path or get_socket_path(args.name)
    effective_lock_path = args.lock_path or get_lock_path(args.name)

    if args.stop:
        stopped = stop_daemon(lock_path=effective_lock_path, socket_path=effective_socket_path)
        if stopped:
            print(f"AE-HW-BRIDGE daemon '{args.name}' stopped. Serial ports released.")
        else:
            print(f"No active AE-HW-BRIDGE daemon found for '{args.name}'.")
        sys.exit(0)

    idle_timeout = None if args.idle_timeout <= 0 else args.idle_timeout
    run_daemon(
        control_port=args.control_port,
        uart_port=args.uart_port,
        socket_path=effective_socket_path,
        lock_path=effective_lock_path,
        idle_timeout=idle_timeout,
    )


if __name__ == "__main__":
    main()
