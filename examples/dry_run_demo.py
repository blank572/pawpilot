"""离线演示：不需要 API Key,也能把整个循环跑一遍。

这里用了一个假的模型 —— 它不调任何 API,只是按预设顺序吐出几个动作。
目的是让人先搞明白"截图 → 思考 → 执行"这条链路长什么样,
顺便确认自己的环境(截图、权限、依赖)是通的。

跑起来之后,Agent 会真的移动你的鼠标。别慌,序列里没有破坏性操作。

    python examples/dry_run_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pawpilot.config import Config
from pawpilot.core.agent import ComputerAgent
from pawpilot.core.screen import ScreenCaptureError
from pawpilot.models.base import Turn, VisionModel


class ScriptedModel(VisionModel):
    """一个照本宣科的模型。第 N 次被调用就返回第 N 个预设动作。"""

    def __init__(self, script: list[tuple[str, dict]]) -> None:
        self.script = script
        self.calls = 0

    def predict(self, messages) -> Turn:
        # 顺道打印一下上下文里都有什么,方便理解多轮是怎么串起来的
        self.calls += 1
        n_images = sum(1 for m in messages if isinstance(m.get("content"), list))
        n_text = sum(1 for m in messages if isinstance(m.get("content"), str))
        print(f"       [模型收到 {len(messages)} 条消息,其中带图 {n_images} 条]")

        if self.calls > len(self.script):
            thought, action = "演示脚本放完了。", {"type": "done", "result": "演示结束"}
        else:
            thought, action = self.script[self.calls - 1]

        return Turn(
            request_messages=messages,
            raw_response=str(action),
            thought=thought,
            action=action,
            latency_ms=0,
        )


SCRIPT = [
    ("屏幕上能看到 Windows 任务栏,左下角是开始按钮。先看看当前画面。", {"type": "wait", "seconds": 1}),
    ("把鼠标移到屏幕中间,确认鼠标控制是通的。", {"type": "move", "x": 500, "y": 500}),
    ("再挪到左上角四分之一处。", {"type": "move", "x": 250, "y": 250}),
    ("演示一个单纯的等待。", {"type": "wait", "seconds": 1}),
    ("全部流程走完,可以收工了。", {"type": "done", "result": "离线演示成功"}),
]


def main() -> int:
    print("这是一个不联网的演示。模型是假的,鼠标移动是真的。\n")

    cfg = Config.from_env(max_steps=10, step_delay=0.3, monitor=1)
    cfg.provider, cfg.base_url, cfg.model, cfg.api_key = "mock", "mock", "mock", "mock"

    agent = ComputerAgent(cfg, model=ScriptedModel(SCRIPT))
    steps = []

    def reporter(rec):
        steps.append(rec)
        icon = {"wait": "[等待]", "move": "[移动]", "done": "[完成]"}.get(rec.action_type, "[?]")
        print(f"  第 {rec.index} 步 {icon} {rec.describe}")
        if rec.thought:
            print(f"        想: {rec.thought}")
        if rec.error:
            print(f"        错误: {rec.error}")

    agent.on_step = reporter
    try:
        result = agent.run("离线演示：验证 PawPilot 的循环链路")
    except ScreenCaptureError as e:
        print(f"\n这个环境截不了图:{e}")
        print("换一个能看见桌面的普通终端再试。")
        return 1
    finally:
        agent.close()

    print(f"\n结束状态: {result.reason.value}")
    print(f"共 {len(result.steps)} 步,耗时 {result.elapsed_s}s")

    if result.ok:
        print("\n链路正常。接下来只要配好真实 API Key 就能替换掉这个假模型:")
        print("    $env:ZHIPUAI_API_KEY='你的key'   (Windows PowerShell)")
        print('    pawpilot "打开计算器,算 1234 * 5678"')
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
