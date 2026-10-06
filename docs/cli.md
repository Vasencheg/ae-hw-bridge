# Command Line Interface (CLI) Guide

`ae-hw-bridge` provides a comprehensive command line utility for managing hardware test benches, monitoring background daemons, and inspecting target UART consoles.

---

## 1. Subcommands Overview

| Command | Description |
|:---|:---|
| `ae-hw-bridge console` | Connect to target UART console (interactive terminal or log stream). |
| `ae-hw-bridge status` | Display status of running daemons, connected clients, ports, and virtual PTY. |
| `ae-hw-bridge stop` | Gracefully stop background daemons and release physical serial ports. |
| `ae-hw-bridge list` | List connected HW-Puppet boards, badges, and configured targets. |
| `ae-hw-bridge label` | Assign a persistent hardware badge name in ESP32-S3 NVS. |
| `ae-hw-bridge` | Run the FastMCP server (used by AI assistants like Claude Desktop, Cursor). |

---

## 2. `ae-hw-bridge console`

Connects to the target UART console for live monitoring, log searching, or interactive debugging.

> [!TIP]
> For deep-dive architectural details on virtual pseudoterminal (PTY) sharing, external tool integrations (`tio`, `minicom`), and non-blocking multi-client pass-through, see the [Virtual UART Console Guide](virtual_console.md).

### Usage
```bash
ae-hw-bridge console [options]
```

### Options
* `-t, --target <NAME>`: Target name (default: auto-detected or first configured target).
* `-n, --lines <N>`: Output the last `N` lines from the daemon's ring buffer and exit immediately.
* `-f, --follow`: Continuously stream live console output (like `tail -f`).
* `--grep <PATTERN>`: Filter output lines by regular expression or substring.
* `--builtin`: Force the built-in raw terminal instead of launching external tools (`tio`/`picocom`).

### Execution Modes

#### 1. Interactive Terminal Mode (`ae-hw-bridge console`)
When run directly in a terminal without `-n` or `-f`:
* Connects full-duplex standard I/O to the virtual UART PTY `/tmp/ae-hw-bridge-uart`.
* Launches `tio` or `picocom` if installed, or falls back to the built-in raw terminal.
* You can send keystrokes, press `Enter` to wake up the board's prompt, and run interactive shell commands.
* Exit anytime with `Ctrl-]` or `Ctrl-Q`.

#### 2. Ring Buffer Snapshot Mode (`ae-hw-bridge console -n <N>`)
Outputs the last `N` lines stored in the daemon's in-memory ring buffer and exits immediately:
```bash
ae-hw-bridge console -n 50
ae-hw-bridge console -n 50 --grep "error"
```
> [!NOTE]
> **Why is the buffer empty on a freshly started daemon?**  
> The background daemon's ring buffer records UART data received while the daemon is running. If the daemon just started and your target board has already booted and is sitting silently at a shell prompt (e.g. `nvidia@tegra-ubuntu:~$ `), no UART bytes are transmitted by the target board until activity occurs.  
> To wake up the prompt, enter interactive mode once (`ae-hw-bridge console`) and press `Enter`. Subsequent commands like `ae-hw-bridge console -n 10` will immediately show the prompt.

#### 3. Continuous Streaming & Pipeline Mode (`ae-hw-bridge console -f` or piped)
When piped (`ae-hw-bridge console | grep ...`) or run with `-f`:
* Streams live console output continuously as UART bytes arrive from the target board (like `tail -f`).
* Informational status messages (such as `Streaming live console output...`) are printed to `stderr` so as not to pollute pipes.
* Downstream filters like `grep` wait for matching lines. If the target is idle and produces no matching lines, the pipeline waits until output is emitted.
```bash
# Stream all live output in real time:
ae-hw-bridge console -f

# Filter live output for errors as they occur:
ae-hw-bridge console | grep --line-buffered "ERROR"

# Search existing buffer history without waiting:
ae-hw-bridge console -n 100 | grep "ERROR"
```

---

## 3. `ae-hw-bridge status`

Inspects the state of running `ae-hw-bridge` background daemons, connected MCP clients (AI agents), physical serial ports, and virtual PTY paths.

### Usage
```bash
ae-hw-bridge status [options]
```

