"""FastMCP server factory, target orchestrator, and CLI runner for AE-HW-BRIDGE."""

import os
import sys
import re
import json
import time
import shutil
import argparse
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from mcp.server.fastmcp import FastMCP

from ae_hw_bridge import __version__
from ae_hw_bridge.core.interfaces import IReplClient, IConsoleReader
from ae_hw_bridge.core.repl_client import ReplClient
from ae_hw_bridge.core.console_reader import ConsoleReader
from ae_hw_bridge.core.port_utils import get_default_control_port, get_default_uart_port
from ae_hw_bridge.core.discovery import scan_hw_puppets, resolve_puppet, HWPuppetDevice
from ae_hw_bridge.core.config import load_bridge_config, BridgeConfig, TargetConfig
from ae_hw_bridge.targets.base import BaseTarget
from ae_hw_bridge.targets.loader import TargetLoader
from ae_hw_bridge.daemon.protocol import (
    get_socket_path,
    get_lock_path,
    get_pty_path,
    DEFAULT_PTY_PATH,
)
from ae_hw_bridge.daemon.client import DaemonIpcClient, ensure_daemon_running
from ae_hw_bridge.daemon.server import stop_daemon
from ae_hw_bridge.mcp.tools import register_tools

_logger = logging.getLogger(__name__)


def create_server(
    repl_port: Optional[str] = None,
    uart_port: Optional[str] = None,
    target_path: Optional[str] = None,
    config_path: Optional[str] = None,
    start_console: bool = False,
    repl: Optional[IReplClient] = None,
    console: Optional[IConsoleReader] = None,
    target: Optional[BaseTarget] = None,
) -> FastMCP:
    """Create and configure FastMCP server instance supporting single and multi-target topologies.

    Args:
        repl_port: Optional override for CDC0 MicroPython raw REPL serial port.
        uart_port: Optional override for CDC1 target console UART bridge port.
        target_path: Optional path to custom target module or directory.
        config_path: Optional path to .ae-hw-bridge/config.yml.
        start_console: Whether to immediately launch the background console thread.
        repl: Optional pre-configured IReplClient instance.
        console: Optional pre-configured IConsoleReader instance.
        target: Optional pre-configured BaseTarget instance.
    """
    mcp = FastMCP("ae-hw-bridge")
    config = load_bridge_config(config_path=config_path)

    # If caller explicitly provided a target instance, register it directly
    if target is not None:
        register_tools(mcp, target=target, prefix=None)
        return mcp

    # Determine targets to load
    # Priority 1: Targets defined in config.yml
    # Priority 2: Target directory passed via target_path or AE_TARGET_DIR
    # Priority 3: Discovered target files in .ae-hw-bridge/targets/
    target_specs: Dict[str, TargetConfig] = {}

    if config.targets:
        target_specs = config.targets
    elif target_path:
        p = Path(target_path)
        name = p.stem if p.is_file() else p.name
        if name == "target":
            name = p.parent.name
        target_specs = {name: TargetConfig(name=name, path=str(p), puppet=config.puppet, port=config.port, uart_port=config.uart_port)}
    else:
        discovered = TargetLoader.discover_target_files()
        if discovered:
            for name, path in discovered.items():
                if name == "base":
                    # Standalone target.py
                    target_specs = {"default": TargetConfig(name="default", path=str(path), puppet=config.puppet, port=config.port, uart_port=config.uart_port)}
                    break
                target_specs[name] = TargetConfig(name=name, path=str(path), puppet=config.puppet, port=config.port, uart_port=config.uart_port)

    # --- Mode 1: Targets defined (1 or more) -> Prefix all tools with <target>_ ---
    if target_specs:
        _logger.info("Initializing %d target(s) for MCP server...", len(target_specs))
        for target_name, t_cfg in target_specs.items():
            ctrl_port = repl_port or t_cfg.port or config.port
            u_port = uart_port or t_cfg.uart_port or config.uart_port
            puppet_id = t_cfg.puppet or config.puppet

            if repl is not None:
                client = repl
                console_client = console or (repl if isinstance(repl, IConsoleReader) else ConsoleReader(port=u_port or get_default_uart_port()))
            else:
                try:
                    puppet = resolve_puppet(
                        puppet_name_or_badge=puppet_id,
                        control_port=ctrl_port,
                        uart_port=u_port,
                        probe_badges=True,
                    )
                    client = ensure_daemon_running(
                        name=target_name,
                        control_port=puppet.control_port,
                        uart_port=puppet.uart_port,
                    )
                    console_client = client
                except RuntimeError as e:
                    _logger.warning("Hardware unavailable (%s); using offline mode for target '%s'", e, target_name)
                    client = ReplClient(port=ctrl_port or get_default_control_port())
                    console_client = ConsoleReader(port=u_port or get_default_uart_port())

            target_instance = TargetLoader.load_named_target(
                name=target_name,
                repl=client,
                console=console_client,
                target_path=t_cfg.path or target_path,
            )

            # Register tools with mandatory target prefix (e.g. 'jetson_read_target_console')
            register_tools(mcp, target=target_instance, prefix=target_name)

        return mcp

    # --- Mode 2: Clean bench (No targets defined) -> BaseTarget with NO prefix ---
    _logger.info("No targets defined; initializing BaseTarget core bridge...")
    ctrl_port = repl_port or config.port
    u_port = uart_port or config.uart_port
    puppet_id = config.puppet

    if repl is not None:
        client = repl
        console_client = console or (repl if isinstance(repl, IConsoleReader) else ConsoleReader(port=u_port or get_default_uart_port()))
    else:
        try:
            puppet = resolve_puppet(
                puppet_name_or_badge=puppet_id,
                control_port=ctrl_port,
                uart_port=u_port,
                probe_badges=True,
            )
            client = ensure_daemon_running(
                name="base",
                control_port=puppet.control_port,
                uart_port=puppet.uart_port,
            )
            console_client = client
        except RuntimeError as e:
            _logger.warning("Hardware unavailable (%s); using offline BaseTarget mode", e)
            client = ReplClient(port=ctrl_port or get_default_control_port())
            console_client = ConsoleReader(port=u_port or get_default_uart_port())

    base_instance = BaseTarget(repl=client, console=console_client)
    register_tools(mcp, target=base_instance, prefix=None)
    return mcp


