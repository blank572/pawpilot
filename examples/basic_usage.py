"""当作 Python 库来用的几个例子。

把 pawpilot 装好之后(pip install -e .),这些片段可以直接粘走改改就用。
运行前记得设置 PAWPILOT_API_KEY 之类的环境变量。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. 最简形式
# ---------------------------------------------------------------------------


def simple():
    from pawpilot import ComputerAgent, Config

    cfg = Config.from_env(provider="zhipu")
    agent = ComputerAgent(cfg)
    result = agent.run("打开资源管理器,看看 D 盘根目录有什么")
    print(result.reason, result.message)


# ---------------------------------------------------------------------------
# 2. 自己处理每一步：把过程接进你的界面 / 日志系统
# ---------------------------------------------------------------------------
def with_callbacks():
    from pawpilot import ComputerAgent, Config

    def on_step(rec):
        # rec.describe 是人类可读的动作描述,rec.thought 是模型当时的想法
        print(f"[{rec.index}] {rec.describe:<40} | {rec.thought[:60]}")

    def on_ask(question: str) -> str:
        # AI 遇到需要人提供信息的地方会来问你
        return input(f"AI 问:{question}\n> ")

    cfg = Config.from_env(max_steps=15, record=True)
    agent = ComputerAgent(cfg, on_step=on_step, on_ask=on_ask)
    result = agent.run("把桌面上的 .png 图片都复制到 文档\\图片备份")

    if not result.ok:
        print("没做完:", result.reason.value, result.message)
        if result.run_dir:
            print("完整过程记录在", result.run_dir)


# ---------------------------------------------------------------------------
# 3. 换模型 / 换服务商
# ---------------------------------------------------------------------------
def switch_provider():
    from pawpilot import ComputerAgent, Config

    # 方式一:用预设的服务商
    cfg = Config.from_env(provider="qwen", model="qwen-vl-max-latest")
    ComputerAgent(cfg).run("打开记事本")

    # 方式二:完全自定义端点(兼容 OpenAI 协议的都行)
    cfg = Config.from_env(
        provider="custom",
        base_url="https://my-gateway.example.com/v1",
        model="my-vision-model",
        api_key="sk-xxx",
    )
    ComputerAgent(cfg).run("打开记事本")


# ---------------------------------------------------------------------------
# 4. 需要 AI 代填密码:用占位符,别把密码写进提示词
# ---------------------------------------------------------------------------
def with_secret():
    """先在环境里准备好:

        export PAWPILOT_SECRET='登录用的口令'

    然后让 AI 在需要填密码的地方输出 <secret> 占位符,系统会自动替换成真实值。
    真实值既不进 API 请求,也不进运行日志。
    """
    from pawpilot import ComputerAgent, Config

    cfg = Config.from_env(provider="zhipu")
    agent = ComputerAgent(cfg)
    agent.run("登录这台机器的远程桌面客户端,密码输 <secret>")


# ---------------------------------------------------------------------------
# 5. 接自己的模型:继承 VisionModel,实现一个方法就够了
# ---------------------------------------------------------------------------
def custom_model():
    from pawpilot import ComputerAgent, Config
    from pawpilot.models.base import Turn, VisionModel

    class MyModel(VisionModel):
        def predict(self, messages) -> Turn:
            # messages 是 OpenAI 格式的对话列表,最后一轮的最后一个 part 是截图
            raw = '{"thought": "我来调用自己的模型", "action": {"type": "done", "result": "ok"}}'
            return Turn(request_messages=messages, raw_response=raw, thought="", action={
                "type": "done", "result": "ok",
            })

    cfg = Config.from_env()
    ComputerAgent(cfg, model=MyModel()).run("随便做点什么")


if __name__ == "__main__":
    print(__doc__)
    print("这些函数都需要真实 API Key,此处只是示范。")
