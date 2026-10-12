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
    def discover_target_files(cls, base_dir: Optional[Path] = None) -> dict[str, Path]:
        """Discover target definitions in .ae-hw-bridge/targets/ or targets/ directory.

        Searches current working directory and parent directories up to git root.

        Returns:
            Dict mapping target_name -> file_path
        """
        cwd = (base_dir or Path.cwd()).resolve()
        search_dirs: list[Path] = []

        cur = cwd
        while True:
            search_dirs.extend([
                cur / ".ae-hw-bridge" / "targets",
                cur / "targets",
            ])
            if (cur / ".git").exists() or cur.parent == cur:
                break
            cur = cur.parent

        search_dirs.append(Path.home() / ".config" / "ae-hw-bridge" / "targets")

        results: dict[str, Path] = {}
        for s_dir in search_dirs:
            if s_dir.exists() and s_dir.is_dir():
                for sub in sorted(s_dir.iterdir()):
                    if sub.is_dir() and not sub.name.startswith((".", "_")):
                        t_py = sub / "target.py"
                        if t_py.exists() and sub.name not in results:
                            results[sub.name] = t_py
                        else:
                            py_files = [f for f in sorted(sub.glob("*.py")) if not f.name.startswith("_")]
                            if py_files and sub.name not in results:
                                results[sub.name] = py_files[0]
                    elif sub.is_file() and sub.suffix == ".py" and not sub.name.startswith((".", "_")):
                        name = sub.stem
                        if name not in results and name != "target":
                            results[name] = sub

        if "base" not in results:
            cur = cwd
            while True:
                for s_file in [cur / ".ae-hw-bridge" / "target.py", cur / "target.py"]:
                    if s_file.exists():
                        results["base"] = s_file
                        break
                if "base" in results or (cur / ".git").exists() or cur.parent == cur:
                    break
                cur = cur.parent

        return results

    @classmethod
    def load_named_target(
        cls,
        name: str,
        repl: IReplClient,
        console: IConsoleReader,
        target_path: Optional[str] = None,
    ) -> BaseTarget:
        """Load target class for a named target, falling back to BaseTarget."""
        if target_path:
            p = Path(target_path)
            if p.exists():
                inst = cls._load_from_path(p, repl, console)
                if inst:
                    return inst

        discovered = cls.discover_target_files()
        if name in discovered:
            inst = cls._load_from_path(discovered[name], repl, console)
            if inst:
                return inst

        # Check package built-in targets (e.g. builtin/jetson.py)
        builtin_dir = Path(__file__).resolve().parent / "builtin"
        builtin_candidates = [
            builtin_dir / f"{name}.py",
            builtin_dir / name / "target.py",
        ]
        for b_cand in builtin_candidates:
            if b_cand.exists():
                inst = cls._load_from_path(b_cand, repl, console)
                if inst:
                    return inst

        _logger.debug("No custom class for target '%s', using BaseTarget", name)
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
                    resolved_f = f.resolve()
                    instance.target_file = str(resolved_f)
                    instance.target_mtime = resolved_f.stat().st_mtime
                    builtin_dir = (Path(__file__).resolve().parent / "builtin").resolve()
                    user_cfg_dir = (Path.home() / ".config" / "ae-hw-bridge").resolve()

                    s_f = str(resolved_f)
                    if str(builtin_dir) in s_f:
                        instance.target_source = "builtin"
                    elif str(user_cfg_dir) in s_f:
                        instance.target_source = "user_config"
                    else:
                        instance.target_source = "project"

                    _logger.info("Loaded custom target %s from %s (source: %s)", target_cls.__name__, f, instance.target_source)
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
