"""Abstract interfaces and data structures for AE-HW-BRIDGE.

Adheres to Interface Segregation Principle (ISP) and Dependency Inversion Principle (DIP).
"""

from typing import Protocol, runtime_checkable, Any, Optional
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionResult:
    """Immutable result of a MicroPython code or scenario execution."""

    ok: bool
    stdout: str
    stderr: str
    result: Any = None




@runtime_checkable
class IReplClient(Protocol):
    """Protocol for Raw REPL serial client controlling the ESP32 bridge."""

    def connect(self) -> None:
        """Establish connection to the serial port and enter raw REPL mode."""
        ...

    def close(self) -> None:
        """Exit raw REPL mode and close the underlying serial connection."""
        ...

    def is_connected(self) -> bool:
        """Check if the client is currently connected and ready."""
        ...

    def exec_code(self, code: str, timeout: float = 10.0) -> ExecutionResult:
        """Execute MicroPython code in RAM and return ExecutionResult."""
        ...


@runtime_checkable
class IConsoleReader(Protocol):
    """Protocol for reading and buffering the target console UART output."""

    def start(self) -> None:
        """Start background monitoring thread on the target UART port."""
        ...

    def stop(self) -> None:
        """Stop background monitoring thread and close port."""
        ...

    def get_tail(self, lines: int = 50, grep: Optional[str] = None) -> list[str]:
        """Retrieve recent lines from the circular buffer, optionally filtered by grep."""
        ...

    def get_lines(
        self,
        tail_lines: Optional[int] = None,
        head_lines: Optional[int] = None,
        grep: Optional[str] = None,
    ) -> list[str]:
        """Retrieve lines from buffer: head_lines (from start) or tail_lines (from end), with optional grep."""
        ...

    def get_total_lines_count(self) -> int:
        """Return the monotonic count of all lines processed since start."""
        ...

    def get_lines_since(self, start_count: int, max_lines: int = 200) -> list[str]:
        """Return lines received since a previous total_lines_count mark."""
        ...

    def write(self, data: bytes) -> int:
        """Write raw bytes to the target console."""
        ...

    def clear(self) -> None:
        """Clear all lines currently buffered."""
        ...

    def wait_for(
        self,
        pattern: str,
        timeout: float = 10.0,
        check_history: bool = True,
        history_lines: int = 100,
    ) -> Optional[str]:
        """Block until an incoming line matches pattern (regex) or timeout expires.

        If check_history is True, inspects up to history_lines recent buffered lines before waiting for new lines.
        """
        ...



