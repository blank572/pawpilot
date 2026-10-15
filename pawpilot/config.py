"""配置:所有可调项集中在这里,避免魔法数字散落各处。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# 各家模型的接入预设。国内厂商基本都提供 OpenAI 兼容模式,
# 于是我们只需要一套 SDK 就能通吃,provider 只是帮你把 base_url 填好。
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    base_url: str
    default_model: str
    # 需要去哪里申请 key,写进报错提示里比让人自己搜强
    signup_url: str
    # 该服务商分配 key 时常用的环境变量名
    env_var: str


PROVIDERS: dict[str, ProviderPreset] = {
    p.key: p
    for p in [
        ProviderPreset(
            key="zhipu",
            label="智谱 GLM (推荐,有免费视觉模型)",
            base_url="https://open.bigmodel.cn/api/paas/v4/",
            default_model="glm-4v-flash",
            signup_url="https://open.bigmodel.cn/",
            env_var="ZHIPUAI_API_KEY",
        ),
        ProviderPreset(
            key="qwen",
            label="通义千问 Qwen-VL",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            default_model="qwen-vl-max-latest",
            signup_url="https://dashscope.console.aliyun.com/",
            env_var="DASHSCOPE_API_KEY",
        ),
        ProviderPreset(
            key="stepfun",
            label="阶跃星辰 Step-1V",
            base_url="https://api.stepfun.com/v1",
            default_model="step-1v-8k",
            signup_url="https://platform.stepfun.com/",
            env_var="STEPFUN_API_KEY",
        ),
        ProviderPreset(
            key="siliconflow",
            label="硅基流动 SiliconFlow",
            base_url="https://api.siliconflow.cn/v1",
            default_model="Qwen/Qwen2.5-VL-72B-Instruct",
            signup_url="https://cloud.siliconflow.cn/",
            env_var="SILICONFLOW_API_KEY",
        ),
        ProviderPreset(
            key="openai",
            label="OpenAI GPT-4o",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4o",
            signup_url="https://platform.openai.com/api-keys",
            env_var="OPENAI_API_KEY",
        ),
        ProviderPreset(
            key="ollama",
            label="本地 Ollama (零成本,数据不出本机)",
            base_url="http://localhost:11434/v1",
            default_model="qwen2.5vl:7b",
            signup_url="https://ollama.com/",
            env_var="OLLAMA_API_KEY",
        ),
    ]
}


def resolve_provider(name: str | None) -> ProviderPreset:
    """把 provider 名解析成预设。认不出来就当自定义,让使用者自己填 URL。"""
    if not name:
        return PROVIDERS["zhipu"]
    if name in PROVIDERS:
        return PROVIDERS[name]
    # 未知 provider 不报错,退化成 custom:base_url 的形式
    return ProviderPreset(
        key=name, label=name, base_url="", default_model="", signup_url="", env_var="API_KEY"
    )


@dataclass
class Config:
    """运行期的全部配置。"""

    provider: str = "zhipu"
    base_url: str = ""
    model: str = ""
    api_key: str = ""

    # 采集 / 控制
    monitor: int = 1  # 1 = 主屏;见 screen.py 对多显示器的说明
    max_steps: int = 25
    step_delay: float = 1.0  # 每步之间等待界面稳定

    # 安全
    confirm: bool = False  # 每步都要人工点头
    enable_hotkeys: bool = True  # 是否允许 ctrl组合键
    allow_risky: bool = False  # 关闭危险指令拦截

    # 输出
    record: bool = False  # 录制每一步截图
    run_dir: str = ""  # 本次运行的日志/截图落盘位置

    temperature: float = 0.2
    max_tokens: int = 2048

    extra: dict = field(default_factory=dict)

    # ---- 构造 -----------------------------------------------------------
    @classmethod
    def from_env(
        cls,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        **overrides,
    ) -> "Config":
        provider_name = provider or os.getenv("PAWPILOT_PROVIDER") or os.getenv("PROVIDER")
        if base_url:  # 显式给了 base_url 说明是自建/自定义端点
            provider_name = provider_name or "custom"
        preset = resolve_provider(provider_name)

        cfg = cls(
            provider=preset.key,
            base_url=base_url
            or os.getenv("PAWPILOT_BASE_URL")
            or preset.base_url,
            model=model or os.getenv("PAWPILOT_MODEL") or preset.default_model,
            api_key=api_key
            or os.getenv(preset.env_var)
            or os.getenv("PAWPILOT_API_KEY")
            or "",
        )
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def missing_reason(self) -> str | None:
        """缺什么就告诉人去哪儿补,别让人对着 traceback 猜。"""
        if not self.api_key:
            preset = resolve_provider(self.provider)
            if preset.signup_url:
                return (
                    f"没有找到 API Key。请在{preset.signup_url}申请,"
                    f"然后设置环境变量 {preset.env_var}"
                )
            return "没有找到 API Key。请设置环境变量 PAWPILOT_API_KEY"
        if not self.base_url:
            return "没有配置 API 端点。请通过 --base-url 或 PAWPILOT_BASE_URL 设置"
        if not self.model:
            return "没有配置模型名。请通过 --model 或 PAWPILOT_MODEL 设置"
        return None