def cmd_list_devices() -> None:
    """Print connected HW-Puppet hardware boards and matching target configuration."""
    print("Scanning connected HW-Puppet devices...")
    puppets = scan_hw_puppets(probe_badges=True)
    if not puppets:
        print("  (No HW-Puppet devices detected via USB)")
    else:
        print(f"Found {len(puppets)} connected HW-Puppet device(s):")
        for idx, p in enumerate(puppets, 1):
            badge_display = f"'{p.badge}'" if p.badge else "<not set>"
            ver_display = p.version or "unknown"
            print(f"  {idx}. Control: {p.control_port} | UART: {p.uart_port or 'none'}")
            print(f"     Badge  : {badge_display}")
            print(f"     Serial : {p.serial}")
            print(f"     Version: {ver_display}")

    cfg = load_bridge_config()
    if cfg.targets:
        print("\nConfigured Targets (.ae-hw-bridge/config.yml):")
        for name, t_cfg in cfg.targets.items():
            req_puppet = t_cfg.puppet or "<any>"
            req_port = t_cfg.port or "<auto>"
            print(f"  - Target '{name}': puppet={req_puppet}, port={req_port}")
    elif cfg.puppet or cfg.port:
        print("\nDefault Target (.ae-hw-bridge/config.yml):")
        print(f"  - puppet={cfg.puppet or '<any>'}, port={cfg.port or '<auto>'}")


