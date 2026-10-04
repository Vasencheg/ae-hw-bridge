# AE-HW-BRIDGE (Agents Engine Hardware Bridge)

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python: >=3.10](https://img.shields.io/badge/Python->=3.10-blue.svg)](https://www.python.org/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-FastMCP-green.svg)](https://modelcontextprotocol.io/)

Hardware-in-the-Loop (HIL) automation gateway and FastMCP server for the **Agents Engine (`ae`)** ecosystem.

`ae-hw-bridge` provides AI agents (Agents Engine, Claude, Gemini, etc.) with a safe, programmable, tool-based interface to interact with physical development boards (DUTs) via **Model Context Protocol (MCP)**.

It pairs with the [**`hw-puppet`**](https://github.com/Vasencheg/hw-puppet) Dual-CDC USB bridge. Precompiled firmware binaries are available on the [**hw-puppet Releases**](https://github.com/Vasencheg/hw-puppet/releases) page.

---

## 1. System Architecture

```text
[ AI Agent / Agents Engine (ae) / Claude / Gemini ]
                      │
                      │ stdio / SSE (JSON-RPC via Model Context Protocol)
                      ▼
         [ ae-hw-bridge FastMCP Server ]
                      │
                      │ Unix Domain Socket IPC (/tmp/ae-hw-bridge.sock)
                      ▼
            [ ae-hw-bridge Daemon ]
       ├─ Bounded Circular Log Buffer (collections.deque)
       ├─ Target Domain Logic (.ae-hw-bridge/targets/)
       │
       ├─── CDC 0: MicroPython Raw REPL (/dev/hw-puppet-control or /dev/ttyACM0) ──┐
       └─── CDC 1: Target UART Console  (/dev/hw-puppet-uart or /dev/ttyACM1)    ─┐│
                                                                                 ││ (USB Full-Speed)
                                                                                 ▼▼
                                                                       [ hw-puppet (ESP32-S3) ]
                                                                       (Firmware & HIL Adapter)
                                                                             │        │
                                                                  (Control lines)  (TX/RX UART)
                                                                             ▼        ▼
                                                                     [ Target Dev Board ]
                                                                 (NVIDIA Jetson, Pi, etc.)
```

### Key Principles
* **Separation of Concerns:** Hardware/firmware lives in [`hw-puppet`](../hw-puppet), while high-level orchestration, IPC multiplexing, and MCP tools live in `ae-hw-bridge`.
* **Zero Host Contention:** An auto-spawning, single-owner daemon (`ae-hw-bridge-daemon`) manages exclusive access to the serial devices. Multiple agents and CLI clients connect via non-blocking Unix domain socket IPC.
* **Agent Safety:** Internal `@repl` methods are filtered out from MCP exposure; agents interact strictly through vetted, high-level business tools (`full_reboot`, `login`, `send_target_command`, `wait_for_console_pattern`, etc.).
* **Dynamic Target Loading:** Target behavior (pin definitions, boot sequences, login credentials) is defined modularly in project repositories under `.ae-hw-bridge/targets/<target_name>/target.py`.

---

## 2. FastMCP Tools Exposed to Agents

When connected, agents receive the following MCP tools:

| MCP Tool | Description |
|:---|:---|
| `get_bridge_info()` | Software version, connected ESP32-S3 `hw-puppet` firmware info, and serial port paths. |
| `read_target_console(tail_lines=50, head_lines=None, grep=None)` | Read lines from circular console buffer (passive UART reception). |
| `send_target_command(command, wait_timeout=5.0, idle_threshold=0.3)` | Send interactive shell command to target UART and capture delta response. |
| `wait_for_console_pattern(pattern, timeout=30.0, check_history=True)` | Wait for regex pattern on console stream (e.g. login prompt, bootloader). |
| `clear_target_console()` | Clear the background circular console buffer. |
| `run_custom_code(code, timeout=10.0)` | Execute custom MicroPython script in ESP32-S3 RAM via raw REPL (zero flash wear). |
| *Custom Target Tools* | Any public method declared in `target.py` (e.g. `login`, `get_system_info`, `software_reboot`). |

---

## 3. Installation & Setup

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

### Configure Serial Ports & Device Naming
By default, `ae-hw-bridge` automatically detects ports in the following priority order:
1. Environment variables `HW_PUPPET_CONTROL_PORT` / `HW_PUPPET_UART_PORT`
2. Standard **HW Puppet** Udev symlinks: `/dev/hw-puppet-control` / `/dev/hw-puppet-uart`
3. Standard Linux CDC fallbacks: `/dev/ttyACM0` and `/dev/ttyACM1`

#### Device Naming Reference Matrix
| Context | Name / Identifier | Purpose |
|:---|:---|:---|
| **Hardware Platform** | **HW Puppet** (`hw-puppet`) | Dedicated ESP32-S3 test harness firmware |
| **USB Manufacturer** | `HW-Puppet` | USB Device Descriptor manufacturer |
| **USB Product** | `HW-PUPPET` | USB Device Descriptor product |
| **Control Interface** | `HW-PUPPET REPL` (`/dev/hw-puppet-control`) | CDC 0: MicroPython Raw REPL RPC |
| **UART Interface** | `HW-PUPPET UART Bridge` (`/dev/hw-puppet-uart`) | CDC 1: Transparent target console |
| **Host Package** | `ae-hw-bridge` (`ae_hw_bridge`) | FastMCP server, background daemon, and orchestrator |

#### Linux Udev Setup (Recommended)
Install the provided udev rules to enable non-root access and persistent device names:
```bash
sudo cp udev/99-hw-puppet.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

---

## 4. MCP Client Configuration

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

### Standalone Daemon (Optional)
The server automatically launches the background daemon if it is not already running. To run the daemon manually in foreground for debugging:
```bash
ae-hw-bridge-daemon --idle-timeout 0
```

---

## 5. Target Definitions

Target dev boards are configured in `.ae-hw-bridge/targets/`:

```python
# .ae-hw-bridge/targets/jetson/target.py
import time
from machine import Pin
from ae_hw_bridge.targets.base import BaseTarget, repl, target_tool

class JetsonTarget(BaseTarget):
    name = "jetson"

    @repl
    def reset_pulse(self):
        rst = Pin(1, Pin.OUT, value=1)
        rst.value(0)
        time.sleep(0.2)
        rst.value(1)

    @target_tool
    def full_reboot(self) -> str:
        """Perform a clean hardware reboot of the Jetson board."""
        self.reset_pulse()
        return "Jetson hardware reboot initiated"
```

---

## 6. Running Tests

```bash
pytest
```
36 unit and integration tests covering the console reader, raw REPL client, IPC protocol, daemon server/client, target loader, and MCP tool registration.
