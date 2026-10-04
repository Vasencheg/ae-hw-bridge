# AE-HW-BRIDGE (Agents Engine Hardware Bridge)

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python: >=3.10](https://img.shields.io/badge/Python->=3.10-blue.svg)](https://www.python.org/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-FastMCP-green.svg)](https://modelcontextprotocol.io/)

Hardware-in-the-Loop (HIL) automation gateway and FastMCP server for the **Agents Engine (`ae`)** ecosystem.

`ae-hw-bridge` provides AI agents (Agents Engine, Claude, Gemini, Cursor) with a safe, programmable, tool-based interface to interact with physical development boards (DUTs) via the **Model Context Protocol (MCP)**.

It pairs with the [**`hw-puppet`**](https://github.com/Vasencheg/hw-puppet) Dual-CDC USB bridge. Precompiled firmware binaries are available on the [**hw-puppet Releases**](https://github.com/Vasencheg/hw-puppet/releases) page.

---

## 1. System Architecture

```text
[ AI Agent / Agents Engine (ae) / Claude / Gemini / Cursor ]
                      │
                      │ stdio / SSE (JSON-RPC via Model Context Protocol)
                      ▼
         [ ae-hw-bridge FastMCP Server ]
                      │
                      │ Scoped Unix Domain Sockets (/tmp/ae-hw-bridge-{target}.sock)
                      ▼
            [ ae-hw-bridge Daemon ]
       ├─ Bounded Circular Log Buffer (collections.deque)
       ├─ Target Domain Logic (.ae-hw-bridge/targets/)
       │
       ├─── CDC 0: MicroPython Raw REPL (/dev/ttyACM* or sysfs-paired) ──┐
       └─── CDC 1: Target UART Console  (/dev/ttyACM* or sysfs-paired) ──┐│
                                                                         ││ (USB Full-Speed)
                                                                         ▼▼
                                                               [ hw-puppet (ESP32-S3) ]
                                                               (Firmware & HIL Adapter)
                                                                     │        │
                                                          (Control lines)  (TX/RX UART)
                                                                     ▼        ▼
                                                             [ Target Dev Board ]
                                                         (NVIDIA Jetson, Pi, STM32)
```

### Key Principles
* **Separation of Concerns:** Low-level hardware drivers and USB descriptors live in [`hw-puppet`](https://github.com/Vasencheg/hw-puppet), while high-level orchestration, IPC multiplexing, and MCP tools live in `ae-hw-bridge`.
* **Zero Host Contention:** An auto-spawning, target-scoped daemon (`ae-hw-bridge-daemon`) manages exclusive access to the serial devices. Multiple agents and CLI clients connect via non-blocking Unix domain socket IPC (`/tmp/ae-hw-bridge-{target}.sock`).
* **Multi-Target & Multi-Puppet:** Safely binds multiple physical boards via hardware badges (stored in ESP32-S3 NVS) and Linux sysfs USB pairing without serial port number guessing or symlink collisions.
* **Agent Safety:** Internal `@repl` methods are filtered out from MCP exposure; agents interact strictly through vetted, high-level business tools (`full_reboot`, `login`, `send_target_command`, `wait_for_console_pattern`, etc.).
* **Dynamic Target Loading:** Target behavior (pin definitions, boot sequences, login credentials) is defined modularly under `.ae-hw-bridge/targets/<target_name>/target.py` or `.ae-hw-bridge/config.yml`.

---

## 2. FastMCP Tools & Naming Convention

Tools exposed to AI agents adhere to a strict and predictable prefix convention:

* **When target modules are configured** (e.g. `jetson`, `stm32`), all tools are **strictly prefixed** with `<target>_`:
  * `jetson_read_target_console`
  * `jetson_send_target_command`
  * `jetson_full_reboot`, `jetson_enter_recovery`
  * `stm32_read_target_console`, `stm32_flash_firmware`
  This guarantees consistent agent tool contracts regardless of whether a project has 1 or N targets.
* **In clean bench mode** (no targets defined, single `BaseTarget`), tools carry **no prefix**:
  * `read_target_console`
  * `send_target_command`
  * `clear_target_console`

### Core Inherited Tools

| MCP Tool | Description |
|:---|:---|
| `get_bridge_info()` | Software version, connected ESP32-S3 `hw-puppet` firmware metadata, badge, and port paths. |
| `read_target_console(tail_lines=50, head_lines=None, grep=None)` | Read lines from circular console buffer (passive UART reception). |
| `send_target_command(command, wait_timeout=5.0, idle_threshold=0.3)` | Send interactive shell command to target UART and capture delta response. |
| `wait_for_console_pattern(pattern, timeout=30.0, check_history=True)` | Wait for regex pattern on console stream (e.g. login prompt, bootloader). |
| `clear_target_console()` | Clear the background circular console buffer. |
| `run_custom_code(code, timeout=10.0)` | Execute custom MicroPython script in ESP32-S3 RAM via raw REPL (zero flash wear). |
| *Custom Target Tools* | Any public method declared in `target.py` (e.g. `login`, `software_reboot`, `enter_recovery` — see [Jetson Example](examples/targets/jetson/README.md)). |

---

## 3. Configuration (`.ae-hw-bridge/config.yml`)

You can configure hardware mappings in `.ae-hw-bridge/config.yml`:

### Single Target / Clean Bench
```yaml
# Match connected board by persistent hardware badge:
puppet: jetson

# Or bind directly by port:
# port: /dev/ttyACM1
```

### Multi-Target Setup
```yaml
targets:
  jetson:
    puppet: jetson-desk     # Matches HW-Puppet with badge "jetson-desk"
  stm32:
    puppet: stm32-bench    # Matches HW-Puppet with badge "stm32-bench"
    port: /dev/ttyACM5     # Or explicit port
```

> [!IMPORTANT]
> **Fail-Safe Ambiguity Protection:**
> If multiple HW-Puppet boards are plugged into your machine and no configuration or explicit port is provided, `ae-hw-bridge` safely halts with an informative error rather than guessing a port randomly.

---

## 4. CLI Utilities

`ae-hw-bridge` provides built-in CLI commands for managing hardware test benches:

```bash
# List all connected HW-Puppet devices, serial numbers, badges, and configured targets
ae-hw-bridge list

# Set a persistent hardware badge in ESP32-S3 NVS
ae-hw-bridge label jetson-bench
ae-hw-bridge label stm32-bench --port /dev/ttyACM1

# Run the FastMCP server
ae-hw-bridge

# Stop a running background daemon and release serial ports
ae-hw-bridge-daemon --stop
ae-hw-bridge-daemon --name jetson --stop
```

---

## 5. Installation & Setup

### Prerequisites
* Linux (with udev support)
* Python 3.10+
* A connected **`hw-puppet`** (ESP32-S3) device

### Automated Installation (Smithery)
Install automatically to your preferred AI assistant using the Smithery CLI:
```bash
# Claude Desktop / Claude Code
npx -y @smithery/cli install ae-hw-bridge --client claude

# Cursor
npx -y @smithery/cli install ae-hw-bridge --client cursor
```

### Zero-Install Execution (uvx)
Run the MCP server directly using `uvx` without installing into your local Python environment:
```bash
uvx ae-hw-bridge
```

### Manual Installation
```bash
pip install ae-hw-bridge
# Or editable mode for development:
pip install -e .
```

### Linux Udev Setup (Recommended)
Install the provided udev rules to enable non-root access for all HW-Puppet CDC devices:
```bash
sudo cp udev/99-hw-puppet.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

---

## 6. MCP Client Configuration

### Claude Code CLI
```bash
claude mcp add ae-hw-bridge uvx ae-hw-bridge
```

### Claude Desktop (`claude_desktop_config.json`) / Cursor (`~/.cursor/mcp.json`)
```json
{
  "mcpServers": {
    "ae-hw-bridge": {
      "command": "uvx",
      "args": ["ae-hw-bridge"]
    }
  }
}
```

---

## 7. Target Definitions

Target dev boards (DUTs) are configured modularly in `.ae-hw-bridge/targets/<target_name>/`.

> [!TIP]
> **Complete Target Example:**
> See the [**NVIDIA Jetson Target Example (`examples/targets/jetson/README.md`)**](examples/targets/jetson/README.md) and its [target implementation (`target.py`)](examples/targets/jetson/target.py).
> It provides a production-grade reference showing:
> - Hardware reset & power button sequencing with MicroPython `@repl`
> - Entering NVIDIA Force Recovery mode for flashing
> - Rebooting directly to bootloader
> - Automated Linux shell login state machine (`wait_for_shell`, `login`)
> - Network and system telemetry retrieval (`get_network_info`, `get_system_info`)
> - WS2812 RGB LED board status indication

### Quick Minimal Target Example

```python
# .ae-hw-bridge/targets/jetson/target.py
import time
from machine import Pin
from ae_hw_bridge.targets.base import BaseTarget, repl

class JetsonTarget(BaseTarget):
    name = "jetson"

    @repl
    def reset_pulse(self):
        """MicroPython method executed directly in ESP32 RAM (hidden from MCP)."""
        rst = Pin(1, Pin.OUT, value=1)
        rst.value(0)
        time.sleep(0.2)
        rst.value(1)

    def full_reboot(self) -> str:
        """High-level target operation automatically registered as an MCP tool."""
        self.reset_pulse()
        return "Jetson hardware reboot initiated"
```

---

## 8. Running Tests

```bash
pytest
```
55 unit and integration tests covering the console reader, raw REPL client, IPC protocol, daemon server/client, target loader, sysfs discovery, YAML/JSON configuration, and MCP tool registration.
