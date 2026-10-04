"""End-to-End integration tests for FastMCP server and core operations."""

import pytest
from unittest.mock import MagicMock
from ae_hw_bridge.mcp.server import create_server
from ae_hw_bridge.core.interfaces import ExecutionResult


def test_core_tools_registration_in_server() -> None:
    """Verify create_server registers the 5 foundational tools."""
    server = create_server(start_console=False)
    tools = server._tool_manager._tools

    assert "run_custom_code" in tools
    assert "read_target_console" in tools
    assert "send_target_command" in tools
    assert "clear_target_console" in tools
    assert "wait_for_console_pattern" in tools

    # Scenarios removed from engine
    assert "list_scenarios" not in tools
    assert "run_scenario" not in tools


def test_custom_code_execution_pipeline() -> None:
    """Verify run_custom_code executes MicroPython code through REPL client."""
    mock_repl = MagicMock()
    mock_repl.exec_code.return_value = ExecutionResult(
        ok=True,
        stdout="=== Code executed ===",
        stderr="",
        result="=== Code executed ===",
    )

    server = create_server(repl=mock_repl, start_console=False)
    run_custom_code_fn = server._tool_manager._tools["run_custom_code"].fn
    res = run_custom_code_fn("import machine; print(machine.unique_id())", timeout=5.0)

    assert res["ok"] is True
    assert "Code executed" in res["stdout"]
    mock_repl.exec_code.assert_called_once_with(
        "import machine; print(machine.unique_id())",
        timeout=5.0,
    )
