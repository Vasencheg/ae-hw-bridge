"""Hardware Puppet discovery and USB CDC port pairing via Linux sysfs."""

import os
import glob
import time
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

_logger = logging.getLogger(__name__)

HW_PUPPET_VID = "303a"
HW_PUPPET_PID = "4002"


@dataclass
class HWPuppetDevice:
    """Represents a discovered physical HW-Puppet ESP32-S3 board with dual CDC interfaces."""

    serial: str
    control_port: str          # CDC 0 (Interface 00 - MicroPython REPL / RPC)
    uart_port: Optional[str]   # CDC 1 (Interface 02 - Target UART Bridge)
    sysfs_path: str
    badge: Optional[str] = None
    version: Optional[str] = None
    platform: Optional[str] = "ESP32-S3"

    def __repr__(self) -> str:
        badge_str = f" badge='{self.badge}'" if self.badge else ""
        return (
            f"<HWPuppetDevice ctrl={self.control_port} "
            f"uart={self.uart_port}{badge_str} serial={self.serial}>"
        )


def _probe_badge_from_port(port: str, timeout: float = 1.5) -> dict[str, str]:
    """Connect briefly to CDC 0 Raw REPL to probe badge, version, and platform info."""
    import serial

    info = {"badge": "", "version": "", "platform": "ESP32-S3"}
    try:
        ser = serial.Serial(port, 115200, timeout=timeout)
    except Exception as e:
        _logger.debug("Cannot open %s for badge probing (might be locked by daemon): %s", port, e)
        return info

    try:
        # Send raw REPL enter sequence (Ctrl-C, Ctrl-C, Ctrl-A)
        ser.reset_input_buffer()
        ser.write(b"\r\x03\x03\x01")
        time.sleep(0.1)

        raw_prompt = ser.read_until(b"raw REPL; CTRL-B to exit\r\n>")
        if not raw_prompt.endswith(b">"):
            ser.write(b"\r\x03\x03\x01")
            time.sleep(0.1)
            raw_prompt = ser.read_until(b">")
            if not raw_prompt.endswith(b">"):
                return info

        code = (
            "import hw_puppet\n"
            "try:\n"
            " b = hw_puppet.get_badge()\n"
            "except Exception:\n"
            " b = ''\n"
            "print('B:' + str(b))\n"
            "print('V:' + str(getattr(hw_puppet, '__version__', 'unknown')))\n"
            "print('P:' + str(getattr(hw_puppet, 'PLATFORM', 'ESP32-S3')))\n"
        )
        ser.write(code.encode("utf-8") + b"\x04")
        # Read header 'OK'
        ser.read(2)

        # Stream output
        raw_out = bytearray()
        while True:
            b = ser.read(1)
            if not b or b == b"\x04":
                break
            raw_out.extend(b)

        out_text = raw_out.decode("utf-8", errors="replace")
        for line in out_text.splitlines():
            line = line.strip()
            if line.startswith("B:"):
                info["badge"] = line[2:].strip()
            elif line.startswith("V:"):
                info["version"] = line[2:].strip()
            elif line.startswith("P:"):
                info["platform"] = line[2:].strip()

    except Exception as e:
        _logger.debug("Exception while probing badge on %s: %s", port, e)
    finally:
        try:
            ser.write(b"\x02")  # Exit Raw REPL
        except Exception:
            pass
        try:
            ser.close()
        except Exception:
            pass

    return info


def scan_hw_puppets(probe_badges: bool = False) -> List[HWPuppetDevice]:
    """Scan Linux sysfs for all connected HW-Puppet devices.

    Correctly pairs CDC 0 (Interface 00) and CDC 1 (Interface 02) belonging to
    the same parent physical USB device, completely eliminating port number guessing.

    Args:
        probe_badges: If True, query CDC 0 to read the persistent badge and version.

    Returns:
        List of discovered HWPuppetDevice objects.
    """
    devices_by_parent: Dict[str, Dict[str, Any]] = {}

    tty_patterns = ["/sys/class/tty/ttyACM*", "/sys/class/tty/ttyUSB*"]
    all_tty_paths: List[str] = []
    for pattern in tty_patterns:
        all_tty_paths.extend(glob.glob(pattern))

    for tty_path in sorted(all_tty_paths):
        device_link = os.path.join(tty_path, "device")
        if not os.path.exists(device_link):
            continue

        try:
            dev_real = os.path.realpath(device_link)
            parent = os.path.dirname(dev_real)

            id_vendor_f = os.path.join(parent, "idVendor")
            id_product_f = os.path.join(parent, "idProduct")

            if not (os.path.exists(id_vendor_f) and os.path.exists(id_product_f)):
                continue

            with open(id_vendor_f, "r", encoding="utf-8") as f:
                vid = f.read().strip().lower()
            with open(id_product_f, "r", encoding="utf-8") as f:
                pid = f.read().strip().lower()

            if vid != HW_PUPPET_VID or pid != HW_PUPPET_PID:
                continue

            serial_f = os.path.join(parent, "serial")
            serial = ""
            if os.path.exists(serial_f):
                with open(serial_f, "r", encoding="utf-8") as f:
                    serial = f.read().strip()

            if_num_f = os.path.join(dev_real, "bInterfaceNumber")
            if_num = ""
            if os.path.exists(if_num_f):
                with open(if_num_f, "r", encoding="utf-8") as f:
                    if_num = f.read().strip()

            dev_node = "/dev/" + os.path.basename(tty_path)

            if parent not in devices_by_parent:
                devices_by_parent[parent] = {
                    "serial": serial,
                    "sysfs_path": parent,
                    "control_port": None,
                    "uart_port": None,
                }

            if if_num == "00":
                devices_by_parent[parent]["control_port"] = dev_node
            elif if_num == "02":
                devices_by_parent[parent]["uart_port"] = dev_node

        except (OSError, PermissionError) as e:
            _logger.debug("Error inspecting sysfs node %s: %s", tty_path, e)
            continue

    puppets: List[HWPuppetDevice] = []
    for parent_path, data in devices_by_parent.items():
        if data["control_port"]:
            badge = None
            version = None
            platform = "ESP32-S3"

            if probe_badges:
                probe_res = _probe_badge_from_port(data["control_port"])
                badge = probe_res.get("badge") or None
                version = probe_res.get("version") or None
                platform = probe_res.get("platform") or platform

            puppet = HWPuppetDevice(
                serial=data["serial"],
                control_port=data["control_port"],
                uart_port=data["uart_port"],
                sysfs_path=parent_path,
                badge=badge,
                version=version,
                platform=platform,
            )
            puppets.append(puppet)

    return puppets


