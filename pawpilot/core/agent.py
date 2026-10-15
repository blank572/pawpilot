"""Agent 主循环:观察 -> 思考 -> 行动。

几个不那么显然、但缺了就会明显变差的设计:

* **历史图片裁剪**。每轮都带图,十几轮下来上下文会爆炸。只保留最近几轮的截图,
  更早的轮次退化成纯文字("你点击了 xxx"),可以在几乎不损失效果的前提下省下大量 token。
* **屏幕无变化检测**。AI 点了一下,界面纹丝不动。人会立刻换办法,模型却容易
  在同一个坐标上反复点。我们比较前后帧差异,把"刚才那下没效果"明确告诉它。
* **危险动作先警告再换方式**。一刀切断掉太粗暴 —— 可能是误判。默认允许重试,
  但连续尝试危险动作达到阈值就果断终止。
* **连续失败熔断**。解析不了、接口报错重复发生三次就停,避免烧钱空转。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import pyautogui
from PIL import ImageChops

from ..config import Config
from ..models import Turn, VisionModel, build_model
from ..models.openai_compat import build_message
from .actions import Action, Executor, parse_action
from .prompts import (
    build_system_prompt,
    observation_message,
    retry_message,
    screen_meta,
    task_message,
)
from .safety import Guard, SecurityError, load_secrets
from .screen import Frame, Screen

# 历史里最多保留几张过去的截图
KEEP_RECENT_IMAGES = 3
MAX_CONSECUTIVE_ERRORS = 3
MAX_RISKY_ATTEMPTS = 3


class StopReason(str, Enum):
    DONE = "done"
    FAIL = "fail"
    MAX_STEPS = "max_steps"
    ABORTED = "aborted"
    ERROR = "error"
    TOO_MANY_ERRORS = "too_many_errors"
    TOO_MANY_RISKY = "too_many_risky"


@dataclass
class StepRecord:
    index: int
    action_type: str = ""
    thought: str = ""
    action: dict = field(default_factory=dict)
    describe: str = ""
    error: str = ""
    blocked: str = ""
    screen_changed: bool = True
    latency_ms: int = 0
    usage: dict = field(default_factory=dict)
    screenshot: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "step": self.index,
            "action_type": self.action_type,
            "thought": self.thought,
            "action": self.action,
            "describe": self.describe,
            "error": self.error,
            "blocked": self.blocked,
            "screen_changed": self.screen_changed,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
        }
        if self.screenshot:
            d["screenshot"] = str(self.screenshot)
        return d


@dataclass
class RunResult:
    task: str
    reason: StopReason
    steps: list[StepRecord] = field(default_factory=list)
    message: str = ""
    elapsed_s: float = 0.0
    total_tokens: int = 0
    run_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return self.reason == StopReason.DONE


class ComputerAgent:
    def __init__(
        self,
        cfg: Config,
        model: VisionModel | None = None,
        on_step: Callable[[StepRecord], None] | None = None,
        on_ask: Callable[[str], str] | None = None,
        io=None,
        screen: Screen | None = None,
    ) -> None:
        self.cfg = cfg
        self.model = model or build_model(cfg)
        # screen 允许外部注入,是为了让测试能在没有桌面的环境里跑起来
        self.screen = screen or Screen(cfg.monitor)
        self.guard = Guard(allow_risky=cfg.allow_risky, secrets=load_secrets())
        self.on_step = on_step
        self.on_ask = on_ask
        self.io = io  # 终端输出通道,None 时静默

        self._messages: list[dict] = []
        self._history: list[StepRecord] = []
        self._risky_attempts = 0
        self._consecutive_errors = 0

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.screen.close()

    # --- 运行 -----------------------------------------------------------
    def run(self, task: str) -> RunResult:
        t_start = time.perf_counter()
        result = RunResult(task=task, reason=StopReason.ERROR)
        run_dir = Path(self.cfg.run_dir) if self.cfg.run_dir else None
        if run_dir:
            run_dir.mkdir(parents=True, exist_ok=True)
            result.run_dir = run_dir

        self._messages = [{"role": "system", "content": build_system_prompt()}]

        frame = self.screen.capture()
        self._save_frame_shot(frame, run_dir, 0)
        self._messages.append(
            build_message(f"{task_message(task)}\n\n{screen_meta(frame)}", Screen.to_base64(frame))
        )

        result.reason = StopReason.MAX_STEPS

        try:
            for i in range(1, self.cfg.max_steps + 1):
                if self._history and (r := self._maybe_stop()) is not None:
                    result.reason = r
                    break

                rec = StepRecord(index=i)
                frame = self._step_once(i, frame, run_dir, rec)
                self._history.append(rec)
                if self.on_step:
                    self.on_step(rec)

                if rec.action_type == "done":
                    result.reason = StopReason.DONE
                    result.message = rec.describe
                    break
                if rec.action_type == "fail":
                    result.reason = StopReason.FAIL
                    result.message = rec.describe
                    break
                if frame is None:  # 触发了 ask 且用户要求停止等情况
                    break

                time.sleep(self.cfg.step_delay)
        except pyautogui.FailSafeException:
            result.reason = StopReason.ABORTED
            result.message = "检测到鼠标移动到屏幕角落,已紧急停止"
        except KeyboardInterrupt:
            result.reason = StopReason.ABORTED
            result.message = "用户中断(Ctrl+C)"
        finally:
            result.steps = self._history
            result.elapsed_s = round(time.perf_counter() - t_start, 2)
            result.total_tokens = sum((s.usage or {}).get("prompt_tokens") or 0 for s in self._history)
            if run_dir:
                self._write_manifest(result, run_dir)
        return result

    # --- 单步 -----------------------------------------------------------
    def _step_once(self, i: int, frame: Frame, run_dir: Path | None, rec: StepRecord) -> Frame | None:
        # 1) 问模型
        try:
            turn: Turn = self.model.predict(self._messages)
        except Exception as e:
            rec.error = f"模型调用失败:{e}"
            self._consecutive_errors += 1
            return frame

        rec.thought = turn.thought
        rec.latency_ms = turn.latency_ms
        rec.usage = turn.usage

        # 2) 解析
        try:
            action: Action = parse_action(turn.action)
        except Exception as e:
            rec.error = f"动作解析失败:{e}"
            rec.action = turn.action
            self._consecutive_errors += 1
            self._messages.append(build_message(retry_message(str(e))))
            return frame

        self._consecutive_errors = 0
        rec.action_type = action.type
        rec.action = _redacted(action.raw)

        # 3) 安全把关
        try:
            action = self.guard.check(action)
        except SecurityError as e:
            self._risky_attempts += 1
            rec.blocked = str(e)
            rec.describe = f"被拦截: {action.describe()}"
            self._messages.append(
                build_message(
                    f"刚才的动作被安全策略拦截了:{e}\n请换一种不触及风险的方式达到目的,"
                    "或者用 ask 向用户确认后再做。"
                )
            )
            return frame

        # 4) 人工确认
        if self.cfg.confirm and not action.is_terminal:
            if not self._ask_confirm(action):
                rec.describe = f"用户否决了这一步: {action.describe()}"
                self._messages.append(
                    build_message(f"用户否决了「{action.describe()}」。请换一种做法。")
                )
                return frame

        rec.describe = action.describe()

        # 5) ask 动作:向用户要信息
        if action.type == "ask":
            answer = self.on_ask(action.text) if self.on_ask else ""
            if not answer:
                answer = "用户没有回应。"
            self._messages.append(build_message(f"用户回答:{answer}\n请根据这个回答继续。"))
            return frame

        # 6) 执行
        if not action.is_terminal:
            try:
                Executor(frame, type_interval=0.02).run(action)
            except pyautogui.FailSafeException:
                raise
            except Exception as e:
                rec.error = f"执行失败:{e}"
                self._messages.append(
                    build_message(f"动作执行失败:{e}\n请换一种方式。")
                )
                return frame

        # 7) 观察结果
        new_frame = self.screen.capture()
        rec.screen_changed = _frame_differs(frame, new_frame)
        if run_dir:
            rec.screenshot = self._save_frame_shot(new_frame, run_dir, i)

        # 8) 追加历史
        self._messages.append(_assistant_turn(turn.raw_response or json.dumps(rec.action, ensure_ascii=False)))
        hint = (
            "注意:上一步执行后屏幕几乎没有变化,这个动作可能没有生效。"
            "换一种方式(比如先点击激活、用快捷键、或者换个按钮)。"
            if not rec.screen_changed and not action.is_terminal
            else ""
        )
        self._messages.append(
            build_message(
                f"{observation_message(hint)}\n\n{screen_meta(new_frame)}",
                Screen.to_base64(new_frame),
            )
        )
        _trim_history_images(self._messages, keep=KEEP_RECENT_IMAGES)
        return new_frame

    # --- 辅助 -----------------------------------------------------------
    def _maybe_stop(self) -> StopReason | None:
        if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            return StopReason.TOO_MANY_ERRORS
        if self._risky_attempts >= MAX_RISKY_ATTEMPTS:
            return StopReason.TOO_MANY_RISKY
        return None

    def _ask_confirm(self, action: Action) -> bool:
        if not self.io:
            return True
        return bool(self.io.confirm(f"执行 [bold]{action.describe()}[/bold] ?"))

    def _save_frame_shot(self, frame: Frame, run_dir: Path | None, idx: int) -> Path | None:
        if not run_dir or not self.cfg.record:
            return None
        p = run_dir / f"step_{idx:03d}.png"
        frame.raw_image.save(p)
        return p

    def _write_manifest(self, result: RunResult, run_dir: Path) -> None:
        data = {
            "task": result.task,
            "stop_reason": result.reason.value,
            "message": result.message,
            "elapsed_s": result.elapsed_s,
            "config": {k: v for k, v in vars(self.cfg).items() if k != "api_key"},
            "steps": [s.to_dict() for s in result.steps],
        }
        (run_dir / "run.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# 模块级小工具
# ---------------------------------------------------------------------------
def _redacted(raw: dict) -> dict:
    """日志里把替换过密码的动作打码,避免明文落盘。"""
    if raw.get("_secret_redacted"):
        return {**{k: v for k, v in raw.items() if k != "_secret_redacted"}, "text": "***"}
    return raw


def _frame_differs(a: Frame, b: Frame, threshold: float = 0.35) -> bool:
    """前后两帧是否发生了肉眼可见的变化。

    用灰度图做差再统计直方图,比逐像素比较快得多,足够判断"点下去有没有反应"。
    """
    try:
        ga, gb = a.image.convert("L"), b.image.convert("L")
        if ga.size != gb.size:
            return True
        diff = ImageChops.difference(ga, gb)
        hist = diff.histogram()
        total = sum(hist)
        if total == 0:
            return False
        # 亮度差超过 12 的像素占比
        changed = sum(hist[12:])
        return (changed / total) > threshold * 0.01
    except Exception:
        return True


def _assistant_turn(text: str) -> dict:
    """把模型的原文塞回历史,让它记得自己说过什么。"""
    return {"role": "assistant", "content": text}


def _trim_history_images(messages: list[dict], keep: int) -> None:
    """把较早轮次的图片从内容里剔掉,只留文字,控制上下文体积。

    最近 keep 条用户消息里保留图片(它们是模型判断"刚才发生了什么"的依据),
    更早的只留文字,模型依然能看懂自己点过哪里,但省下了大量 token。
    """
    image_user_msgs = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user" and isinstance(m.get("content"), list)
    ]
    for i in image_user_msgs[:-keep]:
        parts = messages[i]["content"]
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        note = "\n(这张截图已省略以节省上下文)"
        messages[i] = {"role": "user", "content": text + note}
