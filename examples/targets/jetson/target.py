"""Reference implementation of a hardware target module for NVIDIA Jetson.

Demonstrates:
1. Subclassing BaseTarget to inherit all 7 core MCP tools.
2. Using the @repl decorator to write MicroPython pin control methods directly in Python.
3. Adding high-level orchestrations that combine @repl, console buffering, and shell commands.
"""

import time
import logging
import subprocess
from typing import Any
from ae_hw_bridge.targets.base import BaseTarget, repl

_logger = logging.getLogger(__name__)


class JetsonTarget(BaseTarget):
    """NVIDIA Jetson (Orin NX / Nano) target controller."""

    PIN_REC: int = 11
    PIN_RST: int = 12
    PIN_PWR: int = 13
    PIN_LED: int = 48  # Onboard WS2812 RGB LED on ESP32-S3-DevKitC-1

    # --- Atomic MicroPython pin operations executed in ESP32 RAM ---

    @repl
    def set_status_led(self, r: int, g: int, b: int, pin: int = 48):
        """Set onboard WS2812 RGB LED color (R, G, B: 0..255)."""
        try:
            from machine import Pin
            import neopixel
            np = neopixel.NeoPixel(Pin(pin), 1)
            np[0] = (r, g, b)
            np.write()
        except Exception:
            pass

    @repl
    def trigger_reset(self, pin: int = 12, duration: float = 0.5):
        """Send hardware reset pulse via MicroPython on ESP32."""
        import time
        from machine import Pin

        rst = Pin(pin, Pin.OUT, value=1)
        rst.value(0)
        time.sleep(duration)
        rst.value(1)

    @repl
    def trigger_recovery(self, rec_pin: int = 11, rst_pin: int = 12):
        """Hold Force Recovery and pulse Reset to latch Tegra into APX/RCM mode."""
        import time
        from machine import Pin

        rec = Pin(rec_pin, Pin.OUT, value=0)
        rst = Pin(rst_pin, Pin.OUT, value=1)
        time.sleep(0.5)
        rst.value(0)
        time.sleep(0.5)
        rst.value(1)
        time.sleep(1.0)
        rec.value(1)

    @repl
    def trigger_power_button(self, pin: int = 13, duration: float = 0.5):
        """Pulse power button relay on ESP32 (short press to wake/sleep, long press ~5s for forced shutdown)."""
        import time
        from machine import Pin

        pwr = Pin(pin, Pin.OUT, value=1)
        pwr.value(0)
        time.sleep(duration)
        pwr.value(1)

    # --- High-level multi-step operations exposed as MCP tools ---

    def hardware_reset(self, duration: float = 0.5, clear_console: bool = True) -> dict[str, Any]:
        """Pulse the hardware reset line without waiting for OS boot.

        Args:
            duration: Duration of the active-LOW reset pulse in seconds (default: 0.5s).
            clear_console: Whether to clear the UART console buffer before reset (default: True).
        """
        self.set_status_led(60, 0, 0, pin=self.PIN_LED)  # Red during reset
        if clear_console:
            self.clear_target_console()
        res = self.trigger_reset(pin=self.PIN_RST, duration=duration)
        return {"ok": res.ok, "stdout": res.stdout, "stderr": res.stderr}

    def full_reboot(self, timeout: float = 45.0) -> dict[str, Any]:
        """Hardware reset Jetson and wait for Linux login prompt to confirm boot.

        Args:
            timeout: Maximum seconds to wait for boot completion (default: 45.0s).
        """
        self.clear_target_console()
        self.set_status_led(60, 0, 0, pin=self.PIN_LED)  # Red during reset pulse
        self.trigger_reset(pin=self.PIN_RST, duration=0.5)
        self.set_status_led(50, 20, 0, pin=self.PIN_LED)  # Orange while waiting for boot
        res = self.wait_for_console_pattern("login:", timeout=timeout)
        booted = res.get("matched", False)
        if booted:
            self.set_status_led(0, 60, 0, pin=self.PIN_LED)  # Green on successful boot
        else:
            self.set_status_led(60, 0, 0, pin=self.PIN_LED)  # Red on timeout
        return {
            "booted": booted,
            "line": res.get("line"),
            "timeout": timeout,
        }

    def power_button(self, duration: float = 0.5) -> dict[str, Any]:
        """Press the hardware power button (short press to wake/sleep, >=5.0s for forced shutdown).

        Args:
            duration: Pulse duration in seconds (default: 0.5s). Use 5.0+ seconds for force poweroff.
        """
        self.set_status_led(0, 0, 60, pin=self.PIN_LED)  # Blue on power action
        res = self.trigger_power_button(pin=self.PIN_PWR, duration=duration)
        return {"ok": res.ok, "stdout": res.stdout, "stderr": res.stderr, "duration": duration}

    def software_reboot(self, timeout: float = 30.0) -> dict[str, Any]:
        """Send software reboot command via console and wait for reboot confirmation.

        Args:
            timeout: Maximum seconds to wait for reboot messages (default: 30.0s).
        """
        out = self.send_target_command("sudo reboot || reboot", wait_timeout=2.0)
        res = self.wait_for_console_pattern(r"reboot: Restarting system|Restarting system", timeout=timeout)
        return {
            "sent": True,
            "output": out,
            "rebooting": res.get("matched", False),
        }

    def reboot_to_bootloader(self, timeout: float = 15.0) -> dict[str, Any]:
        """Hardware reset Jetson and interrupt autoboot to reach bootloader (U-Boot/UEFI) prompt.

        Args:
            timeout: Maximum seconds to wait for bootloader prompt (default: 15.0s).
        """
        self.clear_target_console()
        self.trigger_reset(pin=self.PIN_RST, duration=0.5)
        start = time.time()
        matched = False
        matched_line = None
        while time.time() - start < timeout:
            self.console.write(b" ")
            res = self.wait_for_console_pattern(r"Hit any key to stop autoboot|Tegra234 #|=>|Shell>", timeout=1.0)
            if res.get("matched"):
                matched = True
                matched_line = res.get("line")
                break
        return {
            "interrupted": matched,
            "prompt": matched_line,
        }

    def wait_for_boot(self, pattern: str = "login:", timeout: float = 60.0) -> dict[str, Any]:
        """Wait for target operating system boot completion.

        Args:
            pattern: Boot completion regex marker (default: 'login:').
            timeout: Maximum seconds to wait (default: 60.0s).
        """
        self.set_status_led(50, 20, 0, pin=self.PIN_LED)  # Orange while booting
        res = self.wait_for_console_pattern(pattern, timeout=timeout)
        booted = res.get("matched", False)
        if booted:
            self.set_status_led(0, 60, 0, pin=self.PIN_LED)  # Green on success
        else:
            self.set_status_led(60, 0, 0, pin=self.PIN_LED)  # Red on timeout
        return {
            "booted": booted,
            "line": res.get("line"),
        }

    def wait_for_shell(self, timeout: float = 10.0) -> dict[str, Any]:
        """Send newline to console and wait for shell prompt to verify console responsiveness.

        Args:
            timeout: Maximum seconds to wait for shell prompt (default: 10.0s).
        """
        self.set_status_led(50, 20, 0, pin=self.PIN_LED)  # Orange
        self.console.write(b"\n")
        res = self.wait_for_console_pattern(r"(?:[\$#]\s*$|\w+@\w+:.*[\$#]\s*)", timeout=timeout)
        ready = res.get("matched", False)
        if ready:
            self.set_status_led(0, 60, 0, pin=self.PIN_LED)  # Green
        else:
            self.set_status_led(60, 0, 0, pin=self.PIN_LED)  # Red
        return {
            "ready": ready,
            "prompt": res.get("line"),
        }

    def login(self, username: str = "nvidia", password: str = "nvidia", timeout: float = 15.0) -> dict[str, Any]:
        """Log into the target Linux console using credentials with robust prompt synchronization.

        Handles:
        - Already logged-in shell detection.
        - Getty timeout / wake-up via newline and line reset.
        - Synchronized entry of username and password.

        Args:
            username: Login username (default: 'nvidia').
            password: Login password (default: 'nvidia').
            timeout: Timeout waiting for password prompt and shell (default: 15.0s).
        """
        shell_regex = r"(?:[\$#]\s*$|\w+@\w+:.*[\$#]\s*)"
        login_regex = r"(?:login:\s*$|Username:\s*$)"
        deadline = time.time() + timeout

        # 1. Check if console is ALREADY in an active shell session
        self.console.write(b"\n")
        check_shell = self.wait_for_console_pattern(shell_regex, timeout=1.0, check_history=False)
        if check_shell.get("matched"):
            _logger.info("Target console already logged in: %s", check_shell.get("line"))
            self.set_status_led(0, 60, 0, pin=self.PIN_LED)  # Green: ready
            return {
                "logged_in": True,
                "already_logged_in": True,
                "output": check_shell.get("line"),
            }

        # 2. Reset line and synchronize with login: prompt
        # Send Ctrl+C and Ctrl+U to discard any corrupted/partial input
        self.console.write(b"\x03\x15\n")
        time.sleep(0.2)

        remaining = max(1.0, deadline - time.time())
        login_match = self.wait_for_console_pattern(login_regex, timeout=min(4.0, remaining), check_history=True)
        if not login_match.get("matched"):
            # Wake up getty once more with a clean newline
            self.console.write(b"\n")
            remaining = max(1.0, deadline - time.time())
            login_match = self.wait_for_console_pattern(login_regex, timeout=min(4.0, remaining), check_history=False)

        if not login_match.get("matched"):
            self.set_status_led(60, 0, 0, pin=self.PIN_LED)  # Red: login failed
            return {
                "logged_in": False,
                "error": "Login prompt (login:) not received from target",
            }

        # 3. Send username
        self.console.write(f"{username}\n".encode("utf-8"))

        # 4. Wait for Password prompt
        remaining = max(1.0, deadline - time.time())
        pwd_match = self.wait_for_console_pattern(r"Password:\s*", timeout=min(5.0, remaining), check_history=False)
        if not pwd_match.get("matched"):
            self.set_status_led(60, 0, 0, pin=self.PIN_LED)  # Red: login failed
            return {
                "logged_in": False,
                "error": "Password prompt not received after sending username",
            }

        # 5. Send password
        self.console.write(f"{password}\n".encode("utf-8"))

        # 6. Wait for shell prompt
        remaining = max(1.0, deadline - time.time())
        shell_match = self.wait_for_console_pattern(shell_regex, timeout=remaining, check_history=False)
        if shell_match.get("matched"):
            self.set_status_led(0, 60, 0, pin=self.PIN_LED)  # Green: logged in successfully
            return {
                "logged_in": True,
                "output": shell_match.get("line"),
            }

        # Check if Login incorrect was reported in recent console lines
        tail = self.read_target_console(tail_lines=6)
        tail_text = "\n".join(tail.get("lines", []))
        self.set_status_led(60, 0, 0, pin=self.PIN_LED)  # Red: login failed
        if "Login incorrect" in tail_text:
            return {
                "logged_in": False,
                "error": "Login incorrect (invalid username or password)",
            }

        return {
            "logged_in": False,
            "error": "Timed out waiting for shell prompt after password",
        }

    def enter_recovery(self) -> dict[str, Any]:
        """Put Jetson into Force Recovery (APX/RCM) mode for flashing."""
        self.set_status_led(60, 50, 0, pin=self.PIN_LED)  # Yellow for Force Recovery
        res = self.trigger_recovery(rec_pin=self.PIN_REC, rst_pin=self.PIN_RST)
        return {"ok": res.ok, "stdout": res.stdout, "stderr": res.stderr}

    def set_led_color(self, r: int = 0, g: int = 60, b: int = 0) -> dict[str, Any]:
        """Set the onboard WS2812 RGB LED color on HW Puppet.

        Args:
            r: Red intensity (0-255).
            g: Green intensity (0-255).
            b: Blue intensity (0-255).
        """
        self.set_status_led(r=r, g=g, b=b, pin=self.PIN_LED)
        return {"ok": True, "r": r, "g": g, "b": b}

    def check_recovery_mode(self) -> dict[str, Any]:
        """Check host USB bus to detect if Jetson is in APX/Force Recovery mode (NVIDIA USB VID 0955)."""
        try:
            res = subprocess.run(
                ["lsusb"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            devices = [line.strip() for line in res.stdout.splitlines() if "0955:" in line or "NVIDIA" in line]
            return {
                "in_recovery": len(devices) > 0,
                "devices": devices,
            }
        except Exception as e:
            return {"in_recovery": False, "error": str(e)}

    def exec_command_with_status(self, command: str, wait_timeout: float = 5.0) -> dict[str, Any]:
        """Execute a shell command on the target and capture output along with its exit code ($?).

        Args:
            command: Shell command to execute.
            wait_timeout: Timeout for command execution (default: 5.0s).
        """
        full_cmd = f"{command}; echo \"__EXIT_CODE:$?__\""
        raw_output = self.send_target_command(full_cmd, wait_timeout=wait_timeout)
        exit_code = None
        cleaned_lines = []
        for line in raw_output.splitlines():
            if "__EXIT_CODE:" in line:
                try:
                    exit_code = int(line.split("__EXIT_CODE:")[1].split("__")[0])
                except (ValueError, IndexError):
                    pass
            else:
                cleaned_lines.append(line)
        return {
            "command": command,
            "exit_code": exit_code,
            "output": "\n".join(cleaned_lines).strip(),
        }

    def get_network_info(self) -> str:
        """Query IP addresses and network interfaces from the Jetson Linux console."""
        return self.send_target_command("ip -br addr; uname -r", wait_timeout=4.0)

    def get_system_info(self) -> dict[str, Any]:
        """Query comprehensive target telemetry (kernel, uptime, memory, storage, temperatures)."""
        info = {}
        info["uname"] = self.send_target_command("uname -a", wait_timeout=3.0).strip()
        info["uptime"] = self.send_target_command("uptime", wait_timeout=3.0).strip()
        info["memory"] = self.send_target_command("free -h", wait_timeout=3.0).strip()
        info["disk"] = self.send_target_command("df -h /", wait_timeout=3.0).strip()
        info["thermal"] = self.send_target_command(
            "cat /sys/devices/virtual/thermal/thermal_zone*/temp 2>/dev/null || tegrastats --interval 1000 | head -n 1",
            wait_timeout=3.0,
        ).strip()
        return info

    def check_alive(self, timeout: float = 3.0) -> dict[str, Any]:
        """Quick healthcheck ping to verify if target console responds.

        Args:
            timeout: Maximum seconds to wait for ping response (default: 3.0s).
        """
        token = f"PONG_{int(time.time())}"
        out = self.send_target_command(f"echo {token}", wait_timeout=timeout)
        alive = token in out
        return {"alive": alive, "response": out.strip()}
