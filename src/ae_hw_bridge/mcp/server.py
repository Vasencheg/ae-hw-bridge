"""FastMCP server factory, target orchestrator, and CLI runner for AE-HW-BRIDGE."""

import os
import sys
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
from ae_hw_bridge.daemon.client import ensure_daemon_running
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

            puppet = resolve_puppet(
                puppet_name_or_badge=puppet_id,
                control_port=ctrl_port,
                uart_port=u_port,
                probe_badges=True,
            )

            client = repl if repl else ensure_daemon_running(
                name=target_name,
                control_port=puppet.control_port,
                uart_port=puppet.uart_port,
            )

            target_instance = TargetLoader.load_named_target(
                name=target_name,
                repl=client,
                console=client,
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

    puppet = resolve_puppet(
        puppet_name_or_badge=puppet_id,
        control_port=ctrl_port,
        uart_port=u_port,
        probe_badges=True,
    )

    client = repl if repl else ensure_daemon_running(
        name="base",
        control_port=puppet.control_port,
        uart_port=puppet.uart_port,
    )

    base_instance = BaseTarget(repl=client, console=client)
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


def main() -> None:
    """CLI entrypoint for running the AE-HW-BRIDGE MCP server and utility subcommands."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handle subcommands: list, label
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        cmd_list_devices()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "label":
        sub_parser = argparse.ArgumentParser(prog="ae-hw-bridge label", description="Assign persistent badge to HW-Puppet board")
        sub_parser.add_argument("badge", help="Badge identifier to store in NVS (e.g. 'jetson', 'stm32')")
        sub_parser.add_argument("--port", default=None, help="CDC0 port (default: auto-detected)")
        sub_args = sub_parser.parse_args(sys.argv[2:])
        cmd_label_device(badge=sub_args.badge, port=sub_args.port)
        return

    parser = argparse.ArgumentParser(description="AE-HW-BRIDGE FastMCP Server")
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