def cmd_label_device(badge: str, port: Optional[str] = None) -> None:
    """Assign a persistent hardware badge name in ESP32-S3 NVS storage."""
    import serial
    import time

    target_port = port
    if not target_port:
        puppet = resolve_puppet(probe_badges=False)
        target_port = puppet.control_port

    print(f"Connecting to {target_port} to set badge '{badge}'...")
    try:
        ser = serial.Serial(target_port, 115200, timeout=1.5)
    except Exception as e:
        print(f"Error opening port {target_port}: {e}")
        print("Tip: If a daemon is running, stop it with: ae-hw-bridge daemon --stop")
        sys.exit(1)

    try:
        ser.reset_input_buffer()
        ser.write(b"\r\x03\x03\x01")
        time.sleep(0.1)

        raw_prompt = ser.read_until(b"raw REPL; CTRL-B to exit\r\n>")
        if not raw_prompt.endswith(b">"):
            ser.write(b"\r\x03\x03\x01")
            time.sleep(0.1)
            raw_prompt = ser.read_until(b">")
            if not raw_prompt.endswith(b">"):
                print("Error: Board did not respond to MicroPython raw REPL prompt.")
                sys.exit(1)

        code = f"import hw_puppet; hw_puppet.set_badge({repr(badge)});\n"
        ser.write(code.encode("utf-8") + b"\x04")
        header = ser.read(2)
        if header != b"OK":
            print(f"Error executing on board (response: {header})")
            sys.exit(1)

        print(f"[{target_port}] Persistent badge successfully set to: '{badge}'")
    finally:
        try:
            ser.write(b"\x02")
        except Exception:
            pass
        ser.close()


def _get_or_start_daemon(target: Optional[str] = None) -> DaemonIpcClient:
    """Connect to a running daemon or start one automatically if hardware is present."""
    cfg = load_bridge_config()
    eff_name = target

    if not eff_name:
        # Check if any daemon is already running
        # 1. Configured targets
        if cfg.targets:
            for t_name in cfg.targets.keys():
                candidate = DaemonIpcClient(socket_path=get_socket_path(t_name))
                if candidate.ping():
                    return candidate
        # 2. Base daemon
        base_candidate = DaemonIpcClient(socket_path=get_socket_path("base"))
        if base_candidate.ping():
            return base_candidate
        # 3. Any active lock file in /tmp
        for p in sorted(Path("/tmp").glob("ae-hw-bridge-*.lock")):
            if p.name.endswith("-spawn.lock"):
                continue
            m = re.match(r"^ae-hw-bridge-(.+)\.lock$", p.name)
            if m:
                cand = DaemonIpcClient(socket_path=get_socket_path(m.group(1)))
                if cand.ping():
                    return cand

        # No daemon running; choose default name to start
        if cfg.targets:
            eff_name = next(iter(cfg.targets.keys()))
        else:
            eff_name = "base"

    sock_path = get_socket_path(eff_name)
    client = DaemonIpcClient(socket_path=sock_path)
    if client.ping():
        return client

    # Daemon not answering; spawn in background
    print(f"AE-HW-BRIDGE daemon '{eff_name}' is not running. Starting...", file=sys.stderr)
    try:
        ctrl_port = None
        uart_port = None
        puppet_id = cfg.puppet
        if cfg.targets and eff_name in cfg.targets:
            t_cfg = cfg.targets[eff_name]
            ctrl_port = t_cfg.port or cfg.port
            uart_port = t_cfg.uart_port or cfg.uart_port
            puppet_id = t_cfg.puppet or cfg.puppet
        elif cfg.port or cfg.uart_port:
            ctrl_port = cfg.port
            uart_port = cfg.uart_port

        puppet = resolve_puppet(
            puppet_name_or_badge=puppet_id,
            control_port=ctrl_port,
            uart_port=uart_port,
            probe_badges=True,
        )
        client = ensure_daemon_running(
            name=eff_name,
            control_port=puppet.control_port,
            uart_port=puppet.uart_port,
        )
        return client
    except Exception as e:
        print(f"Error starting hardware daemon for '{eff_name}': {e}", file=sys.stderr)
        sys.exit(1)


