"""Unit tests for ReplClient implementing Raw REPL communication."""

import pytest
from unittest.mock import MagicMock, patch
from ae_hw_bridge.core.repl_client import ReplClient
from ae_hw_bridge.core.interfaces import IReplClient


def test_repl_client_satisfies_protocol() -> None:
    client = ReplClient(port="/dev/mock")
    assert isinstance(client, IReplClient)


def test_connect_enters_raw_repl() -> None:
    with patch("serial.Serial") as mock_serial_cls:
        mock_instance = MagicMock()
        mock_instance.is_open = True
        mock_instance.read_until.return_value = b"raw REPL; CTRL-B to exit\r\n>"
        mock_serial_cls.return_value = mock_instance

        client = ReplClient(port="/dev/mock")
        client.connect()

        assert client.is_connected() is True
        mock_instance.reset_input_buffer.assert_called_once()
        # Verify Ctrl-C twice and Ctrl-A sequence sent
        mock_instance.write.assert_any_call(b"\r\x03\x03")
        mock_instance.write.assert_any_call(b"\r\x01")


def test_connect_failure_raises_runtime_error() -> None:
    with patch("serial.Serial") as mock_serial_cls:
        mock_instance = MagicMock()
        mock_instance.is_open = True
        mock_instance.read_until.return_value = b"some junk response"
        mock_serial_cls.return_value = mock_instance

        client = ReplClient(port="/dev/mock")
        with pytest.raises(RuntimeError, match="Failed to enter raw REPL"):
            client.connect()


def test_exec_code_success() -> None:
    client = ReplClient(port="/dev/mock")
    mock_serial = MagicMock()
    mock_serial.is_open = True
    # In connected state, read_until receives OK and output
    mock_serial.read_until.side_effect = [
        b"OK",                              # code accepted
        b"HELLO_WORLD\r\n\x04\x04>",       # stdout + EOT + EOT + prompt
    ]
    client._serial = mock_serial
    client._connected = True

    result = client.exec_code("print('HELLO_WORLD')")
    assert result.ok is True
    assert "HELLO_WORLD" in result.stdout
    assert result.stderr == ""


def test_exec_code_auto_connect() -> None:
    with patch("serial.Serial") as mock_serial_cls:
        mock_instance = MagicMock()
        mock_instance.is_open = True
        mock_instance.read_until.side_effect = [
            b"raw REPL; CTRL-B to exit\r\n>",  # enter raw repl
            b"OK",                              # code accepted
            b"42\r\n\x04\x04>",                 # execution output
        ]
        mock_serial_cls.return_value = mock_instance

        client = ReplClient(port="/dev/mock")
        # client is not connected initially
        result = client.exec_code("print(42)")
        assert client.is_connected() is True
        assert result.ok is True
        assert "42" in result.stdout


def test_exec_code_runtime_error() -> None:
    client = ReplClient(port="/dev/mock")
    mock_serial = MagicMock()
    mock_serial.is_open = True
    # Simulate Raw REPL with an exception in stderr
    mock_serial.read_until.side_effect = [
        b"OK",                                              # code accepted
        b"\x04Traceback (most recent call last):\r\nNameError: name 'foo' is not defined\r\n\x04>",
    ]
    client._serial = mock_serial
    client._connected = True

    result = client.exec_code("foo")
    assert result.ok is False
    assert "NameError" in result.stderr


def test_close_exits_raw_repl() -> None:
    client = ReplClient(port="/dev/mock")
    mock_serial = MagicMock()
    mock_serial.is_open = True
    client._serial = mock_serial
    client._connected = True

    client.close()
    assert client.is_connected() is False
    mock_serial.write.assert_called_with(b"\r\x02")
    mock_serial.close.assert_called_once()
