"""Threaded console reader buffering target UART into a bounded ring buffer."""

import re
import logging
import threading
import collections
import time
from datetime import datetime, timezone
from typing import Optional
import serial

from ae_hw_bridge.core.interfaces import IConsoleReader
from ae_hw_bridge.core.port_utils import get_default_uart_port

_logger = logging.getLogger(__name__)


class ConsoleReader(IConsoleReader):
    """Background serial monitor reading target UART into a bounded ring buffer with grep, idle-flush, and pattern wait."""

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        maxlen: int = 3000,
        pty_path: Optional[str] = None,
        enable_pty: bool = True,
    ) -> None:
        self.port = port or get_default_uart_port()
        self.baudrate = baudrate
        self.maxlen = maxlen
        self.pty_path = pty_path
        self.enable_pty = enable_pty
        self._buffer: collections.deque[str] = collections.deque(maxlen=maxlen)
        self._total_lines_count: int = 0
        self._lock = threading.Lock()
        self._serial_lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pty_thread: Optional[threading.Thread] = None
        self._serial: Optional[serial.Serial] = None
        self._pty_master_fd: Optional[int] = None
        self._pty_slave_fd: Optional[int] = None
        self._pty_slave_name: Optional[str] = None
        self._pty_symlinks: list[str] = []

    @property
    def effective_pty_path(self) -> Optional[str]:
        """Return the active PTY symlink or slave device path if initialized."""
        if self._pty_symlinks:
            return self._pty_symlinks[0]
        return self._pty_slave_name

    def start(self) -> None:
        """Start background monitoring thread on the target UART port."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._setup_pty()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ConsoleReaderThread")
        self._thread.start()
        _logger.info("ConsoleReader started for %s", self.port)

    def stop(self) -> None:
        """Stop background monitoring thread and close port."""
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()
        with self._serial_lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.cancel_read()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._pty_thread:
            self._pty_thread.join(timeout=1.0)
            self._pty_thread = None
        with self._serial_lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception:
                    pass
        self._cleanup_pty()
        _logger.info("ConsoleReader stopped for %s", self.port)

    def _setup_pty(self) -> None:
        """Create virtual pseudoterminal (PTY) and symlink for external tools (tio, minicom)."""
        if not self.enable_pty:
            return
        try:
            import os
            import pty
            import tty

            master_fd, slave_fd = pty.openpty()
            try:
                tty.setraw(slave_fd)
            except Exception:
                pass

            slave_name = os.ttyname(slave_fd)
            self._pty_master_fd = master_fd
            self._pty_slave_fd = slave_fd
            self._pty_slave_name = slave_name

            # Determine symlinks to create
            symlinks_to_create = []
            if self.pty_path:
                symlinks_to_create.append(self.pty_path)
                if self.pty_path != "/tmp/ae-hw-bridge-uart":
                    symlinks_to_create.append("/tmp/ae-hw-bridge-uart")
            else:
                symlinks_to_create.append("/tmp/ae-hw-bridge-uart")

            self._pty_symlinks = []
            for target_link in symlinks_to_create:
                try:
                    if os.path.islink(target_link) or os.path.exists(target_link):
                        os.unlink(target_link)
                    os.symlink(slave_name, target_link)
                    self._pty_symlinks.append(target_link)
                    _logger.info("Created Virtual UART PTY symlink: %s -> %s", target_link, slave_name)
                except OSError as e:
                    _logger.warning("Failed to create PTY symlink %s: %s", target_link, e)

            # Start thread reading from PTY master and writing to physical serial
            self._pty_thread = threading.Thread(
                target=self._run_pty_to_serial,
                daemon=True,
                name="PtyToSerialThread",
            )
            self._pty_thread.start()
        except Exception as e:
            _logger.warning("Failed to initialize virtual PTY: %s", e)

    def _cleanup_pty(self) -> None:
        """Clean up virtual PTY descriptors and symlinks."""
        import os

        for link in list(self._pty_symlinks):
            try:
                if os.path.islink(link):
                    os.unlink(link)
            except OSError:
                pass
        self._pty_symlinks.clear()

        if self._pty_thread and self._pty_thread.is_alive() and threading.current_thread() != self._pty_thread:
            self._pty_thread.join(timeout=0.5)
            self._pty_thread = None

        if self._pty_master_fd is not None:
            try:
                os.close(self._pty_master_fd)
            except OSError:
                pass
            self._pty_master_fd = None

        if self._pty_slave_fd is not None:
            try:
                os.close(self._pty_slave_fd)
            except OSError:
                pass
            self._pty_slave_fd = None

    def _run_pty_to_serial(self) -> None:
        """Forward bytes typed by user in tio/minicom (PTY slave) to physical UART."""
        import select
        import os

        while not self._stop_event.is_set():
            fd = self._pty_master_fd
            if fd is None:
                break
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
                if not r:
                    continue
                fd = self._pty_master_fd
                if fd is None:
                    break
                data = os.read(fd, 1024)
                if data:
                    self.write(data)
            except (OSError, ValueError, TypeError):
                time.sleep(0.05)

    def _append_line(self, line: str) -> None:
        """Timestamp, append line to ring buffer, increment counter and notify waiters."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        formatted = f"[{ts}] {line}"
        with self._condition:
            self._buffer.append(formatted)
            self._total_lines_count += 1
            self._condition.notify_all()

    def _open_serial(self) -> serial.Serial:
        """Open serial port with DTR/RTS asserted and exclusive access (TIOCEXCL)."""
        try:
            s = serial.Serial(self.port, baudrate=self.baudrate, timeout=0.1)
        except (serial.SerialException, OSError) as e:
            from ae_hw_bridge.core.port_utils import find_port_holder

            holder = find_port_holder(self.port)
            if holder:
                pid, comm = holder
                _logger.warning("UART port %s held by process '%s' (PID %d)", self.port, comm, pid)
                raise RuntimeError(
                    f"Cannot open UART port {self.port}: busy (held by process '{comm}', PID {pid})"
                ) from e
            _logger.error("Failed to open UART port %s: %s", self.port, e)
            raise

        s.dtr = True
        s.rts = True
        _logger.info("Opened serial port %s (baudrate %d)", self.port, self.baudrate)
        try:
            import termios
            import fcntl

            fcntl.ioctl(s.fileno(), termios.TIOCEXCL)
        except Exception:
            pass
        return s

    def _run(self) -> None:
        """Background thread loop reading chunks via in_waiting and splitting lines with idle flush for prompts."""
        accumulator = bytearray()
        last_rx_time = time.time()
        retry_count = 0
        while not self._stop_event.is_set():
            try:
                with self._serial_lock:
                    if self._serial is None or not self._serial.is_open:
                        self._serial = self._open_serial()
                        if retry_count > 0:
                            _logger.info("Reconnected to %s after %d retries", self.port, retry_count)
                            retry_count = 0
                    n = self._serial.in_waiting
                    chunk = self._serial.read(n) if n > 0 else b""

                if chunk:
                    if self._pty_master_fd is not None:
                        try:
                            import os
                            os.write(self._pty_master_fd, chunk)
                        except OSError:
                            pass
                    accumulator.extend(chunk)
                    last_rx_time = time.time()
                    while b"\n" in accumulator:
                        raw_line, _, rest = accumulator.partition(b"\n")
                        accumulator = bytearray(rest)
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                        if line:
                            self._append_line(line)
                else:
                    # Inter-character idle flush: if text arrived without \n (e.g. login/password/shell prompt)
                    # and no new bytes arrive for >0.08s, flush it as a complete line so it is immediately visible
                    if len(accumulator) > 0 and (time.time() - last_rx_time > 0.08):
                        line = accumulator.decode("utf-8", errors="replace").rstrip("\r")
                        accumulator.clear()
                        if line:
                            self._append_line(line)

                    self._stop_event.wait(0.02)
            except Exception as exc:
                _logger.warning("Console reader lost connection to %s: %s", self.port, exc)
                with self._serial_lock:
                    if self._serial:
                        try:
                            self._serial.close()
                        except Exception:
                            pass
                        self._serial = None
                backoff = min(5.0, 0.5 * (2 ** min(retry_count, 4)))
                retry_count += 1
                _logger.info("Will retry opening %s in %.1fs (attempt %d)", self.port, backoff, retry_count)
                self._stop_event.wait(backoff)

    def get_lines(
        self,
        tail_lines: Optional[int] = None,
        head_lines: Optional[int] = None,
        grep: Optional[str] = None,
    ) -> list[str]:
        """Retrieve lines from buffer: head_lines (from start) or tail_lines (from end), with optional grep regex."""
        with self._lock:
            snapshot = list(self._buffer)

        if grep:
            pattern = re.compile(grep, re.IGNORECASE)
            snapshot = [item for item in snapshot if pattern.search(item)]

        if head_lines is not None and head_lines > 0:
            return snapshot[:head_lines]
        elif tail_lines is not None and tail_lines > 0:
            return snapshot[-tail_lines:]
        elif head_lines is None and tail_lines is None:
            return snapshot[-50:]  # default 50 tail lines
        return snapshot

    def get_tail(self, lines: int = 50, grep: Optional[str] = None) -> list[str]:
        """Retrieve recent lines from the circular buffer (compatibility alias)."""
        return self.get_lines(tail_lines=lines, grep=grep)

    def get_total_lines_count(self) -> int:
        """Return the monotonic count of all lines processed since start."""
        with self._lock:
            return self._total_lines_count

    def get_lines_since(self, start_count: int, max_lines: int = 200) -> list[str]:
        """Return lines received since a previous total_lines_count mark."""
        with self._lock:
            diff = self._total_lines_count - start_count
            if diff <= 0:
                return []
            count_to_take = min(diff, len(self._buffer), max_lines)
            return list(self._buffer)[-count_to_take:]

    def wait_for(
        self,
        pattern: str,
        timeout: float = 10.0,
        check_history: bool = True,
        history_lines: int = 100,
    ) -> Optional[str]:
        """Block until an incoming line matches pattern (regex) or timeout expires.

        Args:
            pattern: Regular expression to match against incoming lines.
            timeout: Maximum seconds to wait.
            check_history: If True, inspect recent buffered lines first before waiting.
            history_lines: Number of recent lines to check when check_history is True.

        Returns:
            The matched line string, or None if timeout expired without match.
        """
        regex = re.compile(pattern, re.IGNORECASE)
        deadline = time.time() + timeout

        with self._condition:
            if check_history and self._buffer:
                recent_lines = list(self._buffer)[-history_lines:]
                for line in reversed(recent_lines):
                    if regex.search(line):
                        return line

            # Track using monotonic counter instead of buffer length
            last_seen_count = self._total_lines_count

            while time.time() < deadline and not self._stop_event.is_set():
                current_count = self._total_lines_count
                if current_count > last_seen_count:
                    diff = current_count - last_seen_count
                    new_count = min(diff, len(self._buffer))
                    new_lines = list(self._buffer)[-new_count:]
                    for line in new_lines:
                        if regex.search(line):
                            return line
                    last_seen_count = current_count

                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=min(0.2, remaining))

        return None

    def write(self, data: bytes) -> int:
        """Write raw bytes to the target console."""
        with self._serial_lock:
            if not self._serial or not self._serial.is_open:
                self._serial = self._open_serial()
            return self._serial.write(data)

    def clear(self) -> None:
        """Clear all lines currently buffered."""
        with self._lock:
            self._buffer.clear()
