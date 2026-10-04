"""Target abstraction subsystem for AE-HW-BRIDGE.

Provides BaseTarget, @repl decorator, and TargetLoader for building and loading
hardware target modules.
"""

from ae_hw_bridge.targets.base import BaseTarget, repl
from ae_hw_bridge.targets.loader import TargetLoader

__all__ = ["BaseTarget", "repl", "TargetLoader"]
