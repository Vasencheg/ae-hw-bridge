# Contributing to AE-HW-BRIDGE

Thank you for your interest in contributing to `ae-hw-bridge`!

## Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/Vasencheg/ae-hw-bridge.git
   cd ae-hw-bridge
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .[dev]
   ```

3. Run test suite:
   ```bash
   pytest
   ```

## Adding New Target Modules

Target modules define board-specific hardware behavior (pins, resets, power cycling, telemetry) and are located in `.ae-hw-bridge/targets/<target_name>/target.py` or inside project repositories:

1. Subclass `BaseTarget` from `ae_hw_bridge.targets.base`.
2. Use `@repl` for methods that execute low-level MicroPython in ESP32 RAM (pin toggling, raw REPL).
3. Public methods without `@repl` are automatically registered as high-level FastMCP tools for AI agents.

## Pull Requests

1. Create a feature branch (`git checkout -b feat/my-target`).
2. Ensure all tests pass (`pytest`).
3. Commit using conventional commit format (`feat: ...`, `fix: ...`, `docs: ...`).
4. Submit a Pull Request on GitHub.
