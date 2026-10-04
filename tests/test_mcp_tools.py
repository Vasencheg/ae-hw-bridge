"""Unit tests for MCP tool registrations and dispatchers."""

import pytest
from unittest.mock import MagicMock
from mcp.server.fastmcp import FastMCP
from ae_hw_bridge.mcp.tools import register_tools
from ae_hw_bridge.core.interfaces import ExecutionResult


def test_register_tools_instantiation() -> None:
    mcp = FastMCP("test-server")
    mock_repl = MagicMock()
    mock_console = MagicMock()

    register_tools(mcp, mock_repl, mock_console)

    # Verify all expected tools registered in FastMCP
    tool_names = set(mcp._tool_manager._tools.keys())
    assert "run_custom_code" in tool_names
    assert "read_target_console" in tool_names
    assert "send_target_command" in tool_names
    assert "clear_target_console" in tool_names
    assert "wait_for_console_pattern" in tool_names
    assert "get_bridge_info" in tool_names
    assert "list_scenarios" not in tool_names
    assert "run_scenario" not in tool_names


def test_tool_invocations() -> None:
    mcp = FastMCP("test-server")
    mock_repl = MagicMock()
    mock_console = MagicMock()

    mock_repl.exec_code.return_value = ExecutionResult(ok=True, stdout="42", stderr="", result="42")
    mock_console.get_lines.return_value = ["[00:00:00.000] Kernel started"]
    mock_console.get_total_lines_count.return_value = 10
    mock_console.get_lines_since.return_value = ["Linux tegra 5.15 #1 SMP"]

    register_tools(mcp, mock_repl, mock_console)

    # Retrieve registered functions from FastMCP tool manager
    run_code_tool = mcp._tool_manager._tools["run_custom_code"].fn
    clear_tool = mcp._tool_manager._tools["clear_target_console"].fn
    read_console_tool = mcp._tool_manager._tools["read_target_console"].fn
    send_cmd_tool = mcp._tool_manager._tools["send_target_command"].fn

    # Test run_custom_code
    res_code = run_code_tool("print(42)")
    assert res_code["ok"] is True
    assert res_code["result"] == "42"
    mock_repl.exec_code.assert_called_once_with("print(42)", timeout=10.0)

    # Test clear_target_console
    res_clear = clear_tool()
    assert res_clear["status"] == "cleared"
    mock_console.clear.assert_called_once()

    # Test read_target_console with head_lines
    lines = read_console_tool(head_lines=5)
    assert len(lines) == 1
    assert "Kernel started" in lines[0]
    mock_console.get_lines.assert_called_with(tail_lines=None, head_lines=5, grep=None)

    # Test send_target_command returns delta
    out = send_cmd_tool("uname -a", wait_timeout=0.01)
    assert "Linux tegra" in out
    mock_console.write.assert_called_once_with(b"uname -a\r\n")
    mock_console.get_lines_since.assert_called_once_with(10)

    # Test wait_for_console_pattern
    wait_tool = mcp._tool_manager._tools["wait_for_console_pattern"].fn
    mock_console.wait_for.return_value = "[00:00:01.000] login: ubuntu"
    res_wait = wait_tool("login:", timeout=5.0)
    assert res_wait["matched"] is True
    assert "login: ubuntu" in res_wait["line"]
    mock_console.wait_for.assert_called_with(pattern="login:", timeout=5.0, check_history=True)
