# Virtual UART Console Guide

This guide explains the **virtual pseudoterminal (PTY)** architecture in **`ae-hw-bridge`**, enabling human developers and AI coding agents to share hardware serial ports simultaneously without port collision errors (`Device or resource busy`).

---

## 1. The Problem: Serial Port Contention

In traditional hardware development, serial ports (`/dev/ttyACM*`, `/dev/ttyUSB*`) allow only **one process** to open the device file at a time:

```text
[ AI Agent (MCP Server) ] ──> /dev/ttyACM1 (EXCLUSIVE LOCK)
                                    ▲
[ Developer (tio / minicom) ] ──────┘ ──> ERROR: "Device or resource busy"
```

When an AI agent (Claude, Cursor, Gemini, Agents Engine) connects to a target board, the developer cannot inspect boot logs or send manual commands without killing the agent's session or stopping the MCP server.

---

## 2. The Solution: Multi-Client Daemon & Virtual PTY

`ae-hw-bridge` solves this with an auto-spawning background daemon and Linux virtual pseudoterminals (`pty`):

```text
┌──────────────────────────┐      ┌───────────────────────────────┐
│ AI Agent (Claude/Cursor) │      │ Human Developer (Terminal)    │
│  - MCP Tools via stdio   │      │  - tio / picocom / minicom    │
│  - read_target_console   │      │  - ae-hw-bridge console       │
│  - send_target_command   │      │  - ae-hw-bridge console | grep│
└────────────┬─────────────┘      └──────────────┬────────────────┘
             │                                   │
             │ IPC (/tmp/ae-hw-bridge-*.sock)    │ Virtual PTY (/tmp/ae-hw-bridge-uart)
             ▼                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ ae-hw-bridge Daemon                                             │
│  ├─ Bounded Ring Buffer (Memory) ──────> AI Agent Responses     │
│  ├─ PTY Master/Slave Pair ─────────────> Human Terminal Mirror  │
│  └─ CDC 0 / CDC 1 Serial Drivers (Exclusive Hardware Holder)    │
└────────────────────────────┬────────────────────────────────────┘
                             │ USB Full-Speed
┌────────────────────────────▼────────────────────────────────────┐
│ HW-Puppet (ESP32-S3 Bridge) ──> Target Dev Board (DUT)          │
└─────────────────────────────────────────────────────────────────┘
```

### How It Works
1. **Exclusive Hardware Ownership:** The background daemon opens `/dev/ttyACM0` (CDC0 Control) and `/dev/ttyACM1` (CDC1 UART Console).
2. **Virtual PTY Symlinks:** The daemon creates a virtual pseudoterminal pair (`openpty()`) and creates symlinks:
   - Default: `/tmp/ae-hw-bridge-uart -> /dev/pts/X`
   - Named target: `/tmp/ae-hw-bridge-{target}-uart -> /dev/pts/X`
3. **Full-Duplex Pass-Through:**
   - **Target TX (Logs):** Copied simultaneously to the agent's memory ring buffer and to the PTY master.
   - **Human Input (Keystrokes):** Forwarded from the PTY slave to the physical target UART via the daemon's background serial worker.
4. **Non-Blocking Mirroring:** If no terminal client is connected, writes to the PTY master ignore disconnect errors (`EIO`/`EAGAIN`), so background logging never blocks or stalls the daemon or the AI agent.

---

## 3. Console Usage Patterns

### A. Live Interactive Terminal
Connect to the virtual PTY interactively:
```bash
ae-hw-bridge console
# or for a named target:
ae-hw-bridge console -t jetson
```
- Automatically resolves the running daemon and connects to the virtual PTY.
- If `tio` or `picocom` is installed on your machine, launches it transparently.
- If no external terminal emulator is installed (e.g. inside a minimal container), launches a **built-in interactive raw terminal** with escape sequence `Ctrl-]` or `Ctrl-Q` to disconnect.

### B. Recent Logs (Tail)
Inspect recent lines from the circular memory buffer and exit immediately:
```bash
# Print last 50 lines:
ae-hw-bridge console -n 50

# Print last 100 lines matching a regex pattern:
ae-hw-bridge console -n 100 --grep "kernel panic"
```

### C. Live Log Streaming (Follow)
Stream live UART logs continuously (similar to `tail -f`):
```bash
# Stream all new incoming lines:
ae-hw-bridge console -f

# Print last 20 lines and continue streaming:
ae-hw-bridge console -n 20 -f
```

### D. Unix Pipes and Redirection
When output is redirected or piped (`not sys.stdout.isatty()`), `console` automatically flushes each line immediately:
```bash
# Filter live output using grep:
ae-hw-bridge console | grep --line-buffered "ERROR"

# Save logs to file while watching:
ae-hw-bridge console -f | tee target_boot.log
```

---

## 4. Connecting External Terminals

Because the virtual PTY is exposed as a standard Linux symlink, you can connect your preferred terminal tools directly without learning new commands:

### tio
```bash
tio /tmp/ae-hw-bridge-uart
```

### picocom
```bash
picocom -b 115200 /tmp/ae-hw-bridge-uart
```

### minicom
```bash
minicom -D /tmp/ae-hw-bridge-uart
```

### Distrobox & Containers
If `ae-hw-bridge` runs inside a container or Distrobox, `/dev/pts` and `/tmp` are shared with the host OS. You can run `tio /tmp/ae-hw-bridge-uart` directly on your **host machine** while AI agents run inside the container.

---

## 5. Related Guides

* [CLI Reference Guide](cli.md) — Complete overview of all `ae-hw-bridge` subcommands (`status`, `stop`, `list`, `label`).
* [Target Development Guide](target_development.md) — Creating custom target definitions (`target.py`).
