# AE-HW-BRIDGE Cheat Sheet

A concise reference card for all `ae-hw-bridge` CLI subcommands and hardware operations.

---

## 1. Terminal / Bash Commands

### Target Actions & Execution
```bash
# List all available actions for the connected target:
ae-hw-bridge actions [-t <target>]

# Output actions in JSON format (for CI/scripts):
ae-hw-bridge actions [-t <target>] --json
# Output clearly indicates the origin of each action:
#   [project]  — from script in the current project (targets/ or .ae-hw-bridge/targets/)
#   [builtin]  — system profile bundled with ae-hw-bridge package
#   [core]     — base hardware bridge operation (BaseTarget)
#   [@repl]    — atomic operation in ESP32-S3 RAM via CDC0 MicroPython

# Invoke a target action (clean status line with origin attribution):
ae-hw-bridge call reset [-t <target>]
# -> Target 'jetson' [builtin]: reset -> OK

ae-hw-bridge call reset duration=1.0 [-t <target>]
ae-hw-bridge call power_cycle [-t <target>]
ae-hw-bridge call recovery_mode [-t <target>]

# Output action result in JSON format (for CI/automation):
ae-hw-bridge call reset --json [-t <target>]

# Execute arbitrary MicroPython code on ESP32-S3 (CDC0):
ae-hw-bridge exec "import machine; print(machine.freq())" [-t <target>]

# Send shell command to target UART console and retrieve output:
ae-hw-bridge send "uname -a" [-t <target>]
ae-hw-bridge send "ip addr show" [-t <target>]
```

### Bench Diagnostics & Port Management
```bash
# Check daemon status, connected ports, uptime, and badges:
ae-hw-bridge status
ae-hw-bridge status --json

# Stop background daemon and release USB serial ports:
ae-hw-bridge stop [-t <target>]
ae-hw-bridge stop --all

# Scan USB bus and discover connected HW-Puppet boards:
ae-hw-bridge list

# Write or clear persistent hardware badge name in ESP32-S3 NVS:
ae-hw-bridge label jetson [--port /dev/ttyACM0]
ae-hw-bridge label --clear [--port /dev/ttyACM0]
```

---

## 2. Interactive Console & Logs

```bash
# Enter interactive target serial console (tio / picocom / raw terminal):
ae-hw-bridge console [-t <target>]

# View last N lines from in-memory ring buffer:
ae-hw-bridge console -n 50 [-t <target>]

# View last lines filtered by regular expression:
ae-hw-bridge console -n 100 --grep "kernel panic|error" [-t <target>]

# Continuous live streaming (tail -f equivalent):
ae-hw-bridge console -f [-t <target>]

# Pipe filtering via Unix pipeline:
ae-hw-bridge console | grep --line-buffered "ERROR"
```

---

## 3. Multiple Boards on a Single Test Bench

When multiple `hw-puppet` boards are connected to the host:
1. Assign badges to boards:
   ```bash
   ae-hw-bridge label jetson --port /dev/ttyACM0
   ae-hw-bridge label stm32  --port /dev/ttyACM2
   ```
2. Target any board using `-t` flag:
   ```bash
   ae-hw-bridge console -t jetson    # Jetson console
   ae-hw-bridge console -t stm32     # STM32 console
   ae-hw-bridge call reset -t jetson # Jetson reset
   ae-hw-bridge call reset -t stm32  # STM32 reset
   ```
3. Virtual PTY ports are isolated per target:
   - `/tmp/ae-hw-bridge-jetson-uart`
   - `/tmp/ae-hw-bridge-stm32-uart`