def _run_builtin_terminal(pty_path: str) -> None:
    """Built-in raw interactive terminal loop connected to virtual UART PTY."""
    import select
    try:
        import termios
        import tty
    except ImportError:
        print(f"Interactive raw terminal not supported on this platform. Access {pty_path} using external terminal.", file=sys.stderr)
        return

    if not os.path.exists(pty_path):
        print(f"Error: Virtual UART PTY does not exist at {pty_path}", file=sys.stderr)
        sys.exit(1)

    try:
        fd = os.open(pty_path, os.O_RDWR | os.O_NOCTTY)
    except OSError as e:
        print(f"Error opening PTY {pty_path}: {e}", file=sys.stderr)
        sys.exit(1)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old_settings = termios.tcgetattr(stdin_fd)

    print(f"\r\n=== Connected to UART Console: {pty_path} ===")
    print("=== Press Ctrl-] or Ctrl-Q to exit ===\r\n", flush=True)

    try:
        tty.setraw(stdin_fd)
        while True:
            r, _, _ = select.select([stdin_fd, fd], [], [])
            if stdin_fd in r:
                data = os.read(stdin_fd, 1024)
                if not data:
                    break
                # Ctrl-] (\x1d) or Ctrl-Q (\x11)
                if b"\x1d" in data or b"\x11" in data:
                    break
                os.write(fd, data)
            if fd in r:
                try:
                    data = os.read(fd, 1024)
                except OSError:
                    break
                if not data:
                    break
                os.write(stdout_fd, data)
    finally:
        try:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
        print("\r\n=== Disconnected from UART Console ===\r\n", flush=True)


def cmd_console(args_list: list[str]) -> None:
    """Connect to target UART console (interactive terminal or log streaming)."""
    sub_parser = argparse.ArgumentParser(
        prog="ae-hw-bridge console",
        description="Connect to target UART console: interactive terminal or piped log stream",
    )
    sub_parser.add_argument("-t", "--target", default=None, help="Target name (default: auto)")
    sub_parser.add_argument(
        "-n", "--lines",
        type=int,
        default=None,
        help="Number of lines to output from ring buffer (e.g. -n 50)",
    )
    sub_parser.add_argument(
        "-f", "--follow",
        action="store_true",
        help="Stream live console output continuously",
    )
    sub_parser.add_argument(
        "--grep",
        default=None,
        help="Filter lines by regex or substring",
    )
    sub_parser.add_argument(
        "--builtin",
        action="store_true",
        help="Force built-in raw terminal instead of external tools (tio/picocom)",
    )
    args = sub_parser.parse_args(args_list)

    is_interactive_tty = sys.stdout.isatty() and sys.stdin.isatty()
    is_log_mode = (args.lines is not None) or args.follow or (not is_interactive_tty)

    client = _get_or_start_daemon(args.target)

    if not is_log_mode:
        # Interactive terminal mode
        pty_path = client.effective_pty_path or get_pty_path(args.target)
        if not os.path.exists(pty_path):
            print(f"Error: Virtual UART PTY does not exist at {pty_path}", file=sys.stderr)
            sys.exit(1)

        if not args.builtin:
            if shutil.which("tio"):
                os.execvp("tio", ["tio", pty_path])
            elif shutil.which("picocom"):
                os.execvp("picocom", ["picocom", "-b", "115200", pty_path])

        _run_builtin_terminal(pty_path)
        return

    # Non-interactive / tail / stream mode
    tail_count = args.lines if args.lines is not None else 50
    should_follow = args.follow or (not is_interactive_tty and args.lines is None)

    if tail_count > 0:
        lines = client.get_lines(tail_lines=tail_count, grep=args.grep)
        for line in lines:
            print(line, flush=True)

    if not should_follow:
        return

    # Continuously follow live console output
    total_count = client.get_total_lines_count()
    try:
        while True:
            time.sleep(0.1)
            current_count = client.get_total_lines_count()
            if current_count > total_count:
                new_lines = client.get_lines_since(total_count, max_lines=500)
                if new_lines:
                    total_count += len(new_lines)
                    for line in new_lines:
                        if args.grep and not re.search(args.grep, line):
                            continue
                        print(line, flush=True)
            elif current_count < total_count:
                total_count = current_count
    except KeyboardInterrupt:
        pass