def resolve_puppet(
    puppet_name_or_badge: Optional[str] = None,
    control_port: Optional[str] = None,
    uart_port: Optional[str] = None,
    probe_badges: bool = True,
) -> HWPuppetDevice:
    """Resolve a target's hardware puppet from configuration or environment.

    Safety rules:
    - If explicit control_port is provided, find its matched device (and auto-resolve uart_port).
    - If puppet_name_or_badge is provided, find the device whose badge or serial matches.
    - If nothing specified and exactly 1 board is attached, auto-select it.
    - If nothing specified and 2+ boards are attached, raise a RuntimeError with clear resolution steps.

    Raises:
        RuntimeError: If no puppets found or if ambiguous multi-puppet collision occurs.
    """
    detected = scan_hw_puppets(probe_badges=probe_badges)

    # Case 1: Explicit port provided
    if control_port:
        for p in detected:
            if p.control_port == control_port:
                if uart_port:
                    p.uart_port = uart_port
                return p
        # Port wasn't in sysfs detection (or synthetic test port), create manual device
        return HWPuppetDevice(
            serial="manual",
            control_port=control_port,
            uart_port=uart_port or "/dev/ttyACM1",
            sysfs_path="",
            badge=puppet_name_or_badge,
        )

    # Case 2: Matching by badge or serial
    if puppet_name_or_badge:
        for p in detected:
            if p.badge == puppet_name_or_badge or p.serial == puppet_name_or_badge:
                if uart_port:
                    p.uart_port = uart_port
                return p

        # Not found among scanned boards
        available_list = [
            f"  - {p.control_port} (Badge: '{p.badge or '<none>'}', Serial: {p.serial})"
            for p in detected
        ]
        devices_msg = "\n".join(available_list) if available_list else "  (No HW-Puppet boards detected)"
        raise RuntimeError(
            f"HW-Puppet with badge or serial '{puppet_name_or_badge}' was not found.\n"
            f"Detected devices:\n{devices_msg}\n"
            f"Please verify connection or label the board with:\n"
            f"  ae-hw-bridge label {puppet_name_or_badge} --port <port>"
        )

    # Case 3: No specific port or badge requested
    if not detected:
        # Fallback check for legacy symlinks if present
        if os.path.exists("/dev/hw-puppet-control") and os.path.exists("/dev/hw-puppet-uart"):
            return HWPuppetDevice(
                serial="legacy-symlink",
                control_port="/dev/hw-puppet-control",
                uart_port="/dev/hw-puppet-uart",
                sysfs_path="",
            )
        raise RuntimeError(
            "HW-PUPPET device not found. Please connect HW-PUPPET via USB."
        )

    if len(detected) == 1:
        single = detected[0]
        if uart_port:
            single.uart_port = uart_port
        return single

    # Case 4: Multiple puppets detected without configuration -> FAIL FAST & SAFE
    details = []
    for p in detected:
        details.append(
            f"  - Control: {p.control_port}, UART: {p.uart_port or 'none'} "
            f"(Badge: '{p.badge or '<not set>'}', Serial: {p.serial})"
        )
    boards_str = "\n".join(details)
    raise RuntimeError(
        f"Multiple HW-Puppet devices detected ({len(detected)} boards found):\n"
        f"{boards_str}\n\n"
        f"Ambiguous target: ae-hw-bridge cannot determine which puppet to use.\n"
        f"To resolve this, do one of the following:\n"
        f"  1. Specify port: ae-hw-bridge run --port {detected[0].control_port}\n"
        f"  2. Or set default puppet in .ae-hw-bridge/config.yml:\n"
        f"     puppet: <badge>\n"
        f"  3. Or define multi-target configuration in .ae-hw-bridge/config.yml"
    )
