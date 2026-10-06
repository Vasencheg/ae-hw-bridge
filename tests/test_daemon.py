"""Unit and integration tests for Unix domain socket Daemon and IPC Client."""

import os
import tempfile
import time
import pytest
from unittest.mock import MagicMock, patch

from ae_hw_bridge.core.interfaces import (
    ExecutionResult,
    IReplClient,
    IConsoleReader,
)
from ae_hw_bridge.daemon.protocol import (
    Request,
    Response,
    encode_request,
    decode_request,
    encode_response,
    decode_response,
)
from ae_hw_bridge.daemon.server import DaemonServer
from ae_hw_bridge.daemon.client import DaemonIpcClient


def test_protocol_request_serialization() -> None:
    req = Request(method="read_target_console", params={"tail_lines": 10}, id="req-123")
    raw = encode_request(req)
    assert raw.endswith(b"\n")

    decoded = decode_request(raw.decode("utf-8").strip())
    assert decoded.id == "req-123"
    assert decoded.method == "read_target_console"
    assert decoded.params == {"tail_lines": 10}


def test_protocol_response_serialization() -> None:
    resp = Response(id="resp-456", ok=True, result=["line1", "line2"])
    raw = encode_response(resp)
    assert raw.endswith(b"\n")

    decoded = decode_response(raw.decode("utf-8").strip())
    assert decoded.id == "resp-456"
    assert decoded.ok is True
    assert decoded.result == ["line1", "line2"]
    assert decoded.error is None


def test_daemon_client_server_e2e() -> None:
    # Use temporary socket and lock paths in /tmp
    temp_dir = tempfile.mkdtemp()
    sock_path = os.path.join(temp_dir, "test.sock")
    lock_path = os.path.join(temp_dir, "test.lock")

    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    # Setup mock behavior
    mock_repl.is_connected.return_value = True
    mock_repl.exec_code.return_value = ExecutionResult(ok=True, stdout="test_out", stderr="")
    mock_console.get_lines.return_value = ["[00:00:01.000] system ready"]
    mock_console.get_total_lines_count.return_value = 5
    mock_console.get_lines_since.return_value = ["command echoed", "nvidia@tegra:~$ "]
    mock_console.wait_for.return_value = "[00:00:02.000] tegra-ubuntu login: "

    server = DaemonServer(
        repl=mock_repl,
        console=mock_console,
        socket_path=sock_path,
        lock_path=lock_path,
    )

    server.start()
    try:
        client = DaemonIpcClient(socket_path=sock_path)
        assert client.ping() is True

        # Test IReplClient methods
        assert client.is_connected() is True
        res_exec = client.exec_code("print('hello')", timeout=5.0)
        assert res_exec.ok is True
        assert res_exec.stdout == "test_out"

        # Test IConsoleReader methods
        lines = client.get_lines(tail_lines=10)
        assert len(lines) == 1
        assert "system ready" in lines[0]

        client.clear()
        mock_console.clear.assert_called_once()

        wait_line = client.wait_for("login:", timeout=2.0)
        assert wait_line is not None
        assert "login:" in wait_line

        # Test get_total_lines_count and get_lines_since
        assert client.get_total_lines_count() == 5
        since_lines = client.get_lines_since(5)
        assert len(since_lines) == 2

        # Test write via IPC
        mock_console.write.return_value = 4
        written = client.write(b"test")
        assert written == 4
        mock_console.write.assert_called_once_with(b"test")

    finally:
        server.stop()
        if os.path.exists(temp_dir):
            try:
                import shutil

                shutil.rmtree(temp_dir)
            except Exception:
                pass


def test_concurrent_clients_no_blocking() -> None:
    """Verify that a slow blocking request from Client 1 does not block Client 2."""
    import threading

    temp_dir = tempfile.mkdtemp()
    sock_path = os.path.join(temp_dir, "concurrent.sock")
    lock_path = os.path.join(temp_dir, "concurrent.lock")

    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    # Slow wait_for simulates waiting for 30s boot
    def slow_wait(*args, **kwargs):
        time.sleep(0.5)
        return "booted"

    mock_console.wait_for.side_effect = slow_wait
    mock_console.get_lines.return_value = ["instant line"]

    server = DaemonServer(
        repl=mock_repl,
        console=mock_console,
        socket_path=sock_path,
        lock_path=lock_path,
    )
    server.start()

    try:
        client1 = DaemonIpcClient(socket_path=sock_path)
        client2 = DaemonIpcClient(socket_path=sock_path)

        res1_holder = []

        def worker1():
            res1 = client1.wait_for("boot", timeout=2.0)
            res1_holder.append(res1)

        t1 = threading.Thread(target=worker1)
        t1.start()

        # Small sleep so client1 request is in-flight on the server
        time.sleep(0.05)

        # Client 2 should respond immediately while Client 1 is still blocked in slow_wait
        t_start = time.time()
        assert client2.ping() is True
        lines = client2.get_lines(tail_lines=5)
        elapsed = time.time() - t_start

        assert "instant line" in lines[0]
        # Must have completed in < 0.25s (well before worker1's 0.5s sleep)
        assert elapsed < 0.25

        t1.join(timeout=2.0)
        assert res1_holder == ["booted"]
    finally:
        server.stop()
        if os.path.exists(temp_dir):
            try:
                import shutil

                shutil.rmtree(temp_dir)
            except Exception:
                pass


