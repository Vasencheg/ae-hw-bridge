"""Unit tests for .ae-hw-bridge/config.yml parsing and target configuration."""

import pytest
from pathlib import Path
from ae_hw_bridge.core.config import load_bridge_config, BridgeConfig, TargetConfig


def test_load_config_empty(tmp_path: Path) -> None:
    cfg = load_bridge_config(base_dir=tmp_path)
    assert isinstance(cfg, BridgeConfig)
    assert cfg.puppet is None
    assert cfg.targets == {}
    assert not cfg.has_targets()
    assert not cfg.is_multi_target()


def test_load_config_single_puppet(tmp_path: Path) -> None:
    dot_dir = tmp_path / ".ae-hw-bridge"
    dot_dir.mkdir()
    config_file = dot_dir / "config.yml"
    config_file.write_text("puppet: jetson-desk\nport: /dev/ttyACM1\n", encoding="utf-8")

    cfg = load_bridge_config(base_dir=tmp_path)
    assert cfg.puppet == "jetson-desk"
    assert cfg.port == "/dev/ttyACM1"
    assert not cfg.has_targets()


def test_load_config_multi_target(tmp_path: Path) -> None:
    dot_dir = tmp_path / ".ae-hw-bridge"
    dot_dir.mkdir()
    config_file = dot_dir / "config.yml"
    content = """
targets:
  jetson:
    puppet: jetson-desk
  stm32:
    puppet: stm32-bench
    port: /dev/ttyACM5
"""
    config_file.write_text(content, encoding="utf-8")

    cfg = load_bridge_config(base_dir=tmp_path)
    assert cfg.has_targets()
    assert cfg.is_multi_target()
    assert len(cfg.targets) == 2

    assert "jetson" in cfg.targets
    assert cfg.targets["jetson"].puppet == "jetson-desk"

    assert "stm32" in cfg.targets
    assert cfg.targets["stm32"].puppet == "stm32-bench"
    assert cfg.targets["stm32"].port == "/dev/ttyACM5"


def test_load_config_shorthand_targets(tmp_path: Path) -> None:
    dot_dir = tmp_path / ".ae-hw-bridge"
    dot_dir.mkdir()
    config_file = dot_dir / "config.yml"
    content = """
targets:
  jetson: jetson-desk
  stm32: stm32-bench
"""
    config_file.write_text(content, encoding="utf-8")

    cfg = load_bridge_config(base_dir=tmp_path)
    assert cfg.targets["jetson"].puppet == "jetson-desk"
    assert cfg.targets["stm32"].puppet == "stm32-bench"
