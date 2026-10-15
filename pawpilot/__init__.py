"""让大模型动手操作你的键盘和鼠标。"""

from .config import Config
from .core.actions import Action, parse_action
from .core.agent import ComputerAgent, StopReason
from .core.screen import Screen

__version__ = "0.1.0"
__all__ = [
    "ComputerAgent",
    "StopReason",
    "Action",
    "parse_action",
    "Screen",
    "Config",
    "__version__",
]
