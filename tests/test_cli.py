"""Unit tests for ae-hw-bridge CLI subcommands (status, stop, console)."""

import os
import sys
import io
import tempfile
import pytest
from unittest.mock import MagicMock, patch

from ae_hw_bridge.mcp.server import cmd_status, cmd_stop, cmd_console, _get_or_start_daemon
from ae_hw_bridge.core.interfaces import IReplClient, IConsoleReader
from ae_hw_bridge.core.discovery import HWPuppetDevice
from ae_hw_bridge.daemon.server import DaemonServer


def test_cmd_status_no_daemons(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("ae_hw_bridge.mcp.server.scan_hw_puppets", return_value=[]), \
         patch("ae_hw_bridge.daemon.client.DaemonIpcClient.get_status", return_value=None):
        cmd_status([])
    captured = capsys.readouterr()
    assert "No active AE-HW-BRIDGE daemons running" in captured.out


def test_cmd_status_with_active_daemon(capsys: pytest.CaptureFixture[str]) -> None:
    temp_dir = tempfile.mkdtemp()
    sock_path = "/tmp/ae-hw-bridge-testcli.sock"
    lock_path = "/tmp/ae-hw-bridge-testcli.lock"
    pty_link = "/tmp/ae-hw-bridge-testcli-uart"

    mock_repl = MagicMock(spec=IReplClient)
    mock_repl.port = "/dev/ttyACM0"
    mock_console = MagicMock(spec=IConsoleReader)
    mock_console.port = "/dev/ttyACM1"
    mock_console.effective_pty_path = pty_link

    server = DaemonServer(
        repl=mock_repl,
        console=mock_console,
        socket_path=sock_path,
        lock_path=lock_path,
        pty_path=pty_link,
        name="testcli",
        firmware_info={"badge": "clibadge", "version": "0.2.0"},
    )
    server.start()

    try:
        cmd_status(["-t", "testcli"])
        captured = capsys.readouterr()
        assert "Target:          testcli" in captured.out
        assert "/dev/ttyACM0" in captured.out
        assert "/dev/ttyACM1" in captured.out
        assert pty_link in captured.out
        assert "clibadge" in captured.out

        # Test JSON format
        cmd_status(["-t", "testcli", "--json"])
        captured_json = capsys.readouterr()
        assert '"target": "testcli"' in captured_json.out
    finally:
        server.stop()
        for p in (sock_path, lock_path, pty_link):
            if os.path.islink(p) or os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        if os.path.exists(temp_dir):
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


def test_cmd_stop(capsys: pytest.CaptureFixture[str]) -> None:
    sock_path = "/tmp/ae-hw-bridge-teststop.sock"
    lock_path = "/tmp/ae-hw-bridge-teststop.lock"
    pty_link = "/tmp/ae-hw-bridge-teststop-uart"

    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    server = DaemonServer(
        repl=mock_repl,
        console=mock_console,
        socket_path=sock_path,
        lock_path=lock_path,
        pty_path=pty_link,
        name="teststop",
    )
    server.start()

    try:
        cmd_stop(["-t", "teststop"])
        captured = capsys.readouterr()
        assert "AE-HW-BRIDGE daemon 'teststop' stopped" in captured.out
        assert not os.path.exists(sock_path)
        assert not os.path.exists(lock_path)
    finally:
        server.stop()
        for p in (sock_path, lock_path, pty_link):
            if os.path.islink(p) or os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def test_cmd_console_tail(capsys: pytest.CaptureFixture[str]) -> None:
    mock_client = MagicMock()
    mock_client.get_lines.return_value = ["line 1", "line 2", "line 3"]

    with patch("ae_hw_bridge.mcp.server._get_or_start_daemon", return_value=mock_client):
        cmd_console(["-n", "3", "-t", "testtarget"])

    mock_client.get_lines.assert_called_once_with(tail_lines=3, grep=None)
    captured = capsys.readouterr()
    assert "line 1\nline 2\nline 3" in captured.out


def test_cmd_console_interactive(capsys: pytest.CaptureFixture[str]) -> None:
    mock_client = MagicMock()
    mock_client.effective_pty_path = "/tmp/mock-pty"

    with patch("ae_hw_bridge.mcp.server._get_or_start_daemon", return_value=mock_client), \
         patch("sys.stdout.isatty", return_value=True), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("os.path.exists", return_value=True), \
         patch("ae_hw_bridge.mcp.server._run_builtin_terminal") as mock_terminal:
        cmd_console(["--builtin", "-t", "testtarget"])

    mock_terminal.assert_called_once_with("/tmp/mock-pty")


def test_cmd_console_invalid_target_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("ae_hw_bridge.mcp.server.scan_hw_puppets", return_value=[]), \
         pytest.raises(SystemExit) as exc_info:
        cmd_console(["-t", "non_existent_target"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error: Target 'non_existent_target' not found" in captured.err


def test_cmd_console_empty_buffer_notice(capsys: pytest.CaptureFixture[str]) -> None:
    mock_client = MagicMock()
    mock_client.get_total_lines_count.return_value = 0

    with patch("ae_hw_bridge.mcp.server._get_or_start_daemon", return_value=mock_client):
        cmd_console(["-n", "50", "-t", "testtarget"])

    mock_client.get_lines.assert_not_called()
    captured = capsys.readouterr()
    assert "Console buffer is empty" in captured.err
    assert captured.out == ""


def test_cmd_console_streaming_notice(capsys: pytest.CaptureFixture[str]) -> None:
    mock_client = MagicMock()
    mock_client.get_total_lines_count.return_value = 0

    with patch("ae_hw_bridge.mcp.server._get_or_start_daemon", return_value=mock_client), \
         patch("sys.stderr.isatty", return_value=True), \
         patch("time.sleep", side_effect=KeyboardInterrupt):
        cmd_console(["-f", "-t", "testtarget"])

    captured = capsys.readouterr()
    assert "Streaming live console output" in captured.err


def test_daemon_resolution_by_connected_badge(capsys: pytest.CaptureFixture[str]) -> None:
    puppet = HWPuppetDevice(
        serial="serial123",
        control_port="/dev/ttyACM0",
        uart_port="/dev/ttyACM1",
        sysfs_path="",
        badge="jetson",
    )
    with patch("ae_hw_bridge.mcp.server.scan_hw_puppets", return_value=[puppet]), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("ae_hw_bridge.mcp.server.ensure_daemon_running") as mock_ensure:
        mock_client = MagicMock()
        mock_ensure.return_value = mock_client
        res = _get_or_start_daemon("jetson")
        assert res == mock_client
        mock_ensure.assert_called_once_with(
            name="jetson",
            control_port="/dev/ttyACM0",
            uart_port="/dev/ttyACM1",
        )


def test_daemon_resolution_auto_names_from_badge(capsys: pytest.CaptureFixture[str]) -> None:
    puppet = HWPuppetDevice(
        serial="serial123",
        control_port="/dev/ttyACM0",
        uart_port="/dev/ttyACM1",
        sysfs_path="",
        badge="jetson",
    )
    with patch("ae_hw_bridge.mcp.server.scan_hw_puppets", return_value=[puppet]), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("ae_hw_bridge.mcp.server.ensure_daemon_running") as mock_ensure:
        mock_client = MagicMock()
        mock_ensure.return_value = mock_client
        res = _get_or_start_daemon(None)
        assert res == mock_client
        mock_ensure.assert_called_once_with(
            name="jetson",
            control_port="/dev/ttyACM0",
            uart_port="/dev/ttyACM1",
        )


def test_daemon_resolution_multiple_puppets_error(capsys: pytest.CaptureFixture[str]) -> None:
    p1 = HWPuppetDevice(serial="s1", control_port="/dev/ttyACM0", uart_port="/dev/ttyACM1", sysfs_path="", badge="jetson")
    p2 = HWPuppetDevice(serial="s2", control_port="/dev/ttyACM2", uart_port="/dev/ttyACM3", sysfs_path="", badge="rockchip")
    with patch("ae_hw_bridge.mcp.server.scan_hw_puppets", return_value=[p1, p2]), \
         patch("pathlib.Path.glob", return_value=[]), \
         pytest.raises(SystemExit) as exc_info:
        _get_or_start_daemon(None)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Multiple HW-Puppet devices connected ('jetson', 'rockchip')" in captured.err


def test_daemon_resolution_by_port(capsys: pytest.CaptureFixture[str]) -> None:
    puppet = HWPuppetDevice(
        serial="serial123",
        control_port="/dev/ttyACM0",
        uart_port="/dev/ttyACM1",
        sysfs_path="",
        badge="",
    )
    with patch("ae_hw_bridge.mcp.server.scan_hw_puppets", return_value=[puppet]), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("ae_hw_bridge.mcp.server.ensure_daemon_running") as mock_ensure:
        mock_client = MagicMock()
        mock_ensure.return_value = mock_client
        res = _get_or_start_daemon("/dev/ttyACM0")
        assert res == mock_client
        mock_ensure.assert_called_once_with(
            name="ttyACM0",
            control_port="/dev/ttyACM0",
            uart_port="/dev/ttyACM1",
        )


def test_cmd_label_clear(capsys: pytest.CaptureFixture[str]) -> None:
    from ae_hw_bridge.mcp.server import cmd_label_device
    mock_serial = MagicMock()
    mock_serial.read_until.return_value = b"raw REPL; CTRL-B to exit\r\n>"
    mock_serial.read.return_value = b"OK"

    with patch("serial.Serial", return_value=mock_serial):
        cmd_label_device(badge="", port="/dev/ttyACM0")

    captured = capsys.readouterr()
    assert "Persistent badge successfully cleared" in captured.out