def cmd_status(args_list: list[str]) -> None:
    """Display status of running AE-HW-BRIDGE daemons and connected hardware."""
    sub_parser = argparse.ArgumentParser(
        prog="ae-hw-bridge status",
        description="Display status of AE-HW-BRIDGE background daemons and hardware",
    )
    sub_parser.add_argument("-t", "--target", default=None, help="Target name to inspect")
    sub_parser.add_argument("--json", action="store_true", help="Output status in JSON format")
    args = sub_parser.parse_args(args_list)

    # Discover candidate targets
    targets_to_check: list[str] = []
    if args.target:
        targets_to_check.append(args.target)
    else:
        for p in sorted(Path("/tmp").glob("ae-hw-bridge-*.lock")):
            if p.name.endswith("-spawn.lock"):
                continue
            m = re.match(r"^ae-hw-bridge-(.+)\.lock$", p.name)
            if m:
                targets_to_check.append(m.group(1))
        if not targets_to_check:
            targets_to_check.append("base")

    results: list[dict[str, Any]] = []
    for t_name in targets_to_check:
        sock_path = get_socket_path(t_name)
        lock_path = get_lock_path(t_name)
        client = DaemonIpcClient(socket_path=sock_path)
        status = client.get_status()
        if not status or status.get("status") != "ok":
            continue

        pid = None
        if os.path.exists(lock_path):
            try:
                content = Path(lock_path).read_text(encoding="utf-8").strip()
                if content.isdigit():
                    pid = int(content)
            except Exception:
                pass

        effective_pty = status.get("pty_path") or get_pty_path(t_name)
        pty_target = None
        if effective_pty and os.path.islink(effective_pty):
            try:
                pty_target = os.readlink(effective_pty)
            except OSError:
                pass

        results.append({
            "target": status.get("name") or t_name,
            "pid": pid,
            "uptime_seconds": status.get("uptime"),
            "clients_count": status.get("clients_count"),
            "control_port": status.get("control_port"),
            "uart_port": status.get("uart_port"),
            "pty_path": effective_pty,
            "pty_target": pty_target,
            "version": status.get("package_version") or status.get("version"),
            "firmware_info": status.get("firmware_info"),
        })

    if args.json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No active AE-HW-BRIDGE daemons running.")
        print("\nConnected hardware check:")
        puppets = scan_hw_puppets(probe_badges=True)
        if not puppets:
            print("  (No HW-Puppet USB boards detected)")
        else:
            for p in puppets:
                b = f"'{p.badge}'" if p.badge else "<not set>"
                v = f" ({p.version})" if p.version else ""
                print(f"  - Control: {p.control_port} | UART: {p.uart_port or 'none'} | Badge: {b}{v}")
        return

    print("AE-HW-BRIDGE Daemon Status:")
    for r in results:
        print("=" * 60)
        print(f"Target:          {r['target']}")
        print(f"PID:             {r['pid'] or 'unknown'}")
        print(f"Uptime:          {r['uptime_seconds']}s")
        print(f"MCP Clients:     {r['clients_count']}")
        print(f"Control Port:    {r['control_port'] or 'none'} (MicroPython CDC0)")
        print(f"UART Port:       {r['uart_port'] or 'none'} (Target Console CDC1)")
        pty_disp = r['pty_path']
        if r['pty_target']:
            pty_disp += f" -> {r['pty_target']}"
        print(f"Virtual Console: {pty_disp}")
        fw = r.get("firmware_info") or {}
        badge = fw.get("badge") or "<unknown>"
        ver = fw.get("version") or "<unknown>"
        print(f"HW-Puppet:       badge='{badge}', firmware={ver}")
    print("=" * 60)


