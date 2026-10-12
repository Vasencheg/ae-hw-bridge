"""FastMCP server factory, target orchestrator, and CLI runner for AE-HW-BRIDGE."""

import os
import sys
import re
import json
import ast
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
            eff_daemon_name = puppet.badge or "base"
            client = ensure_daemon_running(
                name=eff_daemon_name,
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

    action_msg = f"set badge '{badge}'" if badge else "clear persistent badge"
    print(f"Connecting to {target_port} to {action_msg}...")
    try:
        ser = serial.Serial(target_port, 115200, timeout=1.5)
    except Exception as e:
        print(f"Error opening port {target_port}: {e}")
        print("Tip: If a daemon is running, stop it with: ae-hw-bridge stop")
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

        if not badge:
            print(f"[{target_port}] Persistent badge successfully cleared (erased from NVS).")
        else:
            print(f"[{target_port}] Persistent badge successfully set to: '{badge}'")
    finally:
        try:
            ser.write(b"\x02")
        except Exception:
            pass
        ser.close()


def _sync_daemon_target(client: DaemonIpcClient, target_name: str) -> None:
    """Ensure running daemon uses the target definition from the current project if available."""
    try:
        from ae_hw_bridge.targets.loader import TargetLoader
        discovered = TargetLoader.discover_target_files()
        project_target_path = str(discovered[target_name].resolve()) if target_name in discovered else None

        status = client.get_status()
        current_target_file = status.get("target_file")
        current_mtime = status.get("target_mtime", 0.0)
        current_source = status.get("target_source")

        should_reload = False
        if project_target_path:
            if current_target_file != project_target_path:
                should_reload = True
            elif Path(project_target_path).exists() and Path(project_target_path).stat().st_mtime > current_mtime:
                should_reload = True
        elif current_target_file and current_source == "project":
            # Running daemon had a project target loaded from another directory; fallback to default
            should_reload = True

        if should_reload:
            client.load_target(target_path=project_target_path, name=target_name)
    except Exception as e:
        _logger.debug("Failed to sync daemon target: %s", e)


def _get_or_start_daemon(target: Optional[str] = None) -> DaemonIpcClient:
    """Connect to a running daemon or start one automatically based on connected hardware."""
    cfg = load_bridge_config()
    eff_name = target
    matched_puppet: Optional[HWPuppetDevice] = None

    if not eff_name:
        # Check if any daemon is already running
        if cfg.targets:
            for t_name in cfg.targets.keys():
                candidate = DaemonIpcClient(socket_path=get_socket_path(t_name))
                if candidate.ping():
                    if candidate.is_compatible():
                        _sync_daemon_target(candidate, t_name)
                        return candidate
                    candidate.shutdown()
                    stop_daemon(
                        lock_path=get_lock_path(t_name),
                        socket_path=get_socket_path(t_name),
                        pty_path=get_pty_path(t_name),
                    )

        running_clients: list[tuple[str, DaemonIpcClient]] = []
        for p in sorted(Path("/tmp").glob("ae-hw-bridge-*.lock")):
            if p.name.endswith("-spawn.lock"):
                continue
            m = re.match(r"^ae-hw-bridge-(.+)\.lock$", p.name)
            if m:
                cand_name = m.group(1)
                cand = DaemonIpcClient(socket_path=get_socket_path(cand_name))
                if cand.ping():
                    if cand.is_compatible():
                        running_clients.append((cand_name, cand))
                    else:
                        cand.shutdown()
                        stop_daemon(
                            lock_path=get_lock_path(cand_name),
                            socket_path=get_socket_path(cand_name),
                            pty_path=get_pty_path(cand_name),
                        )

        if len(running_clients) == 1:
            _sync_daemon_target(running_clients[0][1], running_clients[0][0])
            return running_clients[0][1]
        elif len(running_clients) > 1:
            for c_name, cand in running_clients:
                if c_name == "base":
                    _sync_daemon_target(cand, c_name)
                    return cand
            _sync_daemon_target(running_clients[0][1], running_clients[0][0])
            return running_clients[0][1]

        # No daemon running: discover connected hardware
        puppets = scan_hw_puppets(probe_badges=True)
        if not puppets:
            print("Error: No HW-Puppet devices detected via USB.", file=sys.stderr)
            sys.exit(1)
        elif len(puppets) == 1:
            matched_puppet = puppets[0]
            eff_name = matched_puppet.badge or "base"
        else:
            badges_str = ", ".join(f"'{p.badge}'" if p.badge else f"<{p.control_port}>" for p in puppets)
            print(
                f"Error: Multiple HW-Puppet devices connected ({badges_str}). "
                f"Please specify target with -t <name>.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        # User specified an explicit target name: check if daemon is running
        sock_path = get_socket_path(eff_name)
        client = DaemonIpcClient(socket_path=sock_path)
        if client.ping():
            if client.is_compatible():
                _sync_daemon_target(client, eff_name)
                return client
            client.shutdown()
            stop_daemon(
                lock_path=get_lock_path(eff_name),
                socket_path=get_socket_path(eff_name),
                pty_path=get_pty_path(eff_name),
            )

        # Daemon not answering; validate target against connected hardware badges
        puppets = scan_hw_puppets(probe_badges=True)

        # 1. Match by hardware badge or serial or port
        for p in puppets:
            if (
                p.badge == eff_name
                or p.serial == eff_name
                or p.control_port == eff_name
                or os.path.basename(p.control_port) == eff_name
                or (p.uart_port and (p.uart_port == eff_name or os.path.basename(p.uart_port) == eff_name))
            ):
                matched_puppet = p
                eff_name = matched_puppet.badge or eff_name.replace("/dev/", "").replace("/", "_")
                break

        # 2. Match unbadged single device if requested 'base' or 'default'
        if not matched_puppet and eff_name in ("base", "default"):
            unbadged = [p for p in puppets if not p.badge]
            if len(unbadged) == 1:
                matched_puppet = unbadged[0]
                eff_name = "base"

        # 3. Match from local config.yml if user configured an explicit target alias
        if not matched_puppet and cfg.targets and eff_name in cfg.targets:
            t_cfg = cfg.targets[eff_name]
            p_badge = t_cfg.puppet
            for p in puppets:
                if p.badge == p_badge or p.serial == p_badge:
                    matched_puppet = p
                    break

        if not matched_puppet:
            print(f"Error: Target '{eff_name}' not found on connected hardware.", file=sys.stderr)
            if puppets:
                print("Connected hardware:", file=sys.stderr)
                for p in puppets:
                    b = f"'{p.badge}'" if p.badge else "<not set>"
                    print(f"  - Control: {p.control_port} | UART: {p.uart_port or 'none'} | Badge: {b}", file=sys.stderr)
                if any(not p.badge for p in puppets):
                    print("Tip: Assign a badge to the connected board using: ae-hw-bridge label <name>", file=sys.stderr)
            else:
                print("  (No HW-Puppet USB boards detected)", file=sys.stderr)
            sys.exit(1)

    # Daemon not answering; spawn in background using matched_puppet
    _logger.debug("AE-HW-BRIDGE daemon '%s' is not running. Starting...", eff_name)
    try:
        from ae_hw_bridge.targets.loader import TargetLoader
        discovered = TargetLoader.discover_target_files()
        init_target_path = str(discovered[eff_name].resolve()) if eff_name in discovered else None

        spawn_kwargs: dict[str, Any] = {
            "name": eff_name,
            "control_port": matched_puppet.control_port,
            "uart_port": matched_puppet.uart_port,
        }
        if init_target_path:
            spawn_kwargs["target_path"] = init_target_path

        client = ensure_daemon_running(**spawn_kwargs)
        _sync_daemon_target(client, eff_name)
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
    old_stdin_settings = termios.tcgetattr(stdin_fd)
    old_pty_settings = None

    print(f"\r\n=== Connected to UART Console: {pty_path} ===")
    print("=== Press Ctrl-] or Ctrl-Q to exit ===\r\n", flush=True)

    try:
        # Put user's stdin into raw mode
        tty.setraw(stdin_fd)
        # Put PTY slave into raw mode (disable canonical buffering, local echo, flow control locks)
        try:
            old_pty_settings = termios.tcgetattr(fd)
            tty.setraw(fd)
        except Exception:
            pass

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
                formatted = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                os.write(stdout_fd, formatted)
    finally:
        try:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_stdin_settings)
        except Exception:
            pass
        if old_pty_settings is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_pty_settings)
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

    try:
        total_in_buffer = int(client.get_total_lines_count())
    except (TypeError, ValueError):
        total_in_buffer = 1

    if total_in_buffer == 0 and not should_follow:
        print("[ae-hw-bridge] Console buffer is empty (target has not produced any output yet).", file=sys.stderr)
        print("[ae-hw-bridge] Tip: Run 'ae-hw-bridge console' to open an interactive session and wake up the target prompt.", file=sys.stderr)
        return

    if tail_count > 0 and total_in_buffer > 0:
        lines = client.get_lines(tail_lines=tail_count, grep=args.grep)
        if not lines and args.grep and not should_follow and sys.stderr.isatty():
            print(f"[ae-hw-bridge] No buffered lines matched pattern '{args.grep}'.", file=sys.stderr)
        for line in lines:
            print(line, flush=True)

    if not should_follow:
        return

    # Continuously follow live console output
    if sys.stderr.isatty():
        if total_in_buffer == 0:
            print("[ae-hw-bridge] Console buffer is empty. Streaming live console output (Ctrl-C to stop)...", file=sys.stderr)
        elif args.grep:
            print(f"[ae-hw-bridge] Streaming live console output matching '{args.grep}' (Ctrl-C to stop)...", file=sys.stderr)
        else:
            print("[ae-hw-bridge] Streaming live console output (Ctrl-C to stop)...", file=sys.stderr)

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


