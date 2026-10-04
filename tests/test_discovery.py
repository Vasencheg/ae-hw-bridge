"""Unit tests for HW-Puppet USB discovery and port pairing via Linux sysfs."""

import pytest
from unittest.mock import patch, mock_open
from pathlib import Path
from ae_hw_bridge.core.discovery import (
    HWPuppetDevice,
    scan_hw_puppets,
    resolve_puppet,
)


def test_hw_puppet_device_repr() -> None:
    p = HWPuppetDevice(
        serial="123456",
        control_port="/dev/ttyACM1",
        uart_port="/dev/ttyACM3",
        sysfs_path="/sys/devices/1-2",
        badge="jetson-bench",
    )
    assert "ctrl=/dev/ttyACM1" in repr(p)
    assert "uart=/dev/ttyACM3" in repr(p)
    assert "badge='jetson-bench'" in repr(p)


@patch("glob.glob")
@patch("os.path.exists")
@patch("os.path.realpath")
def test_scan_hw_puppets_sysfs(mock_realpath, mock_exists, mock_glob) -> None:
    mock_glob.side_effect = lambda pat: (
        ["/sys/class/tty/ttyACM1", "/sys/class/tty/ttyACM2", "/sys/class/tty/ttyACM3"]
        if "ttyACM" in pat else []
    )
    mock_exists.return_value = True

    def fake_realpath(path):
        if "ttyACM1" in path:
            return "/sys/devices/pci0/usb1/1-2/1-2:1.0"
        elif "ttyACM2" in path:
            return "/sys/devices/pci0/usb1/1-6/1-6:1.2"
        elif "ttyACM3" in path:
            return "/sys/devices/pci0/usb1/1-2/1-2:1.2"
        return path

    mock_realpath.side_effect = fake_realpath

    def fake_open(filename, mode="r", **kwargs):
        fn = str(filename)
        if "1-2/idVendor" in fn:
            return mock_open(read_data="303a\n")()
        elif "1-2/idProduct" in fn:
            return mock_open(read_data="4002\n")()
        elif "1-2/serial" in fn:
            return mock_open(read_data="esp32s3_serial_123\n")()
        elif "1-2:1.0/bInterfaceNumber" in fn:
            return mock_open(read_data="00\n")()
        elif "1-2:1.2/bInterfaceNumber" in fn:
            return mock_open(read_data="02\n")()
        elif "1-6/idVendor" in fn:
            return mock_open(read_data="0955\n")()  # Jetson
        elif "1-6/idProduct" in fn:
            return mock_open(read_data="7020\n")()
        return mock_open(read_data="")()

    with patch("builtins.open", side_effect=fake_open):
        devices = scan_hw_puppets(probe_badges=False)

    assert len(devices) == 1
    dev = devices[0]
    assert dev.control_port == "/dev/ttyACM1"
    assert dev.uart_port == "/dev/ttyACM3"
    assert dev.serial == "esp32s3_serial_123"


def test_resolve_puppet_explicit_port() -> None:
    p = resolve_puppet(control_port="/dev/ttyACM1", uart_port="/dev/ttyACM3", probe_badges=False)
    assert p.control_port == "/dev/ttyACM1"
    assert p.uart_port == "/dev/ttyACM3"


@patch("ae_hw_bridge.core.discovery.scan_hw_puppets")
def test_resolve_puppet_by_badge(mock_scan) -> None:
    p1 = HWPuppetDevice(
        serial="ser1",
        control_port="/dev/ttyACM1",
        uart_port="/dev/ttyACM3",
        sysfs_path="/sys/devices/1-2",
        badge="jetson",
    )
    p2 = HWPuppetDevice(
        serial="ser2",
        control_port="/dev/ttyACM4",
        uart_port="/dev/ttyACM5",
        sysfs_path="/sys/devices/1-3",
        badge="stm32",
    )
    mock_scan.return_value = [p1, p2]

    resolved = resolve_puppet("stm32", probe_badges=False)
    assert resolved.control_port == "/dev/ttyACM4"
    assert resolved.badge == "stm32"


@patch("ae_hw_bridge.core.discovery.scan_hw_puppets")
def test_resolve_puppet_multiple_collision_raises(mock_scan) -> None:
    p1 = HWPuppetDevice(
        serial="ser1",
        control_port="/dev/ttyACM1",
        uart_port="/dev/ttyACM3",
        sysfs_path="/sys/devices/1-2",
        badge="jetson",
    )
    p2 = HWPuppetDevice(
        serial="ser2",
        control_port="/dev/ttyACM4",
        uart_port="/dev/ttyACM5",
        sysfs_path="/sys/devices/1-3",
        badge="stm32",
    )
    mock_scan.return_value = [p1, p2]

    with pytest.raises(RuntimeError) as exc_info:
        resolve_puppet(probe_badges=False)

    msg = str(exc_info.value)
    assert "Multiple HW-Puppet devices detected" in msg
    assert "Ambiguous target" in msg
    assert "/dev/ttyACM1" in msg
    assert "/dev/ttyACM4" in msg