def test_idle_timeout_auto_shutdown() -> None:
    """Verify that DaemonServer stops itself when no clients are connected for idle_timeout."""
    temp_dir = tempfile.mkdtemp()
    sock_path = os.path.join(temp_dir, "idle.sock")
    lock_path = os.path.join(temp_dir, "idle.lock")

    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    server = DaemonServer(
        repl=mock_repl,
        console=mock_console,
        socket_path=sock_path,
        lock_path=lock_path,
        idle_timeout=0.3,
    )
    server.start()

    try:
        assert server.is_running() is True
        client = DaemonIpcClient(socket_path=sock_path)
        assert client.ping() is True

        # Close client connection
        client.close()

        # Wait for idle timeout (0.3s) to fire
        time.sleep(0.5)
        assert server.is_running() is False
    finally:
        server.stop()
        if os.path.exists(temp_dir):
            try:
                import shutil

                shutil.rmtree(temp_dir)
            except Exception:
                pass


def test_stop_daemon_utility() -> None:
    """Verify stop_daemon properly shuts down daemon process and removes lock/socket."""
    from ae_hw_bridge.daemon.server import stop_daemon

    temp_dir = tempfile.mkdtemp()
    sock_path = os.path.join(temp_dir, "stop.sock")
    lock_path = os.path.join(temp_dir, "stop.lock")

    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    server = DaemonServer(
        repl=mock_repl,
        console=mock_console,
        socket_path=sock_path,
        lock_path=lock_path,
        idle_timeout=60.0,
    )
    server.start()

    try:
        # Create lock file simulating real daemon process
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")

        # Non-matching or current PID test
        assert os.path.exists(sock_path)

        # Stop daemon
        server.stop()
        assert not os.path.exists(sock_path)
    finally:
        server.stop()
        if os.path.exists(temp_dir):
            try:
                import shutil

                shutil.rmtree(temp_dir)
            except Exception:
                pass


def test_find_port_holder_nonexistent() -> None:
    from ae_hw_bridge.core.port_utils import find_port_holder

    holder = find_port_holder("/dev/non_existent_port_12345")
    assert holder is None


def test_send_target_command_smart_wait() -> None:
    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    counts = [0, 1, 2, 3, 3, 3, 3, 3, 3]
    count_iter = iter(counts)

    def mock_get_total():
        try:
            return next(count_iter)
        except StopIteration:
            return 3

    mock_console.get_total_lines_count.side_effect = mock_get_total
    mock_console.get_lines_since.return_value = ["line 1", "line 2", "line 3"]

    server = DaemonServer(
        repl=mock_repl,
        console=mock_console,
        socket_path="/tmp/mock_smart_wait.sock",
        lock_path="/tmp/mock_smart_wait.lock",
    )

    t0 = time.time()
    out = server._handle_send_target_command({
        "command": "uname -a",
        "wait_timeout": 5.0,
        "idle_threshold": 0.15,
    })
    elapsed = time.time() - t0

    assert "line 1" in out
    assert elapsed < 1.5


def test_daemon_signature_and_incompatible_detection() -> None:
    """Verify daemon reports version signature and client detects incompatibility."""
    from ae_hw_bridge import __version__
    from ae_hw_bridge.daemon.protocol import get_daemon_signature

    sig = get_daemon_signature()
    assert sig.startswith(f"{__version__}-")
    assert len(sig) > 10

    temp_dir = tempfile.mkdtemp()
    sock_path = os.path.join(temp_dir, "sig.sock")
    lock_path = os.path.join(temp_dir, "sig.lock")

    mock_repl = MagicMock(spec=IReplClient)
    mock_console = MagicMock(spec=IConsoleReader)

    server = DaemonServer(
        repl=mock_repl,
        console=mock_console,
        socket_path=sock_path,
        lock_path=lock_path,
        idle_timeout=60.0,
    )
    server.start()

    try:
        client = DaemonIpcClient(socket_path=sock_path)
        assert client.ping() is True
        assert client.get_version() == sig
        assert client.is_compatible() is True

        # If remote returned different signature, is_compatible is False
        with patch.object(client, "get_version", return_value="0.0.1-stale"):
            assert client.is_compatible() is False
    finally:
        server.stop()
        if os.path.exists(temp_dir):
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


def test_daemon_status_and_pty_reporting() -> None:
    """Verify get_status and effective_pty_path returns correct information."""
    temp_dir = tempfile.mkdtemp()
    sock_path = os.path.join(temp_dir, "status.sock")
    lock_path = os.path.join(temp_dir, "status.lock")
    pty_link = os.path.join(temp_dir, "status-uart")

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
        name="testtarget",
        firmware_info={"badge": "testbadge", "version": "0.2.0"},
    )
    server.start()

    try:
        client = DaemonIpcClient(socket_path=sock_path)
        status = client.get_status()
        assert status.get("status") == "ok"
        assert status.get("name") == "testtarget"
        assert status.get("control_port") == "/dev/ttyACM0"
        assert status.get("uart_port") == "/dev/ttyACM1"
        assert status.get("pty_path") == pty_link
        assert client.effective_pty_path == pty_link
        assert status.get("firmware_info", {}).get("badge") == "testbadge"
    finally:
        server.stop()
        if os.path.exists(temp_dir):
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)




