"""Tests for port resolution and HW-PUPPET connection verification."""

import os
from unittest.mock import patch
import pytest

from ae_hw_bridge.core.port_utils import (
    get_default_control_port,
    get_default_uart_port,
    is_hw_puppet_connected,
    check_hw_puppet_connection,
)
from ae_hw_bridge.core.repl_client import ReplClient
from ae_hw_bridge.core.console_reader import ConsoleReader


def test_get_default_ports_from_env(monkeypatch):
    monkeypatch.setenv("HW_PUPPET_CONTROL_PORT", "/dev/custom-control")
    monkeypatch.setenv("HW_PUPPET_UART_PORT", "/dev/custom-uart")
    assert get_default_control_port() == "/dev/custom-control"
    assert get_default_uart_port() == "/dev/custom-uart"


def test_is_hw_puppet_connected_true(tmp_path):
    ctrl = tmp_path / "hw-puppet-control"
    uart = tmp_path / "hw-puppet-uart"
    ctrl.touch()
    uart.touch()

    assert is_hw_puppet_connected(str(ctrl), str(uart)) is True


def test_is_hw_puppet_connected_false(tmp_path):
    ctrl = tmp_path / "nonexistent-control"
    uart = tmp_path / "nonexistent-uart"

    assert is_hw_puppet_connected(str(ctrl), str(uart)) is False


def test_check_hw_puppet_connection_success(tmp_path):
    ctrl = tmp_path / "hw-puppet-control"
    uart = tmp_path / "hw-puppet-uart"
    ctrl.touch()
    uart.touch()

    # Should not raise
    check_hw_puppet_connection(str(ctrl), str(uart))


def test_check_hw_puppet_connection_raises(tmp_path):
    ctrl = tmp_path / "missing-control"
    uart = tmp_path / "missing-uart"

    with pytest.raises(RuntimeError, match="HW-PUPPET is not connected"):
        check_hw_puppet_connection(str(ctrl), str(uart))


def test_clients_use_default_ports(tmp_path, monkeypatch):
    ctrl = tmp_path / "test-ctrl"
    uart = tmp_path / "test-uart"
    ctrl.touch()
    uart.touch()

    monkeypatch.setenv("HW_PUPPET_CONTROL_PORT", str(ctrl))
    monkeypatch.setenv("HW_PUPPET_UART_PORT", str(uart))

    repl = ReplClient()
    console = ConsoleReader()

    assert repl.port == str(ctrl)
    assert console.port == str(uart)


def test_check_firmware_handshake_success():
    from unittest.mock import MagicMock
    from ae_hw_bridge.core.interfaces import ExecutionResult
    from ae_hw_bridge.core.port_utils import check_firmware_handshake

    mock_repl = MagicMock()
    mock_repl.exec_code.return_value = ExecutionResult(
        ok=True,
        stdout="{'version': '0.2.0', 'platform': 'ESP32-S3', 'build_date': 'Oct 4 2026'}\n",
        stderr="",
    )

    info = check_firmware_handshake(mock_repl)
    assert info["version"] == "0.2.0"
    assert info["platform"] == "ESP32-S3"


def test_check_firmware_handshake_failure():
    from unittest.mock import MagicMock
    from ae_hw_bridge.core.interfaces import ExecutionResult
    from ae_hw_bridge.core.port_utils import check_firmware_handshake

    mock_repl = MagicMock()
    mock_repl.exec_code.return_value = ExecutionResult(
        ok=False,
        stdout="",
        stderr="ImportError: no module named 'hw_puppet'",
    )

    with pytest.raises(RuntimeError, match="HW-Puppet firmware handshake failed"):
        check_firmware_handshake(mock_repl)
