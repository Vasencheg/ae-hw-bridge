# Target Development Guide (`target.py`)

This guide explains how to develop custom target definitions (`target.py`) for the **`ae-hw-bridge`** ecosystem, enabling AI agents (Cursor, Claude, Gemini, Agents Engine) to safely control, flash, reset, and monitor physical development boards (DUTs).

---

## 1. System Architecture & The 3 Entities

When designing a target, always distinguish between the **three separate physical entities**:

```text
┌────────────────────────────────────────────────────────┐
│ 1. Host Computer (Developer PC / CI Runner)           │
│    - Runs CPython 3.10+                                │
│    - Executes: ae-hw-bridge FastMCP Server & Daemon    │
│    - Evaluates: target.py (High-level orchestration)   │
└───────────────────────────┬────────────────────────────┘
                            │ USB (CDC0 REPL + CDC1 Console)
┌───────────────────────────▼────────────────────────────┐
│ 2. HW-Puppet (Bridge Controller: ESP32-S3)             │
│    - Runs MicroPython v1.23+ with hw-puppet C-module   │
│    - Executes: @repl methods directly in MCU RAM       │
│    - Interfaces: Hardware GPIOs, Relays, Status LEDs   │
└───────────────────────────┬────────────────────────────┘
                            │ Hardware Control Lines (Reset, Boot) + UART TX/RX
┌───────────────────────────▼────────────────────────────┐
│ 3. Target Board (DUT: Device Under Test)               │
│    - ESP32, STM32, NVIDIA Jetson, Raspberry Pi, etc.   │
│    - The board being tested, flashed, or debugged       │
└────────────────────────────────────────────────────────┘
```

* **Host Computer:** Runs your `target.py` using standard CPython.
* **HW-Puppet:** The intermediate bridge board connected via USB. It drives control pins and reads UART.
* **Target Board (DUT):** The board you are developing for.

---

## 2. File Location & Naming

Place target definitions in your project's `.ae-hw-bridge/targets/` directory:

```text
my-project/
├── .ae-hw-bridge/
│   ├── config.yml                      # Optional multi-target / badge configuration
│   └── targets/
│       ├── esp32/
│       │   └── target.py              # Loaded as target "esp32"
│       └── jetson/
│           └── target.py              # Loaded as target "jetson"
```

* The directory name (`esp32`, `jetson`, `stm32`) becomes the target name.
* When targets are loaded, all public tools are automatically prefixed with `<target>_` (e.g. `esp32_full_reboot`, `jetson_hardware_reset`).

---

## 3. Anatomy of a `target.py`

Every custom target inherits from `BaseTarget`:

```python
from typing import Any, Optional
from ae_hw_bridge.targets.base import BaseTarget, repl

class MyTarget(BaseTarget):
    """Docstring explaining the target device."""
    
    # 1. Hardware Pin Definitions on HW-Puppet
    PIN_RST: int = 4
    PIN_BOOT: int = 5
    PIN_LED: int = 48

    # 2. Low-level Hardware Primitives (@repl)
    @repl
    def pulse_reset(self, pin: int = 4, duration: float = 0.2):
        """Executed inside HW-Puppet ESP32-S3 RAM via MicroPython."""
        import time
        from machine import Pin

        rst = Pin(pin, Pin.OUT, value=1)
        rst.value(0)
        time.sleep(duration)
        rst.value(1)

    # 3. High-level Tools (Exposed to AI Agents via FastMCP)
    def full_reboot(self, timeout: float = 10.0) -> dict[str, Any]:
        """Reset the target board and verify boot sequence."""
        self.clear_target_console()
        self.pulse_reset(pin=self.PIN_RST)
        res = self.wait_for_console_pattern(r"boot|ready", timeout=timeout)
        return {
            "booted": res.get("matched", False),
            "line": res.get("line"),
        }
```

---

## 4. Understanding `@repl` (Remote MicroPython Execution)

### What `@repl` IS:
`@repl` is an **AST/inspect-based remote code execution bridge**. It allows you to write Python functions on your host PC that execute **natively inside the MicroPython VM of HW-Puppet** over USB CDC 0 Raw REPL.

1. When a method decorated with `@repl` is called on the host, `ae-hw-bridge` inspects the function body.
2. It binds all caller arguments (`pin=4`, `duration=0.2`) as MicroPython variables.
3. It sends the code over CDC 0 Raw REPL to HW-Puppet's RAM.
4. HW-Puppet executes the GPIO manipulation at microsecond hardware speed.
5. Execution result (`ExecutionResult(ok=True, stdout=..., stderr=...)`) is returned to the host.

### What `@repl` is NOT:
* `@repl` is **NOT** a tool registration decorator.
* `@repl` methods are **hidden from AI agents** in MCP. This prevents agents from accidentally sending arbitrary GPIO pulses; agents must call your high-level business methods instead.