### Options
* `-t, --target <NAME>`: Specific target daemon to inspect.
* `--json`: Format output as machine-readable JSON.

### Examples & Output

#### When a Daemon is Active
```bash
$ ae-hw-bridge status
AE-HW-BRIDGE Daemon Status:
============================================================
Target:          base
PID:             14208
Uptime:          125.4s
MCP Clients:     1
Control Port:    /dev/ttyACM0 (MicroPython CDC0)
UART Port:       /dev/ttyACM1 (Target Console CDC1)
Virtual Console: /tmp/ae-hw-bridge-uart -> /dev/pts/3
HW-Puppet:       badge='jetson', firmware=0.2.0
============================================================
```

#### When No Daemons are Running (Hardware Discovery)
When all daemons are stopped, `status` probes the USB bus and queries connected HW-Puppet boards directly:
```bash
$ ae-hw-bridge status
No active AE-HW-BRIDGE daemons running.

Connected hardware check:
  - Control: /dev/ttyACM0 | UART: /dev/ttyACM1 | Badge: 'jetson' (v0.2.0)
```

#### JSON Output (For Automation / CI)
```bash
$ ae-hw-bridge status --json
[
  {
    "target": "base",
    "pid": 14208,
    "uptime_seconds": 125.4,
    "clients_count": 1,
    "control_port": "/dev/ttyACM0",
    "uart_port": "/dev/ttyACM1",
    "pty_path": "/tmp/ae-hw-bridge-uart",
    "pty_target": "/dev/pts/3",
    "version": "0.3.0",
    "firmware_info": {
      "badge": "jetson",
      "version": "0.2.0"
    }
  }
]
```

---

## 4. `ae-hw-bridge stop`

Gracefully terminates background daemons, cleans up Unix domain sockets, lock files, and virtual PTY symlinks, and releases physical USB serial ports.

### Usage
```bash
ae-hw-bridge stop [options]
```

### Options
* `-t, --target <NAME>`: Stop the daemon for a specific target.
* `--all`: Stop all active daemons across all targets.

### Examples
```bash
# Stop default daemon:
$ ae-hw-bridge stop
AE-HW-BRIDGE daemon 'base' stopped. Hardware ports released.

# Stop named target daemon:
$ ae-hw-bridge stop -t jetson
AE-HW-BRIDGE daemon 'jetson' stopped. Hardware ports released.

# Stop all running daemons:
$ ae-hw-bridge stop --all
```

---

## 5. `ae-hw-bridge list`

Scans the Linux sysfs subsystem for connected HW-Puppet boards (USB VID `0x303a`, PID `0x4002`), probes their NVS badges over CDC0, and displays configured targets from `.ae-hw-bridge/config.yml`.

### Usage
```bash
$ ae-hw-bridge list
Scanning connected HW-Puppet devices...
Found 1 connected HW-Puppet device(s):
  1. Control: /dev/ttyACM0 | UART: /dev/ttyACM1
     Badge  : 'jetson'
     Serial : 744dbd8a52300000
     Version: 0.2.0
```

---

## 6. `ae-hw-bridge label`

Writes a persistent hardware badge string to ESP32-S3 Non-Volatile Storage (NVS). This allows automatic, fail-safe port binding in multi-target test benches.

### Usage
```bash
ae-hw-bridge label <BADGE> [--port <CDC0_PORT>]
```

### Examples
```bash
# Auto-detects CDC0 and assigns badge:
$ ae-hw-bridge label jetson
Connecting to /dev/ttyACM0 to set badge 'jetson'...
[/dev/ttyACM0] Persistent badge successfully set to: 'jetson'

# Assign badge using explicit port:
$ ae-hw-bridge label stm32-bench --port /dev/ttyACM2
```

---

## 7. `ae-hw-bridge` (FastMCP Server Mode)

Running `ae-hw-bridge` without subcommands launches the **Model Context Protocol (MCP)** server. This is the entrypoint configured in AI assistants (Claude Desktop, Cursor, Agents Engine).

### Transport Options
```bash
# Standard I/O transport (default for AI assistants):
ae-hw-bridge --transport stdio

# Server-Sent Events (SSE) HTTP transport (for network/remote access):
ae-hw-bridge --transport sse --host 0.0.0.0 --port 8000
```
