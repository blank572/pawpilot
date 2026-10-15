"""动作:从模型输出的字符串变成真正的一次键鼠操作。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import pyautogui

from ._clipboard import copy_to_clipboard

# pyautogui 自带的失败保护:鼠标甩到屏幕左上角会抛 FailSafeException。
# 这是我们留给用户的物理急停开关,绝对不能关掉。
pyautogui.FAILSAFE = True


# 动作类型的全集。模型只能从这里边挑。
ACTION_TYPES = (
    "click",
    "double_click",
    "right_click",
    "middle_click",
    "move",
    "drag",
    "scroll",
    "type",
    "press",
    "hotkey",
    "wait",
    "ask",
    "done",
    "fail",
)

# 需要坐标的动作
COORD_ACTIONS = {"click", "double_click", "right_click", "middle_click", "move", "scroll"}
DRAG_ACTIONS = {"drag"}
TERMINAL_ACTIONS = {"done", "fail"}


@dataclass
class Action:
    type: str
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    text: str = ""
    keys: list[str] = field(default_factory=list)
    clicks: int = 0
    seconds: float = 0.0
    button: str = "left"
    reason: str = ""
    result: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_ACTIONS

    def describe(self) -> str:
        """给终端和日志看的简短描述。要求:人一眼能看懂 AI 刚才干了什么。"""
        if self.type == "click":
            return f"单击 ({self.x}, {self.y})"
        if self.type == "double_click":
            return f"双击 ({self.x}, {self.y})"
        if self.type == "right_click":
            return f"右键 ({self.x}, {self.y})"
        if self.type == "middle_click":
            return f"中键 ({self.x}, {self.y})"
        if self.type == "move":
            return f"移动鼠标到 ({self.x}, {self.y})"
        if self.type == "drag":
            return f"拖拽 ({self.x},{self.y}) → ({self.x2},{self.y2})"
        if self.type == "scroll":
            direction = "向上" if self.clicks > 0 else "向下"
            return f"滚轮{direction} {abs(self.clicks)} 格"
        if self.type == "type":
            preview = self.text if len(self.text) <= 40 else self.text[:37] + "..."
            return f'输入 "{preview}"'
        if self.type == "press":
            return f"按下 {self.text or self.keys[0] if self.keys else ''}".strip()
        if self.type == "hotkey":
            return " + ".join(k.upper() for k in self.keys)
        if self.type == "wait":
            return f"等待 {self.seconds}s"
        if self.type == "ask":
            return f'向用户提问: {self.text}'
        if self.type == "done":
            return f"任务完成: {self.result or self.reason}"
        if self.type == "fail":
            return f"放弃: {self.reason}"
        return self.type


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
class ActionParseError(ValueError):
    pass


def _as_int(v: Any, lo: int, hi: int, name: str) -> int:
    """把模型可能给出的各种数值形态收敛成整数.

    模型并不总是老老实实给整数:它可能给 512.7,给 "300",给超出边界的值。
    与其报错重问一轮(贵且慢),不如夹到合法区间。
    """
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError) as e:
        raise ActionParseError(f"{name} 不是合法数字: {v!r}") from e
    return max(lo, min(hi, n))


def parse_action(payload: dict) -> Action:
    """把模型吐出的 dict 校验并归一化为 Action。"""
    if not isinstance(payload, dict):
        raise ActionParseError(f"动作必须是对象,收到 {type(payload).__name__}")
    atype = str(payload.get("type", "")).strip().lower()
    if atype not in ACTION_TYPES:
        raise ActionParseError(f"未知动作类型 {atype!r},可选值: {', '.join(ACTION_TYPES)}")

    act = Action(type=atype, raw=payload)

    if atype in COORD_ACTIONS:
        if "x" not in payload or "y" not in payload:
            raise ActionParseError(f"{atype} 需要 x 和 y")
        act.x = _as_int(payload["x"], 0, 1000, "x")
        act.y = _as_int(payload["y"], 0, 1000, "y")

    if atype in DRAG_ACTIONS:
        # 兼容两种写法: {'start': [x,y], 'end': [x,y]} 或 {'x','y','x2','y2'}
        start = payload.get("start") or [payload.get("x"), payload.get("y")]
        end = payload.get("end") or [payload.get("x2"), payload.get("y2")]
        if not start or not end or len(start) != 2 or len(end) != 2:
            raise ActionParseError("drag 需要 start 和 end 两个坐标")
        act.x, act.y = _as_int(start[0], 0, 1000, "start.x"), _as_int(start[1], 0, 1000, "start.y")
        act.x2, act.y2 = _as_int(end[0], 0, 1000, "end.x"), _as_int(end[1], 0, 1000, "end.y")

    if atype == "scroll":
        act.clicks = _as_int(payload.get("clicks", payload.get("dy", 0)), -20, 20, "clicks")

    if atype in ("type", "ask"):
        act.text = str(payload.get("text", payload.get("question", "")))
        if atype == "type" and not act.text:
            raise ActionParseError("type 需要提供 text")

    if atype == "press":
        key = payload.get("key") or payload.get("text") or payload.get("keys")
        if isinstance(key, list):
            act.keys = [str(k) for k in key]
        elif key:
            act.keys = [str(key)]
        if not act.keys:
            raise ActionParseError("press 需要提供 key")

    if atype == "hotkey":
        raw_keys = payload.get("keys") or payload.get("combo") or []
        if isinstance(raw_keys, str):  # 容忍 "ctrl+c" 这种写法
            raw_keys = raw_keys.replace(" ", "").split("+")
        act.keys = [str(k).lower() for k in raw_keys]
        if len(act.keys) < 2:
            raise ActionParseError("hotkey 至少需要两个键,例如 ['ctrl','c']")

    if atype == "wait":
        act.seconds = max(0.0, min(10.0, float(payload.get("seconds", 1))))

    if atype in ("done", "fail"):
        act.result = str(payload.get("result", ""))
        act.reason = str(payload.get("reason", payload.get("result", "")))

    if atype == "click":
        act.button = str(payload.get("button", "left")).lower()
        if act.button not in ("left", "right", "middle"):
            act.button = "left"

    return act


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
class Executor:
    """把 Action 落到真实的鼠标键盘上。"""

    def __init__(self, frame, type_interval: float = 0.02, pause: float = 0.12) -> None:
        self.frame = frame
        self.type_interval = type_interval
        pyautogui.PAUSE = pause
        self.screen_w, self.screen_h = pyautogui.size()

    def _pt(self, act: Action, nx: int, ny: int) -> tuple[int, int]:
        x, y = self.frame.to_screen(nx, ny)
        # 夹到屏幕内,防止模型给出边缘坐标直接把鼠标甩出屏引发 FailSafe 误触发
        return max(0, min(self.screen_w - 1, x)), max(0, min(self.screen_h - 1, y))

    def run(self, act: Action) -> None:
        getattr(self, f"_do_{act.type}", self._unknown)(act)

    def _unknown(self, act: Action) -> None:
        raise ActionParseError(f"没有实现 {act.type} 的执行逻辑")

    # --- 鼠标 -------------------------------------------------------------
    def _do_click(self, act: Action) -> None:
        x, y = self._pt(act, act.x, act.y)
        if act.button == "right":
            pyautogui.rightClick(x, y)
        elif act.button == "middle":
            pyautogui.middleClick(x, y)
        else:
            pyautogui.click(x, y)

    def _do_double_click(self, act: Action) -> None:
        pyautogui.doubleClick(*self._pt(act, act.x, act.y))

    def _do_right_click(self, act: Action) -> None:
        pyautogui.rightClick(*self._pt(act, act.x, act.y))

    def _do_middle_click(self, act: Action) -> None:
        pyautogui.middleClick(*self._pt(act, act.x, act.y))

    def _do_move(self, act: Action) -> None:
        pyautogui.moveTo(*self._pt(act, act.x, act.y))

    def _do_drag(self, act: Action) -> None:
        sx, sy = self._pt(act, act.x, act.y)
        ex, ey = self._pt(act, act.x2, act.y2)
        pyautogui.moveTo(sx, sy)
        pyautogui.mouseDown()
        # 分几步移动,而不是瞬移:很多拖拽菜单位移动画的存在要求路径连续
        steps = 20
        for i in range(1, steps + 1):
            pyautogui.moveTo(
                sx + (ex - sx) * i // steps, sy + (ey - sy) * i // steps, duration=0.01
            )
        pyautogui.mouseUp()

    def _do_scroll(self, act: Action) -> None:
        pyautogui.scroll(int(act.clicks), x=self._pt(act, act.x, act.y)[0])

    # --- 键盘 -------------------------------------------------------------
    def _do_type(self, act: Action) -> None:
        text = act.text
        # 非 ASCII(中文/日文/emoji 等)走剪贴板。typewrite 对它们只能丢字符。
        if any(ord(ch) > 127 for ch in text):
            if copy_to_clipboard(text):
                time.sleep(0.05)
                pyautogui.hotkey("ctrl", "v")
                return
            # 剪贴板不可用时退化用 \uXXXX 转义字符串输入,Windows 支持这个写法
            pyautogui.typewrite(text.encode("unicode_escape").decode("ascii"), interval=0)
            return
        pyautogui.typewrite(text, interval=self.type_interval)

    def _do_press(self, act: Action) -> None:
        pyautogui.press(act.keys[0])

    def _do_hotkey(self, act: Action) -> None:
        pyautogui.hotkey(*act.keys)

    # --- 其他 -------------------------------------------------------------
    def _do_wait(self, act: Action) -> None:
        time.sleep(act.seconds)

    def _do_ask(self, _act: Action) -> None:
        pass  # 由上层 BlockHandler 负责向用户提问

    def _do_done(self, _act: Action) -> None:
        pass

    def _do_fail(self, _act: Action) -> None:
        pass


KEY_ALIASES: dict[str, Sequence[str]] = {}
