"""MicroPython Raw REPL client implementation over serial."""

import time
import logging
from typing import Optional
import serial

from ae_hw_bridge.core.interfaces import IReplClient, ExecutionResult
from ae_hw_bridge.core.port_utils import get_default_control_port

_logger = logging.getLogger(__name__)


class ReplClient(IReplClient):
    """Client for controlling MicroPython via Raw REPL over serial."""

    def __init__(self, port: Optional[str] = None, baudrate: int = 115200, timeout: float = 2.0) -> None:
        self.port = port or get_default_control_port()
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None
        self._connected: bool = False

    def connect(self) -> None:
        """Establish connection to the serial port and enter raw REPL mode."""
        if self._connected and self._serial and self._serial.is_open:
            return

        try:
            self._serial = serial.Serial(self.port, baudrate=self.baudrate, timeout=self.timeout)
        except (serial.SerialException, OSError) as e:
            from ae_hw_bridge.core.port_utils import find_port_holder

            holder = find_port_holder(self.port)
            if holder:
                pid, comm = holder
                _logger.warning("REPL port %s held by process '%s' (PID %d)", self.port, comm, pid)
                raise RuntimeError(
                    f"Cannot open REPL port {self.port}: busy (held by process '{comm}', PID {pid})"
                ) from e
            _logger.error("Failed to open REPL port %s: %s", self.port, e)
            raise RuntimeError(f"Cannot open REPL port {self.port}: {e}") from e

        self._enter_raw_repl()
        self._connected = True
        _logger.info("Connected to MicroPython raw REPL on %s", self.port)

    def close(self) -> None:
        """Exit raw REPL mode and close the underlying serial connection."""
        if self._serial and self._serial.is_open:
            try:
                self._exit_raw_repl()
            except Exception:
                pass
            self._serial.close()
        self._connected = False

    def is_connected(self) -> bool:
        """Check if the client is currently connected and port is open."""
        return self._connected and self._serial is not None and self._serial.is_open

    def _enter_raw_repl(self) -> None:
        """Send Ctrl-C and Ctrl-A sequence to enter Raw REPL."""
        assert self._serial is not None
        self._serial.reset_input_buffer()
        # Send Ctrl-C twice to break any running code
        self._serial.write(b"\r\x03\x03")
        time.sleep(0.05)
        # Send Ctrl-A to enter raw REPL
        self._serial.write(b"\r\x01")
        time.sleep(0.05)
        resp = self._serial.read_until(b"raw REPL; CTRL-B to exit\r\n>")
        if b"raw REPL" not in resp:
            raise RuntimeError(f"Failed to enter raw REPL on {self.port}: {resp!r}")

    def _exit_raw_repl(self) -> None:
        """Send Ctrl-B to return to normal REPL."""
        if self._serial and self._serial.is_open:
            self._serial.write(b"\r\x02")

    def exec_code(self, code: str, timeout: float = 10.0) -> ExecutionResult:
        """Execute MicroPython code in RAM and return ExecutionResult."""
        if not self.is_connected():
            self.connect()

        assert self._serial is not None
        prev_timeout = self._serial.timeout
        self._serial.timeout = timeout
        try:
            clean_code = code.strip().encode("utf-8") + b"\r\n"
            self._serial.write(clean_code)
            self._serial.write(b"\x04")  # Send EOT to execute

            ack = self._serial.read_until(b"OK")
            if not ack.endswith(b"OK"):
                return ExecutionResult(
                    ok=False,
                    stdout="",
                    stderr=f"MicroPython rejected execution: {ack.decode('utf-8', errors='replace')}",
                    result=None,
                )

            # Read until EOT followed by '>' prompt: b"\x04>"
            raw_output = self._serial.read_until(b"\x04>")
            if not raw_output.endswith(b"\x04>"):
                return ExecutionResult(
                    ok=False,
                    stdout="",
                    stderr=f"Execution timed out after {timeout}s",
                    result=None,
                )

            # Strip trailing \x04>
            body = raw_output[:-2]
            parts = body.split(b"\x04")
            stdout = parts[0].decode("utf-8", errors="replace").strip()
            stderr = parts[1].decode("utf-8", errors="replace").strip() if len(parts) > 1 else ""

            is_ok = len(stderr) == 0
            return ExecutionResult(
                ok=is_ok,
                stdout=stdout,
                stderr=stderr,
                result=stdout if is_ok else None,
            )
        finally:
            if self._serial and self._serial.is_open:
                self._serial.timeout = prev_timeout