def cmd_actions(args_list: list[str]) -> None:
    """List available target actions exposed by hardware bridge daemon."""
    sub_parser = argparse.ArgumentParser(
        prog="ae-hw-bridge actions",
        description="List available target actions exposed by hardware bridge daemon",
    )
    sub_parser.add_argument("-t", "--target", default=None, help="Target name (default: auto)")
    sub_parser.add_argument("--json", action="store_true", help="Output actions in JSON format")
    args = sub_parser.parse_args(args_list)

    client = _get_or_start_daemon(args.target)
    actions = client.list_actions()

    if args.json:
        print(json.dumps(actions, indent=2))
        return

    target_name = args.target or getattr(client, "name", "target")
    # Identify target class and source
    source_type = None
    target_class = None
    for a in actions:
        if not a.get("is_core") and a.get("source"):
            source_type = a["source"]
            target_class = a.get("defined_in")
            break

    source_desc = f"source: {source_type}" if source_type else "source: core BaseTarget"
    if target_class:
        source_desc += f", class: {target_class}"

    print(f"\nTarget: '{target_name}' ({source_desc})")
    print("=" * 68)
    for a in actions:
        params_str = ""
        if a.get("parameters"):
            p_parts = []
            for p in a["parameters"]:
                if p.get("has_default"):
                    p_parts.append(f"{p['name']}={p['default']}")
                else:
                    p_parts.append(p["name"])
            params_str = f" ({', '.join(p_parts)})"
        name_str = f"{a['name']}{params_str}"
        summary = a.get("summary") or ""
        tags = []
        if a.get("source") and a.get("source") != "core":
            tags.append(a["source"])
        elif a.get("is_core"):
            tags.append("core")
        if a.get("is_repl"):
            tags.append("@repl")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        print(f"  {name_str:<36} {summary}{tag_str}")
    print("Run via CLI:     ae-hw-bridge call <action> [args...]\n")


