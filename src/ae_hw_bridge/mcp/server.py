"""FastMCP server factory and CLI runner for AE-HW-BRIDGE."""

import os
import sys
import argparse
import logging
from pathlib import Path
from typing import Optional
from mcp.server.fastmcp import FastMCP

from ae_hw_bridge import __version__
from ae_hw_bridge.core.interfaces import IReplClient, IConsoleReader
from ae_hw_bridge.core.repl_client import ReplClient
from ae_hw_bridge.core.console_reader import ConsoleReader
from ae_hw_bridge.core.port_utils import get_default_control_port, get_default_uart_port
from ae_hw_bridge.targets.base import BaseTarget
from ae_hw_bridge.targets.loader import TargetLoader
from ae_hw_bridge.mcp.tools import register_tools

_logger = logging.getLogger(__name__)


def create_server(
    repl_port: Optional[str] = None,
    uart_port: Optional[str] = None,
    target_path: Optional[str] = None,
    start_console: bool = False,
    repl: Optional[IReplClient] = None,
    console: Optional[IConsoleReader] = None,
    target: Optional[BaseTarget] = None,
) -> FastMCP:
    """Create and configure a FastMCP server instance.

    Args:
        repl_port: Serial port for CDC0 MicroPython raw REPL.
        uart_port: Serial port for CDC1 target console UART bridge.
        target_path: Optional path to custom target module or directory.
        start_console: Whether to immediately launch the background console thread.
        repl: Optional pre-configured IReplClient instance.
        console: Optional pre-configured IConsoleReader instance.
        target: Optional pre-configured BaseTarget instance.
    """
    mcp = FastMCP("ae-hw-bridge")
    effective_repl_port = repl_port or get_default_control_port()
    effective_uart_port = uart_port or get_default_uart_port()
    repl_client = repl or ReplClient(port=effective_repl_port)
    console_reader = console or ConsoleReader(port=effective_uart_port)
    if start_console:
        console_reader.start()

    active_target = target or TargetLoader.load(
        repl=repl_client,
        console=console_reader,
        target_path=target_path,
    )
    register_tools(mcp, target=active_target)
    return mcp


def main() -> None:
    """CLI entrypoint for running the AE-HW-BRIDGE MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    default_ctrl = get_default_control_port()
    default_uart = get_default_uart_port()
    parser = argparse.ArgumentParser(description="AE-HW-BRIDGE FastMCP Server")
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--control-port",
        default=default_ctrl,
        help=f"Serial port for CDC0 MicroPython raw REPL (default: {default_ctrl})",
    )
    parser.add_argument(
        "--uart-port",
        default=default_uart,
        help=f"Serial port for CDC1 target UART console (default: {default_uart})",
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

    # Ensure background hardware daemon is running (auto-spawns if needed)
    from ae_hw_bridge.daemon.client import ensure_daemon_running

    daemon_client = ensure_daemon_running(
        control_port=args.control_port,
        uart_port=args.uart_port,
    )

    mcp = create_server(
        repl=daemon_client,
        console=daemon_client,
        target_path=args.target_path,
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
