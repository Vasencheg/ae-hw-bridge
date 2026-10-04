"""Unit tests for ConsoleReader circular buffer and background monitor."""

import time
import threading
import pytest
from unittest.mock import MagicMock, patch
from ae_hw_bridge.core.console_reader import ConsoleReader
from ae_hw_bridge.core.interfaces import IConsoleReader


def test_console_reader_satisfies_protocol() -> None:
    reader = ConsoleReader(port="/dev/mock")
    assert isinstance(reader, IConsoleReader)


def test_buffer_tail_and_grep() -> None:
    reader = ConsoleReader(port="/dev/mock", maxlen=5)
    # Inject lines directly via _append_line
    for i in range(10):
        reader._append_line(f"line {i} - info")
    reader._append_line("CRITICAL: kernel panic")

    # Verify maxlen is respected (buffer should have at most 5 items)
    assert len(reader.get_tail(lines=100)) == 5

    tail = reader.get_tail(lines=3)
    assert len(tail) == 3
    assert "CRITICAL: kernel panic" in tail[-1]

    filtered = reader.get_tail(lines=10, grep="CRITICAL")
    assert len(filtered) == 1
    assert "kernel panic" in filtered[0]

    # Test head_lines vs tail_lines
    head = reader.get_lines(head_lines=2)
    assert len(head) == 2
    assert "line 6" in head[0]  # First line currently in 5-element buffer (items 6, 7, 8, 9, CRITICAL)

    reader.clear()
    assert len(reader.get_tail()) == 0


def test_monotonic_counter_and_lines_since() -> None:
    reader = ConsoleReader(port="/dev/mock", maxlen=10)
    reader._append_line("initial line")
    mark = reader.get_total_lines_count()
    assert mark == 1

    reader._append_line("new output 1")
    reader._append_line("new output 2")

    since = reader.get_lines_since(mark)
    assert len(since) == 2
    assert "new output 1" in since[0]
    assert "new output 2" in since[1]


def test_write_bytes() -> None:
    reader = ConsoleReader(port="/dev/mock")
    mock_serial = MagicMock()
    mock_serial.is_open = True
    reader._serial = mock_serial

    reader.write(b"reboot\r\n")
    mock_serial.write.assert_called_once_with(b"reboot\r\n")


def test_start_and_stop_lifecycle() -> None:
    reader = ConsoleReader(port="/dev/mock")
    with patch("serial.Serial") as mock_serial_cls:
        mock_instance = MagicMock()
        mock_instance.is_open = True
        # readline returns empty bytes to simulate idle
        mock_instance.readline.return_value = b""
        mock_serial_cls.return_value = mock_instance

        reader.start()
        assert reader._thread is not None
        assert reader._thread.is_alive()

        # Idempotent start
        reader.start()

        reader.stop()
        assert reader._thread is None
        assert reader._stop_event.is_set()


def test_wait_for_pattern() -> None:
    import threading
    import time

    reader = ConsoleReader(port="/dev/mock")

    # Inject line in background after 50ms
    def delayed_append():
        time.sleep(0.05)
        reader._append_line("login: ubuntu")

    t = threading.Thread(target=delayed_append)
    t.start()

    match = reader.wait_for("login:", timeout=1.0)
    t.join()

    assert match is not None
    assert "login: ubuntu" in match

    # Test timeout with non-matching pattern
    no_match = reader.wait_for("NON_EXISTENT_PATTERN", timeout=0.05)
    assert no_match is None


def test_wait_for_pattern_history() -> None:
    reader = ConsoleReader(port="/dev/mock")
    reader._append_line("tegra-ubuntu login: ")

    # With check_history=True (default), returns immediately from history
    match = reader.wait_for("login:", timeout=0.1, check_history=True)
    assert match is not None
    assert "login: " in match

    # With check_history=False, ignores history and times out if no new lines arrive
    match_no_hist = reader.wait_for("login:", timeout=0.05, check_history=False)
    assert match_no_hist is None


def test_wait_for_full_buffer() -> None:
    # Buffer capacity 5
    reader = ConsoleReader(port="/dev/mock", maxlen=5)
    for i in range(5):
        reader._append_line(f"old line {i}")

    assert len(reader._buffer) == 5

    def delayed_append():
        time.sleep(0.05)
        reader._append_line("kernel ready event")

    t = threading.Thread(target=delayed_append)
    t.start()

    match = reader.wait_for("kernel ready", timeout=1.0, check_history=False)
    t.join()

    assert match is not None
    assert "kernel ready event" in match


def test_console_reader_reconnect_on_error() -> None:
    reader = ConsoleReader(port="/dev/mock")
    call_count = 0
    mock_s = MagicMock()
    mock_s.is_open = True
    mock_s.in_waiting = 0

    def mock_open():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("USB disconnected")
        return mock_s

    with patch.object(reader, "_open_serial", side_effect=mock_open):
        reader.start()
        # Wait a short moment for retry loop (first backoff is 0.5s)
        time.sleep(0.7)
        reader.stop()

    assert call_count >= 2
