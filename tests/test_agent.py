"""主循环测试。

用假的屏幕和假的模型,并且把 pyautogui 替换掉 —— 测试不能真的去动用户的鼠标。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from pawpilot.config import Config
from pawpilot.core.agent import ComputerAgent, StopReason, _frame_differs, _trim_history_images
from pawpilot.core.screen import Frame
from pawpilot.models.base import Turn


def make_frame(color=(10, 10, 10), raw_w=1920, raw_h=1080) -> Frame:
    return Frame(
        image=Image.new("RGB", (200, 100), color),
        raw_image=Image.new("RGB", (raw_w, raw_h), color),
        view_width=200,
        view_height=100,
    )


class FakeScreen:
    """总是返回同一帧(或给定序列)的屏幕替身。"""

    def __init__(self, frames=None):
        self.frames = frames or [make_frame()]
        self.i = 0

    def capture(self) -> Frame:
        f = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return f

    def close(self):
        pass

    @staticmethod
    def to_base64(_frame) -> str:
        return "ZmFrZQ=="


class ScriptedModel:
    """按脚本吐动作的模型替身;脚本用完就返回 done。"""

    def __init__(self, script):
        self.script = script
        self.calls = 0

    def predict(self, messages) -> Turn:
        self.calls += 1
        if self.calls > len(self.script):
            thought, action = "没了", {"type": "done", "result": "脚本结束"}
        else:
            thought, action = self.script[self.calls - 1]
        return Turn(request_messages=messages, thought=thought, action=action, latency_ms=0)


@pytest.fixture
def fake_pyautogui():
    """把真正的键鼠操作替换成空操作。"""
    fake = MagicMock()
    fake.FailSafeException = RuntimeError  # except 语句要求它必须是异常类
    fake.size.return_value = (1920, 1080)
    with patch("pawpilot.core.actions.pyautogui", fake):
        yield fake


def make_agent(model, **cfgkw) -> ComputerAgent:
    opts = {"max_steps": 5, "step_delay": 0, "record": False}
    opts.update(cfgkw)  # 调用方的覆盖要能盖住默认值,而不是重复传参报错
    cfg = Config.from_env(**opts)
    cfg.provider, cfg.base_url, cfg.model, cfg.api_key = "t", "t", "t", "t"
    return ComputerAgent(cfg, model=model, screen=FakeScreen())


# ---------------------------------------------------------------------------
class TestAgentLoop:
    def test_runs_until_done(self, fake_pyautogui):
        model = ScriptedModel(
            [
                ("点一下", {"type": "click", "x": 100, "y": 100}),
                ("做好了", {"type": "done", "result": "完成"}),
            ]
        )
        result = make_agent(model).run("任务")
        assert result.reason == StopReason.DONE
        assert len(result.steps) == 2
        assert fake_pyautogui.click.called  # 真的试图去点了

    def test_stops_on_fail(self, fake_pyautogui):
        model = ScriptedModel([("找不到", {"type": "fail", "reason": "没看到按钮"})])
        result = make_agent(model).run("任务")
        assert result.reason == StopReason.FAIL

    def test_stops_at_max_steps(self, fake_pyautogui):
        # 脚本里没有终止动作,应当被步数上限挡住
        model = ScriptedModel([("继续", {"type": "wait", "seconds": 0})] * 50)
        result = make_agent(model, max_steps=4).run("任务")
        assert result.reason == StopReason.MAX_STEPS
        assert len(result.steps) == 4

    def test_terminal_actions_do_not_touch_mouse(self, fake_pyautogui):
        model = ScriptedModel([("完事了", {"type": "done", "result": "x"})])
        make_agent(model).run("任务")
        fake_pyautogui.click.assert_not_called()
        fake_pyautogui.typewrite.assert_not_called()

    def test_repeated_risky_attempts_abort(self, fake_pyautogui):
        """模型反复横跳要删库 -> 停止执行。"""
        model = ScriptedModel([("删掉它", {"type": "type", "text": "rm -rf /"})] * 10)
        result = make_agent(model, max_steps=8).run("任务")
        assert result.reason == StopReason.TOO_MANY_RISKY
        fake_pyautogui.typewrite.assert_not_called()  # 一次都没真输进去

    def test_ask_invokes_callback(self, fake_pyautogui):
        answers = []

        def ask(question):
            answers.append(question)
            return "答案是 42"

        model = ScriptedModel(
            [
                ("需要确认", {"type": "ask", "question": "你要几份?"}),
                ("知道了", {"type": "done", "result": "ok"}),
            ]
        )
        agent = make_agent(model)
        agent.on_ask = ask
        agent.run("任务")
        assert answers == ["你要几份?"]

    def test_missing_secret_is_blocked_not_typed(self, fake_pyautogui):
        """模型想输密码但没配置真实值 —— 应当被拦住,而不是把 "<secret>" 原样敲出去。"""
        model = ScriptedModel([("填密码", {"type": "type", "text": "<secret>"})])
        result = make_agent(model, max_steps=3).run("任务")
        fake_pyautogui.typewrite.assert_not_called()
        fake_pyautogui.hotkey.assert_not_called()
        assert result.steps[0].blocked  # 第一步就被拦下了


class TestHistory:
    def test_trim_keeps_recent_images(self):
        from pawpilot.models.openai_compat import build_message

        msgs = [{"role": "system", "content": "sys"}]
        for i in range(6):
            msgs.append(build_message(f"step{i}", "B64"))
            msgs.append({"role": "assistant", "content": f"act{i}"})

        _trim_history_images(msgs, keep=3)
        n_img = sum(1 for m in msgs if isinstance(m.get("content"), list))
        assert n_img == 3
        # 早先轮次的文字要保留下来
        assert any("step0" in m.get("content", "") for m in msgs if isinstance(m.get("content"), str))


class TestFrameDiff:
    def test_identical_frames_not_changed(self):
        a, b = make_frame(), make_frame()
        assert _frame_differs(a, b) is False

    def test_different_frames_changed(self):
        a, b = make_frame((0, 0, 0)), make_frame((255, 255, 255))
        assert _frame_differs(a, b) is True
