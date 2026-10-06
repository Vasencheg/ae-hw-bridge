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
[ AI Agent / Claude / Cursor ]          [ Human Developer (Terminal) ]
             │                                        │
             │ stdio / SSE (MCP JSON-RPC)             │ Virtual PTY (/tmp/ae-hw-bridge-uart)
             ▼                                        ▼
   [ FastMCP Server ]                    [ tio / picocom / ae-hw-bridge console ]
             │                                        │
             │ Scoped Unix Domain Sockets             │
             └───────────────────┬────────────────────┘
                                 ▼
                     [ ae-hw-bridge Daemon ]
             ├─ Bounded Circular Log Buffer (collections.deque)
             ├─ Virtual Pseudoterminal Mirror (Master/Slave PTY)
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
* **Zero Host Contention & Virtual PTY:** An auto-spawning, target-scoped daemon (`ae-hw-bridge-daemon`) manages exclusive access to physical serial devices and exposes a virtual pseudoterminal symlink (`/tmp/ae-hw-bridge-uart`). Developers can watch UART logs live (via `tio`, `minicom`, or `ae-hw-bridge console`) concurrently with AI agents running tasks without port collision (`Device or resource busy`).
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

## 4. CLI Utilities & Virtual UART Console

`ae-hw-bridge` provides built-in CLI commands for managing hardware test benches and viewing UART logs without serial port contention:

```bash
# Connect to live target UART console (interactive terminal session via tio / picocom / built-in)
ae-hw-bridge console

# Inspect recent UART console logs (non-interactive, exit immediately)
ae-hw-bridge console -n 50

# Continuously follow live console logs
ae-hw-bridge console -f

# Filter and pipe live output into standard Unix tools
ae-hw-bridge console | grep "ERROR"
ae-hw-bridge console -n 100 --grep "kernel"

# External terminal access (works simultaneously with AI agents!):
# Use your favorite terminal tool directly via the virtual PTY symlink:
tio /tmp/ae-hw-bridge-uart
picocom -b 115200 /tmp/ae-hw-bridge-uart

# View daemon status, connected MCP clients, physical ports, and virtual PTY
ae-hw-bridge status

# Stop background daemons and release serial ports
ae-hw-bridge stop
ae-hw-bridge stop --all

# List all connected HW-Puppet devices, serial numbers, badges, and configured targets
ae-hw-bridge list

# Set a persistent hardware badge in ESP32-S3 NVS
ae-hw-bridge label jetson-bench
ae-hw-bridge label stm32-bench --port /dev/ttyACM1

# Run the FastMCP server
ae-hw-bridge
```

> [!TIP]
> * For full command-line usage and daemon management, see the [CLI Reference Guide](docs/cli.md).
> * For in-depth architectural details, virtual PTY sharing, and external terminal setup (`tio`, `minicom`), see the [Virtual UART Console Guide](docs/virtual_console.md).

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
> **Complete Target Guide & Examples:**
> - See the in-depth **[Target Development Guide (`docs/target_development.md`)](docs/target_development.md)** for architecture details, `@repl` execution rules, and ready-to-use recipes for ESP32, STM32, and Linux SBCs.
> - See the **[NVIDIA Jetson Reference Target (`examples/targets/jetson/README.md`)](examples/targets/jetson/README.md)** and its [implementation (`target.py`)](examples/targets/jetson/target.py).
> - For AI assistants and agents, see **[`llms.txt`](llms.txt)**.

### Quick Target Example

```python
# .ae-hw-bridge/targets/jetson/target.py
from typing import Any
from ae_hw_bridge.targets.base import BaseTarget, repl

class JetsonTarget(BaseTarget):
    PIN_RST: int = 12

    @repl
    def reset_pulse(self, pin: int = 12):
        """MicroPython code executed directly in HW-Puppet RAM (hidden from MCP)."""
        import time
        from machine import Pin  # MicroPython imports MUST be inside @repl!

        rst = Pin(pin, Pin.OUT, value=1)
        rst.value(0)
        time.sleep(0.2)
        rst.value(1)

    def full_reboot(self, timeout: float = 30.0) -> dict[str, Any]:
        """High-level target operation automatically registered as an MCP tool."""
        self.clear_target_console()
        self.reset_pulse(pin=self.PIN_RST)
        res = self.wait_for_console_pattern(r"login:", timeout=timeout)
        return {"booted": res.get("matched", False), "line": res.get("line")}
```

---

## 8. Running Tests

```bash
pytest
```
55 unit and integration tests covering the console reader, raw REPL client, IPC protocol, daemon server/client, target loader, sysfs discovery, YAML/JSON configuration, and MCP tool registration.
