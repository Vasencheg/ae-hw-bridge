"""Configuration parser for .ae-hw-bridge/config.yml."""

import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


class TargetConfig(BaseModel):
    """Configuration for an individual target device under test."""

    name: str = ""
    puppet: Optional[str] = None          # Badge name or hardware serial
    port: Optional[str] = None            # Direct CDC 0 control port (e.g. /dev/ttyACM1)
    uart_port: Optional[str] = None       # Direct CDC 1 target UART port (e.g. /dev/ttyACM3)
    path: Optional[str] = None            # Path to target Python module or directory


class BridgeConfig(BaseModel):
    """Global configuration for ae-hw-bridge session."""

    puppet: Optional[str] = None          # Default puppet badge
    port: Optional[str] = None            # Default control port
    uart_port: Optional[str] = None       # Default UART port
    targets: Dict[str, TargetConfig] = Field(default_factory=dict)

    def is_multi_target(self) -> bool:
        """Return True if multiple distinct targets are configured."""
        return len(self.targets) > 1

    def has_targets(self) -> bool:
        """Return True if at least one target is explicitly defined."""
        return len(self.targets) > 0


def find_config_file(base_dir: Optional[Path] = None) -> Optional[Path]:
    """Search working directory hierarchy for .ae-hw-bridge configuration file."""
    cwd = (base_dir or Path.cwd()).resolve()

    candidates = [
        cwd / ".ae-hw-bridge" / "config.yml",
        cwd / ".ae-hw-bridge" / "config.yaml",
        cwd / ".ae-hw-bridge" / "config.json",
        cwd / "config.yml",
        cwd / "config.yaml",
    ]

    for c in candidates:
        if c.exists() and c.is_file():
            return c
    return None


def load_bridge_config(
    config_path: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> BridgeConfig:
    """Load and validate BridgeConfig from YAML/JSON file.

    Args:
        config_path: Explicit path to config file.
        base_dir: Working directory root to search for default config locations.

    Returns:
        BridgeConfig instance (defaults to empty config if no file found).
    """
    file_to_load: Optional[Path] = None
    if config_path:
        p = Path(config_path)
        if p.exists() and p.is_file():
            file_to_load = p
        else:
            raise FileNotFoundError(f"Specified configuration file not found: {config_path}")
    else:
        file_to_load = find_config_file(base_dir=base_dir)

    if not file_to_load:
        _logger.debug("No configuration file found; using default empty BridgeConfig")
        return BridgeConfig()

    _logger.info("Loading configuration from %s", file_to_load)
    raw_text = file_to_load.read_text(encoding="utf-8")
    data: Dict[str, Any] = {}

    if file_to_load.suffix in (".yml", ".yaml"):
        if yaml is None:
            raise RuntimeError("PyYAML is required to parse YAML config files. Install with: pip install pyyaml")
        data = yaml.safe_load(raw_text) or {}
    else:
        data = json.loads(raw_text) if raw_text.strip() else {}

    # Normalize targets mapping
    raw_targets = data.get("targets", {})
    parsed_targets: Dict[str, TargetConfig] = {}

    if isinstance(raw_targets, dict):
        for target_name, target_info in raw_targets.items():
            if target_info is None:
                target_info = {}
            if isinstance(target_info, str):
                # e.g. targets: jetson: "jetson-bench" (shorthand for puppet badge)
                target_info = {"puppet": target_info}
            cfg = TargetConfig(name=target_name, **target_info)
            parsed_targets[target_name] = cfg

    return BridgeConfig(
        puppet=data.get("puppet"),
        port=data.get("port") or data.get("control_port"),
        uart_port=data.get("uart_port"),
        targets=parsed_targets,
    )
