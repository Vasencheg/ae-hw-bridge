"""BaseTarget class and @repl decorator for hardware target modules."""

import time
import inspect
import textwrap
import functools
import logging
from typing import Optional, Any, Callable
from ae_hw_bridge.core.interfaces import (
    IReplClient,
    IConsoleReader,
    ExecutionResult,
)

_logger = logging.getLogger(__name__)


def repl(func: Callable[..., Any]) -> Callable[..., ExecutionResult]:
    """Decorator marking a method to be executed in ESP32 MicroPython RAM via Raw REPL.

    Extracts the function body via AST/inspect, binds arguments, creates MicroPython
    variable assignments, sends the code to self.repl.exec_code(), and returns ExecutionResult.
    """
    @functools.wraps(func)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> ExecutionResult:
        sig = inspect.signature(func)
        bound = sig.bind(self, *args, **kwargs)
        bound.apply_defaults()

        # Build parameter assignments for MicroPython preamble
        params_code = []
        for name, val in bound.arguments.items():
            if name == "self":
                continue
            params_code.append(f"{name} = {repr(val)}")

        # Extract source lines and find body
        try:
            source_lines = inspect.getsource(func).splitlines()
        except (OSError, IOError) as e:
            _logger.error("Failed to inspect source for @repl function %s: %s", func.__name__, e)
            return ExecutionResult(ok=False, stdout="", stderr=f"Cannot inspect function source: {e}")

        body_lines = []
        in_body = False
        for line in source_lines:
            stripped = line.strip()
            if not in_body:
                # Check for end of function header
                if stripped.startswith("def ") and stripped.endswith(":"):
                    in_body = True
                elif stripped.endswith(":") and "def " in line:
                    in_body = True
            else:
                body_lines.append(line)

        dedented_body = textwrap.dedent("\n".join(body_lines))
        full_code = "\n".join(params_code) + ("\n" if params_code else "") + dedented_body

        _logger.debug("Executing @repl method %s on MicroPython REPL:\n%s", func.__name__, full_code)
        repl_client: IReplClient = getattr(self, "repl", None)
        if repl_client is None:
            raise RuntimeError(f"Target {self} has no 'repl' client attribute for @repl execution")

        return repl_client.exec_code(full_code)

    setattr(wrapper, "_is_repl_method", True)
    return wrapper


class BaseTarget:
    """Foundational target controller providing the 5 baseline hardware bridge operations.

    Custom targets should subclass BaseTarget to inherit all core methods and extend with
    board-specific operations (e.g. JetsonTarget, STM32Target).
    """

    def __init__(
        self,
        repl: IReplClient,
        console: IConsoleReader,
    ) -> None:
        self.repl = repl
        self.console = console

    def run_custom_code(self, code: str, timeout: float = 10.0) -> dict[str, Any]:
        """Execute arbitrary MicroPython code directly in ESP32 RAM via CDC0 Raw REPL.

        Args:
            code: MicroPython source code string to execute.
            timeout: Maximum execution time in seconds before aborting (default: 10.0s).
        """
        res = self.repl.exec_code(code, timeout=timeout)
        return {"ok": res.ok, "stdout": res.stdout, "stderr": res.stderr, "result": res.result}

    def clear_target_console(self) -> dict[str, str]:
        """Clear the target UART console circular buffer.

        Recommended before triggering a reset or running a test scenario to isolate fresh output.
        """
        self.console.clear()
        return {"status": "cleared"}

    def read_target_console(
        self,
        tail_lines: Optional[int] = None,
        head_lines: Optional[int] = None,
        grep: Optional[str] = None,
    ) -> list[str]:
        """Read lines from the target board UART console buffer (CDC1).

        NOTE: This reads the passive UART reception ring buffer (e.g. bootloader or kernel output).
        To read system logs from an already running Linux target, use `send_target_command`.

        Args:
            tail_lines: Number of most recent lines from the end of the buffer (default: 50 if head_lines not specified).
            head_lines: Number of earliest lines from the start of the buffer (e.g. first N lines of bootloader after reset).
            grep: Optional case-insensitive regex pattern to filter lines (e.g. 'panic|error|SafeRTOS').
        """
        return self.console.get_lines(tail_lines=tail_lines, head_lines=head_lines, grep=grep)

    def send_target_command(
        self,
        command: str,
        wait_timeout: float = 5.0,
        idle_threshold: float = 0.3,
    ) -> str:
        """Send an interactive command string to the target console and collect ONLY new response lines.

        Args:
            command: Shell command string to transmit to target UART (e.g. 'uname -a' or 'nvidia').
            wait_timeout: Maximum seconds to wait before returning response (default: 5.0s hard ceiling).
            idle_threshold: Seconds of UART silence before considering output complete (default: 0.3s).
        """
        start_count = self.console.get_total_lines_count()
        raw_cmd = command.encode("utf-8")
        if not raw_cmd.endswith(b"\r") and not raw_cmd.endswith(b"\n"):
            raw_cmd += b"\r\n"
        elif raw_cmd.endswith(b"\r") and not raw_cmd.endswith(b"\r\n"):
            raw_cmd += b"\n"
        self.console.write(raw_cmd)

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

    def wait_for_console_pattern(
        self,
        pattern: str,
        timeout: float = 30.0,
        check_history: bool = True,
    ) -> dict[str, Any]:
        r"""Wait for a line matching regex pattern in target UART console stream.

        First checks recent buffer history (unless check_history=False). If not found,
        blocks until matching line appears or timeout expires. Ideal for waiting for
        bootloader, kernel messages, login prompts or errors after reset.

        Args:
            pattern: Regular expression to match (e.g. 'login:', r'(?:[$#]\s*$|\w+@\w+:.*[\$#]\s*)', or 'kernel panic').
            timeout: Maximum seconds to wait (default: 30.0s).
            check_history: If True, inspect recent buffered lines first before waiting for new lines (default: True).
        """
        match = self.console.wait_for(pattern=pattern, timeout=timeout, check_history=check_history)
        return {
            "matched": match is not None,
            "line": match,
            "pattern": pattern,
        }

    def get_bridge_info(self) -> dict[str, Any]:
        """Get bridge software version, connected hardware puppet firmware info, and active ports.

        Returns metadata including ae-hw-bridge package version, ESP32-S3 hw-puppet firmware
        build details (version, platform, git hash), and serial port paths.
        """
        import ast
        from ae_hw_bridge import __version__
        from ae_hw_bridge.core.port_utils import get_default_control_port, get_default_uart_port

        firmware_info: dict[str, Any] = {}
        if hasattr(self.repl, "get_firmware_info"):
            try:
                firmware_info = self.repl.get_firmware_info()
            except Exception as e:
                firmware_info = {"error": str(e)}
        else:
            try:
                from ae_hw_bridge.core.port_utils import check_firmware_handshake
                firmware_info = check_firmware_handshake(self.repl)
            except Exception as e:
                firmware_info = {"error": str(e)}

        control_port = getattr(self.repl, "port", None) or get_default_control_port()
        uart_port = getattr(self.console, "port", None) or get_default_uart_port()

        return {
            "bridge_version": __version__,
            "firmware": firmware_info,
            "ports": {
                "control": control_port,
                "uart": uart_port,
            },
        }
