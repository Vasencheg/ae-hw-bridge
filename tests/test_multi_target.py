"""Unit tests for multi-target FastMCP server creation and tool prefixing."""

import pytest
from unittest.mock import MagicMock, patch
from ae_hw_bridge.core.interfaces import IReplClient, IConsoleReader
from ae_hw_bridge.core.discovery import HWPuppetDevice
from ae_hw_bridge.targets.base import BaseTarget
from ae_hw_bridge.mcp.server import create_server
from ae_hw_bridge.core.config import BridgeConfig, TargetConfig


@patch("ae_hw_bridge.mcp.server.ensure_daemon_running")
@patch("ae_hw_bridge.mcp.server.resolve_puppet")
@patch("ae_hw_bridge.mcp.server.load_bridge_config")
def test_create_server_multi_target_prefixes(
    mock_load_cfg,
    mock_resolve_puppet,
    mock_ensure_daemon,
) -> None:
    # 2 targets configured in config.yml
    mock_load_cfg.return_value = BridgeConfig(
        targets={
            "jetson": TargetConfig(name="jetson", puppet="jetson-desk"),
            "stm32": TargetConfig(name="stm32", puppet="stm32-tester"),
        }
    )

    mock_resolve_puppet.side_effect = lambda puppet_name_or_badge, **kw: HWPuppetDevice(
        serial="ser_" + str(puppet_name_or_badge),
        control_port=f"/dev/ctrl_{puppet_name_or_badge}",
        uart_port=f"/dev/uart_{puppet_name_or_badge}",
        sysfs_path="",
        badge=puppet_name_or_badge,
    )

    mock_client = MagicMock(spec=IReplClient)
    mock_ensure_daemon.return_value = mock_client

    server = create_server()
    tools = server._tool_manager._tools

    # Both targets must be registered with their respective prefixes
    assert "jetson_send_target_command" in tools
    assert "jetson_read_target_console" in tools
    assert "jetson_clear_target_console" in tools

    assert "stm32_send_target_command" in tools
    assert "stm32_read_target_console" in tools
    assert "stm32_clear_target_console" in tools

    # Tools WITHOUT prefix must NOT exist in multi-target mode
    assert "send_target_command" not in tools
    assert "read_target_console" not in tools


@patch("ae_hw_bridge.mcp.server.ensure_daemon_running")
@patch("ae_hw_bridge.mcp.server.resolve_puppet")
@patch("ae_hw_bridge.mcp.server.load_bridge_config")
@patch("ae_hw_bridge.targets.loader.TargetLoader.discover_target_files")
def test_create_server_clean_bench_no_prefix(
    mock_discover_targets,
    mock_load_cfg,
    mock_resolve_puppet,
    mock_ensure_daemon,
) -> None:
    # Clean bench: no targets in config and no targets on disk
    mock_load_cfg.return_value = BridgeConfig()
    mock_discover_targets.return_value = {}

    mock_resolve_puppet.return_value = HWPuppetDevice(
        serial="single_ser",
        control_port="/dev/ttyACM1",
        uart_port="/dev/ttyACM3",
        sysfs_path="",
    )

    mock_client = MagicMock(spec=IReplClient)
    mock_ensure_daemon.return_value = mock_client

    server = create_server()
    tools = server._tool_manager._tools

    # In clean bench mode (BaseTarget), tools must have NO prefix
    assert "send_target_command" in tools
    assert "read_target_console" in tools
    assert "clear_target_console" in tools
    assert "wait_for_console_pattern" in tools
