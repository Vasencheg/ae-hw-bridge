"""Utility functions for inspecting serial ports and detecting process conflicts."""

import os
from typing import Optional, Tuple, Any


def find_port_holder(port_path: str) -> Optional[Tuple[int, str]]:
    """Scan /proc to find the PID and command name of any process holding port_path open.

    Args:
        port_path: Path to character device (e.g. /dev/ttyACM1).

    Returns:
        Tuple of (pid, comm) if a holding process is found (excluding current PID), else None.
    """
    try:
        real_port = os.path.realpath(port_path)
        current_pid = os.getpid()

        for pid_entry in os.listdir("/proc"):
            if not pid_entry.isdigit():
                continue
            pid = int(pid_entry)
            if pid == current_pid:
                continue

            fd_dir = f"/proc/{pid_entry}/fd"
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        link = os.path.realpath(f"{fd_dir}/{fd}")
                        if link == real_port:
                            comm = "unknown"
                            comm_path = f"/proc/{pid_entry}/comm"
                            if os.path.exists(comm_path):
                                with open(comm_path, "r", encoding="utf-8") as f:
                                    comm = f.read().strip()
                            return pid, comm
                    except (OSError, PermissionError):
                        continue
            except (OSError, PermissionError):
                continue
    except Exception:
        pass
    return None


def get_default_control_port() -> str:
    """Resolve default CDC0 control port with priority:
    1. HW_PUPPET_CONTROL_PORT / AE_BRIDGE_CONTROL_PORT env vars
    2. /dev/hw-puppet-control
    3. /dev/ae-bridge-control
    4. /dev/ttyACM0 fallback
    """
    for env_var in ("HW_PUPPET_CONTROL_PORT", "AE_BRIDGE_CONTROL_PORT"):
        val = os.getenv(env_var)
        if val:
            return val
    if os.path.exists("/dev/hw-puppet-control"):
        return "/dev/hw-puppet-control"
    if os.path.exists("/dev/ae-bridge-control"):
        return "/dev/ae-bridge-control"
    return "/dev/ttyACM0"


def get_default_uart_port() -> str:
    """Resolve default CDC1 target UART port with priority:
    1. HW_PUPPET_UART_PORT / AE_BRIDGE_UART_PORT env vars
    2. /dev/hw-puppet-uart
    3. /dev/ae-bridge-uart
    4. /dev/ttyACM1 fallback
    """
    for env_var in ("HW_PUPPET_UART_PORT", "AE_BRIDGE_UART_PORT"):
        val = os.getenv(env_var)
        if val:
            return val
    if os.path.exists("/dev/hw-puppet-uart"):
        return "/dev/hw-puppet-uart"
    if os.path.exists("/dev/ae-bridge-uart"):
        return "/dev/ae-bridge-uart"
    return "/dev/ttyACM1"


def is_hw_puppet_connected(control_port: Optional[str] = None, uart_port: Optional[str] = None) -> bool:
    """Check if both HW-PUPPET control and UART ports are present."""
    ctrl = control_port or get_default_control_port()
    uart = uart_port or get_default_uart_port()
    return os.path.exists(ctrl) and os.path.exists(uart)


def check_hw_puppet_connection(control_port: Optional[str] = None, uart_port: Optional[str] = None) -> None:
    """Validate that HW-PUPPET is connected and available.

    Logs diagnostic details to logger, raising RuntimeError if device is missing.
    """
    ctrl = control_port or get_default_control_port()
    uart = uart_port or get_default_uart_port()

    missing = []
    if not os.path.exists(ctrl):
        missing.append(ctrl)
    if not os.path.exists(uart):
        missing.append(uart)

    if missing:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(
            "HW-PUPPET device not found. Missing port(s): %s. Please connect HW-PUPPET via USB.",
            ", ".join(missing),
        )
        raise RuntimeError("HW-PUPPET is not connected. Please check physical connection.")


def check_firmware_handshake(repl: Any) -> dict[str, Any]:
    """Perform firmware compatibility handshake with HW-Puppet via Raw REPL.

    Executes `import hw_puppet; print(hw_puppet.info())` on the board.
    Validates that hw_puppet module exists and returns firmware metadata dictionary.

    Raises:
        RuntimeError: If hw_puppet module cannot be imported or returns error.
    """
    import ast
    import logging
    logger = logging.getLogger(__name__)

    res = repl.exec_code("import hw_puppet; print(hw_puppet.info())", timeout=3.0)
    if not res.ok:
        err = res.stderr.strip() or "REPL execution failed"
        logger.error("Firmware handshake failed: %s", err)
        raise RuntimeError(
            f"HW-Puppet firmware handshake failed: {err}. "
            "Please ensure the device is running compatible hw-puppet firmware (v0.2.0+)."
        )

    info: dict[str, Any] = {}
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                info = ast.literal_eval(line)
                break
            except Exception:
                pass

    if not info:
        logger.warning("Could not parse hw_puppet.info() dictionary from: %s", res.stdout)
        info = {"raw": res.stdout.strip()}

    logger.info(
        "HW-Puppet handshake successful: platform=%s, version=%s, build=%s",
        info.get("platform", "unknown"),
        info.get("version", "unknown"),
        info.get("build_date", "unknown"),
    )
    return info