def cmd_call(args_list: list[str]) -> None:
    """Invoke a target action dynamically via the hardware bridge daemon."""
    sub_parser = argparse.ArgumentParser(
        prog="ae-hw-bridge call",
        description="Invoke a target action (e.g. reset, power_cycle, recovery_mode, exec, send)",
    )
    sub_parser.add_argument("-t", "--target", default=None, help="Target name (default: auto)")
    sub_parser.add_argument("--json", action="store_true", help="Output result in structured JSON format")
    sub_parser.add_argument("action", help="Name of the action to invoke")
    sub_parser.add_argument("action_args", nargs="*", help="Positional arguments and key=value kwargs")
    args = sub_parser.parse_args(args_list)

    client = _get_or_start_daemon(args.target)
    target_name = args.target or getattr(client, "name", None) or client.get_status().get("name") or "target"

    pos_args: list[Any] = []
    kw_args: dict[str, Any] = {}
    for tok in args.action_args:
        if "=" in tok:
            k, v = tok.split("=", 1)
            try:
                kw_args[k] = ast.literal_eval(v)
            except Exception:
                kw_args[k] = v
        else:
            try:
                pos_args.append(ast.literal_eval(tok))
            except Exception:
                pos_args.append(tok)

    # Query actions for origin attribution and alias resolution
    actions = client.list_actions()
    matched_action = None
    for a in actions:
        if a.get("name") == args.action:
            matched_action = a
            break

    if not matched_action:
        aliases = getattr(BaseTarget, "ALIASES", {})
        if args.action in aliases:
            for alt in aliases[args.action]:
                for a in actions:
                    if a.get("name") == alt:
                        matched_action = a
                        break
                if matched_action:
                    break

    origin_tags = []
    if matched_action:
        src = matched_action.get("source") or "custom"
        t_file = matched_action.get("target_file")
        if src == "project" and t_file:
            origin_tags.append(f"project: {Path(t_file).name}")
        elif src:
            origin_tags.append(src)
        if matched_action.get("is_repl"):
            origin_tags.append("@repl")
    else:
        origin_tags.append("unknown")

    tag_str = f" [{', '.join(origin_tags)}]" if origin_tags else ""
    alias_note = f" (via {matched_action['name']})" if matched_action and matched_action.get("name") != args.action else ""

    try:
        res = client.call_action(args.action, *pos_args, **kw_args)
    except Exception as e:
        print(f"Error executing action '{args.action}' on target '{target_name}'{tag_str}: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        if isinstance(res, (dict, list)):
            print(json.dumps(res, indent=2))
        else:
            print(json.dumps({"result": res}, indent=2))
        return

    # Clean human-readable output
    header = f"Target '{target_name}'{tag_str}: {args.action}{alias_note}"
    if isinstance(res, dict):
        if "ok" in res:
            if res.get("ok"):
                print(f"{header} -> OK")
                if res.get("stdout"):
                    sys.stdout.write(res["stdout"] if res["stdout"].endswith("\n") else f"{res['stdout']}\n")
                if res.get("result") is not None and res.get("result") != res.get("stdout"):
                    print(f"Result: {res['result']}")
            else:
                print(f"{header} -> FAILED", file=sys.stderr)
                err = res.get("stderr") or res.get("error") or "Unknown error"
                print(f"Error: {err}", file=sys.stderr)
                sys.exit(1)
        elif len(res) == 1 and "status" in res:
            print(f"{header} -> {str(res['status']).upper()}")
        else:
            print(f"{header} -> OK")
            for k, v in res.items():
                print(f"  {k}: {v}")
    elif isinstance(res, str):
        print(f"{header}:")
        print(res.rstrip())
    elif isinstance(res, list):
        print(f"{header}:")
        for item in res:
            print(f"  {item}")
    elif res is None or res is True:
        print(f"{header} -> OK")
    elif res is False:
        print(f"{header} -> FAILED", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"{header} -> {res}")


def cmd_exec(args_list: list[str]) -> None:
    """Execute arbitrary MicroPython code directly on ESP32-S3 via CDC0 Raw REPL."""
    sub_parser = argparse.ArgumentParser(
        prog="ae-hw-bridge exec",
        description="Execute MicroPython code on ESP32-S3 raw REPL",
    )
    sub_parser.add_argument("-t", "--target", default=None, help="Target name (default: auto)")
    sub_parser.add_argument("code", help="MicroPython code string to execute")
    sub_parser.add_argument("--timeout", type=float, default=10.0, help="Execution timeout in seconds")
    args = sub_parser.parse_args(args_list)

    client = _get_or_start_daemon(args.target)
    try:
        res = client.target_exec(args.code, timeout=args.timeout)
        if res.get("stdout"):
            sys.stdout.write(res["stdout"])
        if res.get("stderr"):
            sys.stderr.write(res["stderr"])
        if not res.get("ok"):
            sys.exit(1)
    except Exception as e:
        print(f"Error executing MicroPython code: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_send(args_list: list[str]) -> None:
    """Send shell command to target UART console (CDC1) and print output."""
    sub_parser = argparse.ArgumentParser(
        prog="ae-hw-bridge send",
        description="Send shell command to target UART console and display response",
    )
    sub_parser.add_argument("-t", "--target", default=None, help="Target name (default: auto)")
    sub_parser.add_argument("command", help="Shell command to send (e.g. 'uname -a')")
    sub_parser.add_argument("--timeout", type=float, default=5.0, help="Wait timeout in seconds")
    args = sub_parser.parse_args(args_list)

    client = _get_or_start_daemon(args.target)
    try:
        out = client.target_send(args.command, wait_timeout=args.timeout)
        print(out)
    except Exception as e:
        print(f"Error sending command to target UART: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """CLI entrypoint for running the AE-HW-BRIDGE MCP server and utility subcommands."""
    log_level = logging.DEBUG if os.getenv("AE_DEBUG") else logging.WARNING
    logging.basicConfig(
        level=log_level,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handle subcommands: console, status, stop, actions, call, exec, send, list, label, daemon
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
        elif cmd == "actions":
            cmd_actions(sys.argv[2:])
            return
        elif cmd == "call":
            cmd_call(sys.argv[2:])
            return
        elif cmd == "exec":
            cmd_exec(sys.argv[2:])
            return
        elif cmd == "send":
            cmd_send(sys.argv[2:])
            return
        elif cmd == "list":
            cmd_list_devices()
            return
        elif cmd == "label":
            sub_parser = argparse.ArgumentParser(
                prog="ae-hw-bridge label",
                description="Assign or clear persistent badge on HW-Puppet board",
            )
            sub_parser.add_argument("badge", nargs="?", default=None, help="Badge identifier to store in NVS (e.g. 'jetson', 'stm32')")
            sub_parser.add_argument("--clear", "--delete", action="store_true", help="Erase badge from NVS storage")
            sub_parser.add_argument("--port", default=None, help="CDC0 port (default: auto-detected)")
            sub_args = sub_parser.parse_args(sys.argv[2:])
            if sub_args.clear:
                badge_val = ""
            elif sub_args.badge is not None:
                badge_val = sub_args.badge
            else:
                sub_parser.error("Specify a badge name (e.g. 'ae-hw-bridge label jetson') or use --clear to erase the badge.")
            cmd_label_device(badge=badge_val, port=sub_args.port)
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
               "  actions   List available target actions (reset, power_cycle, exec, send, etc.)\n"
               "  call      Invoke a target action dynamically (e.g. ae-hw-bridge call reset)\n"
               "  exec      Execute MicroPython code on ESP32-S3 raw REPL\n"
               "  send      Send shell command to target UART console\n"
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
