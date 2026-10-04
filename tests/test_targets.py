"""Unit tests for BaseTarget, @repl decorator, and TargetLoader."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from ae_hw_bridge.core.interfaces import (
    IReplClient,
    IConsoleReader,
    ExecutionResult,
)
from ae_hw_bridge.targets.base import BaseTarget, repl
from ae_hw_bridge.targets.loader import TargetLoader
from ae_hw_bridge.mcp.server import create_server


def test_base_target_core_methods() -> None:
    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    mock_repl.exec_code.return_value = ExecutionResult(ok=True, stdout="code_ok", stderr="", result="res")
    mock_console.get_lines.return_value = ["line 1", "line 2"]
    mock_console.get_total_lines_count.return_value = 2
    mock_console.get_lines_since.return_value = ["cmd out"]
    mock_console.wait_for.return_value = "[00:00:01] login: ok"

    target = BaseTarget(repl=mock_repl, console=mock_console)

    # 1. run_custom_code
    res_code = target.run_custom_code("print('hello')")
    assert res_code["ok"] is True
    assert res_code["result"] == "res"

    # 2. clear_target_console
    res_clear = target.clear_target_console()
    assert res_clear["status"] == "cleared"
    mock_console.clear.assert_called_once()

    # 3. read_target_console
    lines = target.read_target_console(tail_lines=2)
    assert len(lines) == 2

    # 4. send_target_command
    out = target.send_target_command("uname -a", wait_timeout=0.01)
    assert out == "cmd out"

    # 5. wait_for_console_pattern
    match = target.wait_for_console_pattern("login:")
    assert match["matched"] is True
    assert "login: ok" in match["line"]

    # 6. get_bridge_info
    mock_repl.exec_code.return_value = ExecutionResult(
        ok=True,
        stdout="{'version': '0.2.0', 'platform': 'ESP32-S3'}\n",
        stderr="",
    )
    info = target.get_bridge_info()
    assert info["bridge_version"] == "0.2.0"
    assert info["firmware"]["version"] == "0.2.0"
    assert info["firmware"]["platform"] == "ESP32-S3"
    assert "control" in info["ports"]
    assert "uart" in info["ports"]


def test_repl_decorator() -> None:
    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    mock_repl.exec_code.return_value = ExecutionResult(ok=True, stdout="pin toggled", stderr="")

    class CustomDeviceTarget(BaseTarget):
        @repl
        def pulse_pin(self, pin: int = 12, duration: float = 0.5):
            import time
            from machine import Pin
            p = Pin(pin, Pin.OUT, value=1)
            p.value(0)
            time.sleep(duration)
            p.value(1)

    device = CustomDeviceTarget(repl=mock_repl, console=mock_console)
    res = device.pulse_pin(pin=14, duration=0.2)

    assert res.ok is True
    assert res.stdout == "pin toggled"

    # Verify generated MicroPython code sent to exec_code
    call_args = mock_repl.exec_code.call_args[0][0]
    assert "pin = 14" in call_args
    assert "duration = 0.2" in call_args
    assert "p = Pin(pin, Pin.OUT, value=1)" in call_args


def test_target_loader_discovery_and_fallback() -> None:
    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    # 1. Fallback when path does not exist
    fallback_target = TargetLoader.load(
        repl=mock_repl,
        console=mock_console,
        target_path="/non/existent/path/12345",
    )
    assert type(fallback_target) is BaseTarget

    # 2. Fallback when directory is empty
    with tempfile.TemporaryDirectory() as empty_dir:
        t_empty = TargetLoader.load(
            repl=mock_repl,
            console=mock_console,
            target_path=empty_dir,
        )
        assert type(t_empty) is BaseTarget

    # 3. Discovery from reference Jetson example
    example_dir = Path(__file__).resolve().parent.parent / "examples" / "targets" / "jetson"
    assert example_dir.exists()

    jetson_target = TargetLoader.load(
        repl=mock_repl,
        console=mock_console,
        target_path=str(example_dir),
    )
    assert jetson_target.__class__.__name__ == "JetsonTarget"
    assert isinstance(jetson_target, BaseTarget)
    assert hasattr(jetson_target, "hardware_reset")
    assert hasattr(jetson_target, "full_reboot")
    assert hasattr(jetson_target, "enter_recovery")
    assert hasattr(jetson_target, "trigger_reset")


def test_create_server_with_custom_target() -> None:
    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    example_dir = Path(__file__).resolve().parent.parent / "examples" / "targets" / "jetson"

    server = create_server(
        repl=mock_repl,
        console=mock_console,
        target_path=str(example_dir),
    )

    tools = server._tool_manager._tools
    # Base tools must be present
    assert "send_target_command" in tools
    assert "read_target_console" in tools
    assert "clear_target_console" in tools
    assert "wait_for_console_pattern" in tools
    assert "run_custom_code" in tools

    # Scenarios tools must NOT be present (scenarios removed from engine)
    assert "run_scenario" not in tools
    assert "list_scenarios" not in tools

    # Custom high-level JetsonTarget tools must be present
    assert "hardware_reset" in tools
    assert "full_reboot" in tools
    assert "enter_recovery" in tools
    assert "check_recovery_mode" in tools
    assert "power_button" in tools
    assert "software_reboot" in tools
    assert "reboot_to_bootloader" in tools
    assert "wait_for_boot" in tools
    assert "wait_for_shell" in tools
    assert "login" in tools
    assert "exec_command_with_status" in tools
    assert "get_network_info" in tools
    assert "get_system_info" in tools
    assert "check_alive" in tools

    # Low-level @repl methods must NOT be exposed as agent MCP tools
    assert "trigger_reset" not in tools
    assert "trigger_recovery" not in tools
    assert "trigger_power_button" not in tools



def test_target_loader_discovers_from_ae_hw_bridge_targets_in_cwd(monkeypatch) -> None:
    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        monkeypatch.chdir(tmp_path)

        target_dir = tmp_path / ".ae-hw-bridge" / "targets" / "custom_board"
        target_dir.mkdir(parents=True)

        target_code = """
from ae_hw_bridge.targets.base import BaseTarget

class CustomBoardTarget(BaseTarget):
    def board_ping(self) -> str:
        return "pong"
"""
        (target_dir / "target.py").write_text(target_code)

        loaded_target = TargetLoader.load(
            repl=mock_repl,
            console=mock_console,
        )

        assert loaded_target.__class__.__name__ == "CustomBoardTarget"
        assert hasattr(loaded_target, "board_ping")
        assert loaded_target.board_ping() == "pong"