def cmd_stop(args_list: list[str]) -> None:
    """Stop running AE-HW-BRIDGE daemon(s) and release hardware ports."""
    sub_parser = argparse.ArgumentParser(
        prog="ae-hw-bridge stop",
        description="Stop AE-HW-BRIDGE background daemon(s) and release hardware ports",
    )
    sub_parser.add_argument("-t", "--target", default=None, help="Target name to stop")
    sub_parser.add_argument("--all", action="store_true", help="Stop all running daemons")
    args = sub_parser.parse_args(args_list)

    targets_to_stop: list[str] = []
    if args.target:
        targets_to_stop.append(args.target)
    else:
        for p in sorted(Path("/tmp").glob("ae-hw-bridge-*.lock")):
            if p.name.endswith("-spawn.lock"):
                continue
            m = re.match(r"^ae-hw-bridge-(.+)\.lock$", p.name)
            if m:
                targets_to_stop.append(m.group(1))
        if not targets_to_stop:
            targets_to_stop.append("base")

    stopped_count = 0
    for t_name in targets_to_stop:
        l_path = get_lock_path(t_name)
        s_path = get_socket_path(t_name)
        p_path = get_pty_path(t_name)
        stopped = stop_daemon(lock_path=l_path, socket_path=s_path, pty_path=p_path)
        if stopped:
            stopped_count += 1
            print(f"AE-HW-BRIDGE daemon '{t_name}' stopped. Hardware ports released.")

    # Also clean default pty if no more daemons running
    if stopped_count > 0 and (os.path.islink(DEFAULT_PTY_PATH) or os.path.exists(DEFAULT_PTY_PATH)):
        try:
            os.unlink(DEFAULT_PTY_PATH)
        except OSError:
            pass

    if stopped_count == 0:
        print("No active AE-HW-BRIDGE daemon found.")


def main() -> None:
    """CLI entrypoint for running the AE-HW-BRIDGE MCP server and utility subcommands."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handle subcommands: console, status, stop, list, label, daemon
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "console":
            cmd_console(sys.argv[2:])
            return
        elif cmd == "status":
            cmd_status(sys.argv[2:])
            return
        elif cmd == "stop":
            cmd_stop(sys.argv[2:])
            return
        elif cmd == "list":
            cmd_list_devices()
            return
        elif cmd == "label":
            sub_parser = argparse.ArgumentParser(prog="ae-hw-bridge label", description="Assign persistent badge to HW-Puppet board")
            sub_parser.add_argument("badge", help="Badge identifier to store in NVS (e.g. 'jetson', 'stm32')")
            sub_parser.add_argument("--port", default=None, help="CDC0 port (default: auto-detected)")
            sub_args = sub_parser.parse_args(sys.argv[2:])
            cmd_label_device(badge=sub_args.badge, port=sub_args.port)
            return
        elif cmd == "daemon":
            from ae_hw_bridge.daemon.server import main as daemon_main
            sys.argv = [sys.argv[0] + " daemon"] + sys.argv[2:]
            daemon_main()
            return

    parser = argparse.ArgumentParser(
        prog="ae-hw-bridge",
        description="AE-HW-BRIDGE FastMCP Server and hardware bridge CLI.",
        epilog="Subcommands:\n"
               "  console   Connect to target UART console (interactive terminal or log streaming)\n"
               "  status    Check running daemon status, ports, and virtual PTY\n"
               "  stop      Stop running daemon and release hardware ports\n"
               "  list      List connected HW-Puppet boards and targets\n"
               "  label     Set persistent hardware badge on HW-Puppet\n"
               "  daemon    Run background hardware daemon service directly\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--control-port",
        default=None,
        help="Serial port for CDC0 MicroPython raw REPL (default: auto-detected via sysfs / badge)",
    )
    parser.add_argument(
        "--uart-port",
        default=None,
        help="Serial port for CDC1 target UART console (default: auto-detected via sysfs / badge)",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to .ae-hw-bridge/config.yml configuration file",
    )
    parser.add_argument(
        "--target",
        "--target-dir",
        dest="target_path",
        default=os.getenv("AE_TARGET_DIR"),
        help="Path to custom target file or directory (default: auto-detected from .ae-hw-bridge/targets)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind for SSE transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind for SSE transport (default: 8000)",
    )
    args = parser.parse_args()

    mcp = create_server(
        repl_port=args.control_port,
        uart_port=args.uart_port,
        target_path=args.target_path,
        config_path=args.config_path,
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
