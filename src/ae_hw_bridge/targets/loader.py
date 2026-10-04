"""Target loader and dynamic discovery mechanism with guaranteed BaseTarget fallback."""

import os
import sys
import re
import inspect
import logging
import importlib.util
from pathlib import Path
from typing import Optional, Type

from ae_hw_bridge.core.interfaces import (
    IReplClient,
    IConsoleReader,
)
from ae_hw_bridge.targets.base import BaseTarget

_logger = logging.getLogger(__name__)


class TargetLoader:
    """Discovers, loads, and instantiates custom target modules with fallback to BaseTarget."""

    @classmethod
    def load(
        cls,
        repl: IReplClient,
        console: IConsoleReader,
        target_path: Optional[str] = None,
    ) -> BaseTarget:
        """Discover and instantiate a custom target subclass or fall back to BaseTarget.

        Args:
            repl: MicroPython Raw REPL client.
            console: Target UART console reader.
            target_path: Optional path to a target file (e.g. 'targets/jetson.py') or directory.

        Returns:
            An instantiated BaseTarget (or subclass thereof). Guaranteed never to return None or raise.
        """
        resolved_path = cls._resolve_path(target_path)
        if resolved_path:
            target_instance = cls._load_from_path(resolved_path, repl, console)
            if target_instance:
                return target_instance

        _logger.info("No custom target loaded; using BaseTarget core fallback")
        return BaseTarget(repl=repl, console=console)

    @classmethod
    def _resolve_path(cls, target_path: Optional[str] = None) -> Optional[Path]:
        """Resolve candidate target file or directory from argument, env, or working directory."""
        candidates = []
        if target_path:
            candidates.append(Path(target_path))

        env_path = os.getenv("AE_TARGET_DIR") or os.getenv("AE_TARGET_PATH")
        if env_path:
            candidates.append(Path(env_path))

        cwd = Path.cwd()
        candidates.extend([
            cwd / ".ae-hw-bridge" / "targets",
            cwd / ".ae-hw-bridge",
            cwd / "targets",
            cwd / ".targets",
            cwd / ".ae-hw-bridge" / "target.py",
            cwd / "target.py",
        ])

        for p in candidates:
            if p.exists():
                return p.resolve()
        return None

    @classmethod
    def _load_from_path(
        cls,
        path: Path,
        repl: IReplClient,
        console: IConsoleReader,
    ) -> Optional[BaseTarget]:
        """Search path for BaseTarget subclasses and instantiate the first one found."""
        py_files = []
        if path.is_file() and path.suffix == ".py":
            py_files.append(path)
        elif path.is_dir():
            # Check target.py first if it exists
            t_file = path / "target.py"
            if t_file.exists():
                py_files.append(t_file)
            for f in sorted(path.glob("*.py")):
                if f.name.startswith("_") or f == t_file:
                    continue
                py_files.append(f)
            # Also check subdirectories, e.g. .ae-hw-bridge/targets/jetson/target.py or jetson/*.py
            for sub_target in sorted(path.glob("*/*.py")):
                if not sub_target.name.startswith("_") and sub_target not in py_files:
                    py_files.append(sub_target)

        for f in py_files:
            target_cls = cls._import_target_class(f)
            if target_cls:
                try:
                    instance = target_cls(repl=repl, console=console)
                    _logger.info("Loaded custom target %s from %s", target_cls.__name__, f)
                    return instance
                except Exception as e:
                    _logger.error("Failed to instantiate target %s from %s: %s", target_cls.__name__, f, e)

        return None

    @classmethod
    def _import_target_class(cls, file_path: Path) -> Optional[Type[BaseTarget]]:
        """Dynamically load module from file_path and find the first BaseTarget subclass."""
        safe_parent = re.sub(r"[^a-zA-Z0-9_]", "_", file_path.parent.name)
        safe_stem = re.sub(r"[^a-zA-Z0-9_]", "_", file_path.stem)
        module_name = f"ae_target_{safe_parent}_{safe_stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(file_path))
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)

            for _, obj in inspect.getmembers(mod, inspect.isclass):
                if issubclass(obj, BaseTarget) and obj is not BaseTarget:
                    return obj
        except Exception as e:
            _logger.error("Error importing target module from %s: %s", file_path, e)

        return None
