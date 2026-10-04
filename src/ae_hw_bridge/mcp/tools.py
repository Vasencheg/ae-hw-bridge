"""FastMCP tool registration from BaseTarget and custom target modules."""

import logging
from typing import Optional
from mcp.server.fastmcp import FastMCP
from ae_hw_bridge.core.interfaces import IReplClient, IConsoleReader
from ae_hw_bridge.targets.base import BaseTarget

_logger = logging.getLogger(__name__)


def register_tools(
    mcp: FastMCP,
    repl: Optional[IReplClient] = None,
    console: Optional[IConsoleReader] = None,
    target: Optional[BaseTarget] = None,
) -> BaseTarget:
    """Register all operations from a target module (or BaseTarget) onto the FastMCP server instance.

    Args:
        mcp: FastMCP server instance.
        repl: Optional MicroPython REPL client (used if target is None).
        console: Optional console reader (used if target is None).
        target: Optional instantiated BaseTarget or subclass.

    Returns:
        The active target instance registered on the server.
    """
    if target is None:
        if repl is None or console is None:
            raise ValueError("Either 'target' or both ('repl', 'console') must be provided")
        target = BaseTarget(repl=repl, console=console)

    # Discover and register all public callable methods of the target
    reserved_attributes = {"repl", "console"}
    registered_count = 0

    for name in dir(target):
        if name.startswith("_") or name in reserved_attributes:
            continue
        attr = getattr(target, name)
        if callable(attr):
            # Do NOT register @repl methods as agent MCP tools:
            # @repl methods execute MicroPython directly in ESP32 RAM (pin toggling, raw REPL)
            # and are internal hardware drivers/primitives used by target methods, not agent tools.
            if getattr(attr, "_is_repl_method", False):
                _logger.debug("Skipping internal @repl method '%s' from MCP tool registration", name)
                continue
            mcp.add_tool(attr)
            registered_count += 1
            _logger.debug("Registered MCP tool '%s' from target %s", name, target.__class__.__name__)

    _logger.info(
        "Registered %d tools from target %s onto FastMCP server '%s'",
        registered_count,
        target.__class__.__name__,
        mcp.name,
    )
    return target
