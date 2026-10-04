# NVIDIA Jetson Target Module Example

This directory contains a reference implementation of a custom target module for **NVIDIA Jetson** (Orin NX, Orin Nano, Xavier, etc.) using `ae-hw-bridge`.

---

## What is a Target Module?

A **Target Module** encapsulates all hardware knowledge and automated workflows for a specific board under test:
- GPIO pin assignments for reset, recovery, power.
- Atomic MicroPython control functions executed on the ESP32 bridge (via `@repl`).
- High-level orchestrations that combine hardware pulses with console verification.

Every target subclasses `BaseTarget` and automatically inherits the 5 core bridge operations (`send_target_command`, `read_target_console`, `wait_for_console_pattern`, `clear_target_console`, `run_custom_code`).

---

## Directory Structure

```
my_jetson_project/
└── .ae-hw-bridge/targets/jetson/
    ├── README.md           # Documentation for your target
    └── target.py           # Python class JetsonTarget(BaseTarget)
```

---

## How It Works

### 1. Atomic MicroPython Methods with `@repl`

Decorating a method with `@repl` lets you write clean Python functions with IDE autocompletion, type hints, and linting. Under the hood, the method body and its parameters are serialized and executed in ESP32 RAM via the Raw REPL:

```python
from ae_hw_bridge.targets.base import BaseTarget, repl

class JetsonTarget(BaseTarget):
    PIN_RST = 12

    @repl
    def trigger_reset(self, pin: int = 12, duration: float = 0.5):
        """Send hardware reset pulse via MicroPython on ESP32."""
        import time
        from machine import Pin
        rst = Pin(pin, Pin.OUT, value=1)
        rst.value(0)
        time.sleep(duration)
        rst.value(1)
```

### 2. High-Level Operations

Methods on your target class combine hardware pulses with console verification:

```python
    def full_reboot(self, timeout: float = 45.0) -> dict:
        """Clear console, pulse reset, and wait for Linux login prompt."""
        self.clear_target_console()
        self.trigger_reset(pin=self.PIN_RST)
        res = self.wait_for_console_pattern("login:", timeout=timeout)
        return {"booted": res.get("matched", False), "line": res.get("line")}
```

### 3. Automatic MCP Tool Registration

When you launch `ae-hw-bridge-mcp`:
```bash
ae-hw-bridge-mcp --target ./targets/jetson
```
FastMCP discovers `JetsonTarget` and registers all operations with the target prefix:
- **Power & Reset**: `jetson_hardware_reset`, `jetson_full_reboot`, `jetson_software_reboot`, `jetson_power_button`
- **Bootloader & Boot**: `jetson_reboot_to_bootloader`, `jetson_wait_for_boot`
- **Shell & Session**: `jetson_wait_for_shell`, `jetson_login`, `jetson_exec_command_with_status`, `jetson_check_alive`
- **Flashing & Recovery**: `jetson_enter_recovery`, `jetson_check_recovery_mode`
- **Telemetry & Diagnostics**: `jetson_get_network_info`, `jetson_get_system_info`
- **Core inherited tools**: `jetson_send_target_command`, `jetson_read_target_console`, `jetson_wait_for_console_pattern`
- Internal hardware methods decorated with `@repl` (`trigger_reset`, `trigger_recovery`, `trigger_power_button`) are executed directly on ESP32 RAM and kept private (NOT exposed as agent tools).

---

## Guaranteed Fallback

If `--target` points to an empty folder or a file with syntax errors:
- `TargetLoader` logs a warning.
- The server falls back to `BaseTarget` without crashing.
- All 5 core bridge tools remain fully available to AI agents.

