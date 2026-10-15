"""安全熔断。

把鼠标键盘交给别人(哪怕是个模型)都是危险的事。这个模块的立场是宁可拦错,
不可放过。所有拦截都可以通过 --allow-risky 关掉,但会明确警告。
同时提供 secret 占位符机制,让密码这类东西不需要出现在对话里。
"""
from __future__ import annotations

import os
import re

from .actions import Action

# 文本层面的危险模式。命中即拦截,除非 --allow-risky。
DANGEROUS_TEXT_PATTERNS: list[tuple[str, str]] = [
    (r"rm\s+-rf\s+/", "递归删除根目录"),
    (r"rm\s+-rf\s+~", "递归删除用户目录"),
    (r"rm\s+-rf\s+\*", "递归删除当前目录下所有内容"),
    (r"rmdir\s+/s", "递归删除目录树"),
    (r"del\s+/[fs]{1,2}\b", "强制静默删除"),
    (r"format\s+[a-z]:", "格式化磁盘"),
    (r"\bmkfs\b", "格式化文件系统"),
    (r"\bdiskpart\b", "磁盘分区工具"),
    (r"shutdown\s+/s", "关机"),
    (r"shutdown\s+-h", "关机"),
    (r"\bhalt\b|\bpoweroff\b", "关机"),
    (r":\(\)\{", "fork 炸弹"),
    (r"chmod\s+-R\s+777\s+/", "根目录全开权限"),
    (r"DROP\s+DATABASE", "删库"),
    (r"TRUNCATE\s+TABLE", "清表"),
    (r"git\s+push\s+.*--force", "强制推送覆盖远端"),
    (r"git\s+reset\s+--hard", "硬重置丢弃改动"),
    (r"curl\s+[^\s|>]+\s*\|\s*(ba)?sh", "下载即执行"),
    (r"wget\s+[^\s|>]+\s*\|\s*(ba)?sh", "下载即执行"),
    (r"Invoke-Expression", "PowerShell 动态执行"),
    (r"reg\s+delete\b", "删除注册表项"),
    (r"taskkill\s+/f\s+/im\s+(explorer|winlogon|csrss|lsass)", "杀掉系统关键进程"),
]

# 组合键黑名单。单个大多无害,拦的是那些会造成不可逆后果的。
DANGEROUS_HOTKEYS: list[tuple[tuple[str, ...], str]] = [
    (("ctrl", "alt", "delete"), "安全桌面组合键,系统会吃掉这些按键"),
    (("win", "r"), "唤起运行对话框(可接任意命令)"),
    (("win", "x"), "系统级快捷菜单"),
    (("alt", "f4"), "关闭当前窗口,可能丢失未保存内容"),
    (("ctrl", "shift", "esc"), "任务管理器"),
    (("win", "l"), "锁定屏幕"),
]

# 可能被模型当成密码输入的提示词 —— 见到就该停下来问人,而不是自己填。
SECRET_PROMPT_HINT = re.compile(
    r"(密码|口令|passwd|password|credential|secret|验证码|pin\b)", re.IGNORECASE
)

SECRET_PLACEHOLDER = re.compile(r"[<\[]\s*(secret|password|密码)\s*[>\]]", re.IGNORECASE)


class SecurityError(Exception):
    """动作被安全策略拦截。"""


class Guard:
    def __init__(self, allow_risky: bool = False, secrets: dict[str, str] | None = None) -> None:
        self.allow_risky = allow_risky
        self.secrets = secrets or {}

    # --- 秘密值替换 -------------------------------------------------------
    def apply_secrets(self, act: Action) -> Action:
        """把 <secret> 占位符换成真实值,但不让明文进日志。

        AI 只知道有这么个占位符,它想"输密码"就输出占位符,真实值从环境变量来。
        这样密码既不会进 API 请求,也不会进日志文件。
        """
        if act.type != "type" or not act.text:
            return act
        if not SECRET_PLACEHOLDER.search(act.text):
            return act

        if not self.secrets:
            raise SecurityError(
                "模型想输入密码占位符 <secret>,但没有配置真实值。"
                "用环境变量 PAWPILOT_SECRET='你的内容' 传进来(不要写进命令行历史)。"
            )
        real = next(iter(self.secrets.values()))
        act.text = SECRET_PLACEHOLDER.sub(real, act.text)
        act.raw["_secret_redacted"] = True
        return act

    # --- 文本检查 ---------------------------------------------------------
    def check_text(self, text: str) -> None:
        if self.allow_risky:
            return
        for pattern, why in DANGEROUS_TEXT_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                raise SecurityError(
                    f"拦截了一段看起来很危险的输入(疑似{why}):\n"
                    f"    {text[:120]}\n"
                    f"如果是你确实要做的操作,加 --allow-risky 放行。"
                )
        # 模型自己造一个密码去填表单 —— 这几乎一定是错的
        if SECRET_PROMPT_HINT.search(text) and len(text) < 60:
            pass  # 输入框标题本身带这些字样是正常的,不拦;拦的是我们从 DISTINCT

    # --- 组合键检查 -------------------------------------------------------
    def check_hotkey(self, keys: list[str]) -> None:
        if self.allow_risky:
            return
        lowered = [k.lower() for k in keys]
        key_set = {k for k in lowered if k}
        for combo, why in DANGEROUS_HOTKEYS:
            if set(combo).issubset(key_set):
                raise SecurityError(
                    f"拦截组合键 {'+'.join(k.upper() for k in keys)}({why})。"
                    f"确需的话加 --allow-risky。"
                )

    # --- 统一入口 ---------------------------------------------------------
    def check(self, act: Action) -> Action:
        act = self.apply_secrets(act)
        if act.type == "type":
            self.check_text(act.text)
        elif act.type == "hotkey":
            self.check_hotkey(act.keys)
        elif act.type in ("press", "hotkey") and act.keys:
            self.check_hotkey(act.keys)
        return act


def load_secrets() -> dict[str, str]:
    """从环境变量收集允许替换的秘密值。"""
    out: dict[str, str] = {}
    for name in ("PAWPILOT_SECRET", "PAWPILOT_PASSWORD"):
        val = os.getenv(name)
        if val:
            out[name] = val
    return out