### ⚠️ The Golden Rule of `@repl` Imports:
> **NEVER import MicroPython modules (`machine`, `neopixel`, `esp32`) at the top of `target.py`!**
> The top level of `target.py` runs on your **host PC** under standard CPython, where `machine` does not exist.
> Always put MicroPython imports **inside** the `@repl` method body:

```python
# ❌ INCORRECT - Will crash with ModuleNotFoundError on host!
from machine import Pin
from ae_hw_bridge.targets.base import BaseTarget, repl

# ✅ CORRECT - Runs safely inside MicroPython on HW-Puppet
class MyTarget(BaseTarget):
    @repl
    def toggle_pin(self, pin: int = 12):
        from machine import Pin  # <--- Imported inside @repl
        p = Pin(pin, Pin.OUT)
        p.value(not p.value())
```

---

## 5. Built-in Core Capabilities (Inherited from `BaseTarget`)

Your target inherits all standard console and communication methods automatically. You never need to write raw serial reading or byte-buffering logic:

| Inherited Method | Description |
|:---|:---|
| `self.clear_target_console()` | Clears the circular console buffer. Always call this before a reset if you plan to wait for fresh boot logs. |
| `self.read_target_console(tail_lines=50, head_lines=None, grep=None)` | Returns buffered lines received from target UART (CDC 1). |
| `self.send_target_command(command, wait_timeout=5.0, idle_threshold=0.3)` | Sends a command string to the target console and captures delta output until idle. |
| `self.wait_for_console_pattern(pattern, timeout=30.0, check_history=True)` | Blocks until a regex pattern (e.g. `login:`, `rst:`) appears in the console stream. |
| `self.run_custom_code(code, timeout=10.0)` | Sends arbitrary MicroPython code to HW-Puppet RAM. |
| `self.get_bridge_info()` | Queries hardware badge, firmware version, and active port paths. |

---

## 6. High-Level Tools & Agent Contracts

Every public method defined on your `Target` class (that is **not** decorated with `@repl` and does not start with `_`) is automatically exposed as an MCP tool to AI agents.

### Guidelines for High-Level Methods:
1. **Return Standard Python Types:** Return `dict[str, Any]`, `str`, `int`, or `bool`. Do **not** invent wrapper classes like `TargetResult`. FastMCP converts standard types into clean JSON Schema for LLMs.
2. **Type Annotations & Docstrings:** Write comprehensive docstrings and type annotations. FastMCP parses them directly into the tool description and parameter schema seen by Claude, Cursor, and Gemini.

```python
def full_reboot(self, timeout: float = 45.0) -> dict[str, Any]:
    """Hardware reset target and wait for Linux login prompt to confirm boot.

    Args:
        timeout: Maximum seconds to wait for boot completion (default: 45.0s).
    """
    self.clear_target_console()
    self.pulse_reset()
    res = self.wait_for_console_pattern(r"login:", timeout=timeout)
    return {
        "booted": res.get("matched", False),
        "login_prompt": res.get("line"),
    }
```

---

## 7. Production Recipes

### Recipe 1: ESP32 / ESP32-S3 Target (`targets/esp32/target.py`)
Controls hardware EN (Reset) and IO0 (Boot/Download) for automated flashing with `esptool.py` and boot validation:

```python
"""Target configuration for ESP32 / ESP32-S3 development boards."""

import time
from typing import Any
from ae_hw_bridge.targets.base import BaseTarget, repl

class Esp32Target(BaseTarget):
    """Target controller for ESP32 / ESP32-S3 boards."""

    PIN_EN: int = 4     # EN (Reset) active-LOW
    PIN_BOOT: int = 5   # IO0 (Boot) active-LOW
    PIN_LED: int = 48   # HW-Puppet status WS2812 RGB LED

    @repl
    def set_status_led(self, r: int, g: int, b: int, pin: int = 48):
        import neopixel
        from machine import Pin
        try:
            np = neopixel.NeoPixel(Pin(pin, Pin.OUT), 1)
            np[0] = (r, g, b)
            np.write()
        except Exception:
            pass

    @repl
    def trigger_reset(self, pin: int = 4, duration: float = 0.2):
        import time
        from machine import Pin
        en = Pin(pin, Pin.OUT, value=1)
        en.value(0)
        time.sleep(duration)
        en.value(1)

    @repl
    def trigger_bootloader_mode(self, en_pin: int = 4, boot_pin: int = 5):
        import time
        from machine import Pin
        boot = Pin(boot_pin, Pin.OUT, value=1)
        en = Pin(en_pin, Pin.OUT, value=1)

        # Pull BOOT LOW, pulse EN LOW -> HIGH, release BOOT
        boot.value(0)
        time.sleep(0.05)
        en.value(0)
        time.sleep(0.15)
        en.value(1)
        time.sleep(0.05)
        boot.value(1)

    # --- High-level MCP Tools ---

    def hardware_reset(self, duration: float = 0.2) -> dict[str, Any]:
        """Pulse EN pin to reboot the ESP32 chip immediately."""
        self.set_status_led(60, 0, 0, pin=self.PIN_LED)
        self.clear_target_console()
        res = self.trigger_reset(pin=self.PIN_EN, duration=duration)
        self.set_status_led(0, 60, 0, pin=self.PIN_LED)
        return {"ok": res.ok}

    def enter_bootloader(self) -> dict[str, Any]:
        """Put target ESP32 into ROM Bootloader mode for esptool.py flashing."""
        self.set_status_led(50, 0, 50, pin=self.PIN_LED)  # Purple
        self.clear_target_console()
        res = self.trigger_bootloader_mode(en_pin=self.PIN_EN, boot_pin=self.PIN_BOOT)
        match = self.wait_for_console_pattern(r"waiting for download|download_boot", timeout=2.0)
        return {
            "ok": res.ok,
            "in_download_mode": match.get("matched", False),
            "log": match.get("line"),
        }

    def full_reboot(self, timeout: float = 5.0) -> dict[str, Any]:
        """Reboot ESP32 and wait for ROM/RTOS bootloader banner."""
        self.clear_target_console()
        self.set_status_led(60, 0, 0, pin=self.PIN_LED)
        self.trigger_reset(pin=self.PIN_EN, duration=0.2)
        self.set_status_led(50, 25, 0, pin=self.PIN_LED)

        res = self.wait_for_console_pattern(r"rst:0x[0-9a-fA-F]+.*boot:0x[0-9a-fA-F]+|ESP-IDF|MicroPython", timeout=timeout)
        booted = res.get("matched", False)
        self.set_status_led(0, 60, 0 if booted else 0, pin=self.PIN_LED)
        return {"booted": booted, "banner": res.get("line"), "timeout": timeout}

    def check_alive(self) -> dict[str, Any]:
        """Check if target firmware responds over serial console."""
        resp = self.send_target_command("", wait_timeout=1.0)
        return {"alive": resp != "[No output received]", "response": resp}
```

---

### Recipe 2: Embedded Linux SBC Target (`targets/jetson/target.py`)
Complete reference implementation available in [`examples/targets/jetson/target.py`](../examples/targets/jetson/target.py), featuring:
* Hardware reset and Force Recovery mode sequencing
* Power button emulation (short press for sleep/wake, long press $\ge 5\text{s}$ for forced shutdown)
* Automated shell login state machine (`login(username, password)`)
* Dynamic network interface detection and IP reporting

---

### Recipe 3: Bare-Metal MCU Target (STM32 / nRF)
```python
"""Target configuration for STM32 microcontrollers."""

import time
from typing import Any
from ae_hw_bridge.targets.base import BaseTarget, repl

class Stm32Target(BaseTarget):
    """Target controller for STM32 development boards."""

    PIN_NRST: int = 18
    PIN_BOOT0: int = 19

    @repl
    def trigger_nrst(self, pin: int = 18, duration: float = 0.1):
        import time
        from machine import Pin
        nrst = Pin(pin, Pin.OUT, value=1)
        nrst.value(0)
        time.sleep(duration)
        nrst.value(1)

    def hardware_reset(self) -> dict[str, Any]:
        """Pulse NRST pin to cold-restart STM32."""
        self.clear_target_console()
        res = self.trigger_nrst(pin=self.PIN_NRST)
        return {"ok": res.ok}

    def wait_for_app_start(self, timeout: float = 3.0) -> dict[str, Any]:
        """Wait for firmware startup banner on UART."""
        res = self.wait_for_console_pattern(r"SystemInit|App Start|main\(\)", timeout=timeout)
        return {"started": res.get("matched", False), "line": res.get("line")}
```

---

## 8. Development Checklist

Before deploying a custom target:

- [ ] `from machine import Pin` is **inside** `@repl` functions, **never** at module level.
- [ ] Low-level GPIO functions are decorated with `@repl`.
- [ ] High-level orchestrations call `self.clear_target_console()` before triggering reset pulses if awaiting fresh boot logs.
- [ ] Public methods have clear type hints and docstrings.
- [ ] Methods return standard Python types (`dict`, `str`, `bool`), not custom wrapper objects.
- [ ] Target file is placed at `.ae-hw-bridge/targets/<name>/target.py`.
- [ ] Verified locally using `ae-hw-bridge` CLI and tested with an MCP client.
