"""命令行入口。

用法:
    pawpilot "打开注册表编辑器,找到 Run 项"
    pawpilot --confirm "..."           # 每步都问过我再说
    pawpilot --record "..."            # 保存每一步截图
    pawpilot --list-monitors           # 看看有几个显示器
    pawpilot --check                   # 只检查配置是否就绪,不跑任务
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pyautogui
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from .config import PROVIDERS, Config
from .core.agent import ComputerAgent, StopReason
from .core.screen import ScreenCaptureError

BANNER = r"""
   ____                ____  _ _       _
  |  _ \ __ ___      _|  _ \| (_)_ __ | |_
  | |_) / _` \ \ /\ / / |_) | | | '_ \| __|
  |  __/ (_| |\ V  V /|  __/| | | |_) | |_
  |_|   \__,_| \_/\_/ |_|   |_|_| .__/ \__|
                                |_|
"""

STOP_TEXT = {
    StopReason.DONE: ("任务完成", "green"),
    StopReason.FAIL: ("主动放弃", "yellow"),
    StopReason.MAX_STEPS: ("达到步数上限", "yellow"),
    StopReason.ABORTED: ("已中断", "red"),
    StopReason.ERROR: ("出错", "red"),
    StopReason.TOO_MANY_ERRORS: ("连续出错过多,已停止", "red"),
    StopReason.TOO_MANY_RISKY: ("反复尝试危险操作,已停止", "red"),
}

# 用一个大写 EMBLEM 表示动作类型,终端里扫一眼就能分辨这一步在干嘛
ACTION_ICON = {
    "click": "[点]",
    "double_click": "[双击]",
    "right_click": "[右键]",
    "middle_click": "[中键]",
    "move": "[移动]",
    "drag": "[拖拽]",
    "scroll": "[滚动]",
    "type": "[输入]",
    "press": "[按键]",
    "hotkey": "[组合键]",
    "wait": "[等待]",
    "ask": "[提问]",
    "done": "[完成]",
    "fail": "[放弃]",
}


class TerminalIO:
    """负责把每一步讲清楚。让 AI 在你眼皮底下操作,是信任的前提。"""

    def __init__(self, console: Console, verbose: bool = True) -> None:
        self.console = console
        self.verbose = verbose

    def confirm(self, text: str) -> bool:
        ans = Prompt.ask(text, choices=["y", "n", "q"], default="y", console=self.console)
        if ans == "q":
            raise KeyboardInterrupt
        return ans == "y"

    def ask_user(self, question: str) -> str:
        self.console.print(Panel(escape(question), title="AI 向你提问", border_style="cyan"))
        return Prompt.ask("你的回答", console=self.console)

    def on_step(self, rec) -> None:
        if not self.verbose:
            return
        icon = ACTION_ICON.get(rec.action_type, "[?]")
        head = Text()
        head.append(f"第 {rec.index} 步 ", style="dim")
        head.append(icon, style="bold cyan")
        head.append(f"  {escape(rec.describe)}", style="bold")

        if rec.thought:
            head.append("\n")
            head.append("   想: ", style="dim cyan")
            head.append(escape(_shorten(rec.thought, 200)), style="cyan")

        extras = []
        if rec.latency_ms:
            extras.append(f"{rec.latency_ms}ms")
        if rec.usage and rec.usage.get("prompt_tokens"):
            extras.append(f"{rec.usage['prompt_tokens']} tok")
        if rec.error:
            extras.append(f"[red]{escape(_shorten(rec.error, 160))}[/red]")
        if rec.blocked:
            extras.append(f"[yellow]被拦截: {escape(_shorten(rec.blocked, 160))}[/yellow]")
        if not rec.screen_changed and rec.action_type not in ("done", "fail", "wait"):
            extras.append("[dim]屏幕无变化[/dim]")
        if extras:
            head.append("\n   " + "  ".join(f"({e})" if not e.startswith("[") else e for e in extras))

        self.console.print(Panel(head, border_style="bright_black", padding=(0, 1)))


def _shorten(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pawpilot",
        description="让大模型真正动手:用自然语言操控你的键盘和鼠标",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "环境变量:\n"
            "  PAWPILOT_API_KEY   各家 API Key(也可写 .env)\n"
            "  PAWPILOT_PROVIDER  服务商,见 --list-providers\n"
            "  PAWPILOT_MODEL     模型名\n"
            "  PAWPILOT_SECRET    需要输入的密码,模型输出 <secret> 时自动替换\n"
            "\n"
            "示例:\n"
            '  pawpilot "打开计算器,算 1234 * 5678"\n'
            '  pawpilot --confirm --record "登录后台导出昨天的报表"\n'
        ),
    )
    p.add_argument("task", nargs="?", help="用自然语言描述要机器替你做的事")
    p.add_argument("--provider", "-p", default=None, help="服务商:zhipu/qwen/stepfun/siliconflow/openai/ollama")
    p.add_argument("--model", "-m", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument("--monitor", type=int, default=1, help="操作第几个显示器,默认 1(主屏)")
    p.add_argument("--max-steps", type=int, default=25)
    p.add_argument("--step-delay", type=float, default=1.0, help="每步之后等待界面稳定的秒数")
    p.add_argument("--confirm", "-c", action="store_true", help="每一步都等你确认再执行")
    p.add_argument("--allow-risky", action="store_true", help="关闭危险指令拦截(后果自负)")
    p.add_argument("--record", "-r", action="store_true", help="保存每一步的截图到 runs/ 目录")
    p.add_argument("--quiet", "-q", action="store_true", help="只显示结果")
    p.add_argument("--list-providers", action="store_true")
    p.add_argument("--list-monitors", action="store_true")
    p.add_argument("--check", action="store_true", help="检查配置是否就绪")
    p.add_argument("--version", action="version", version="pawpilot 0.1.0")
    return p


def _print_providers(console: Console) -> None:
    t = Table(title="支持的模型服务商", header_style="bold cyan")
    t.add_column("provider", style="bold")
    t.add_column("默认模型")
    t.add_column("key 环境变量")
    t.add_column("申请地址", overflow="fold")
    for key, p in PROVIDERS.items():
        t.add_row(key, p.default_model, p.env_var, p.signup_url)
    console.print(t)


def _check(cfg: Config, console: Console) -> int:
    from .models import build_model

    console.print(f"provider : {cfg.provider}")
    console.print(f"endpoint : {cfg.base_url}")
    console.print(f"model    : {cfg.model}")
    miss = cfg.missing_reason()
    if miss:
        console.print(f"[red]{miss}[/red]")
        return 1
    console.print("api key  : 已配置")
    stub = None
    try:
        stub = build_model(cfg)
    except Exception as e:
        console.print(f"[red]初始化失败: {e}[/red]")
        return 1
    finally:
        del stub
    console.print("[green]配置看起来没问题。真实调用需要联网,这里不做试探请求。[/green]")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()

    if args.list_providers:
        _print_providers(console)
        return 0

    if args.list_monitors:
        from .core.screen import Screen

        s = Screen(1)
        for i, m in enumerate(s.list_monitors(), start=1):
            console.print(f"显示器 {i}: {m['width']}x{m['height']} @ ({m['left']}, {m['top']})")
        console.print(f"[dim]{s.dpi_note}[/dim]")
        s.close()
        return 0

    cfg = Config.from_env(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        monitor=args.monitor,
        max_steps=args.max_steps,
        step_delay=args.step_delay,
        confirm=args.confirm,
        allow_risky=args.allow_risky,
        record=args.record,
        run_dir=str(Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S")) if args.record else "",
    )

    if args.check:
        return _check(cfg, console)

    if not args.task:
        build_parser().print_help()
        return 1

    miss = cfg.missing_reason()
    if miss:
        console.print(f"[red]{miss}[/red]")
        console.print("[dim]跑 --list-providers 看有哪些选项,--check 检查当前配置。[/dim]")
        return 1

    console.print(Text(BANNER.strip(), style="bold cyan"))
    if cfg.allow_risky:
        console.print("[bold red]警告: 危险指令拦截已关闭,AI 不会被任何东西阻止。[/bold red]")
    console.print(
        Panel(
            escape(args.task),
            title="任务",
            subtitle=f"{cfg.provider} / {cfg.model}  |  最多 {cfg.max_steps} 步"
            f"{'  |  逐步确认' if cfg.confirm else ''}",
            border_style="cyan",
        )
    )
    console.print("[dim]把鼠标甩到屏幕左上角可随时紧急停止。[/dim]\n")

    io = TerminalIO(console, verbose=not args.quiet)
    agent = ComputerAgent(cfg, on_step=io.on_step, on_ask=io.ask_user, io=io)
    try:
        result = agent.run(args.task)
    except ScreenCaptureError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        return 3
    except pyautogui.FailSafeException:
        console.print("\n[yellow]鼠标移到屏幕角落,已紧急停止。[/yellow]")
        return 130
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。[/yellow]")
        return 130
    finally:
        try:
            agent.close()
        except Exception:
            pass

    label, color = STOP_TEXT.get(result.reason, (result.reason.value, "white"))
    summary = Text()
    summary.append(f"{label}", style=f"bold {color}")
    if result.message:
        summary.append(f"  {escape(result.message)}")
    summary.append(
        f"\n[dim]共 {len(result.steps)} 步  |  耗时 {result.elapsed_s}s"
        f"  |  prompt token 累计约 {result.total_tokens}[/dim]"
    )
    if result.run_dir:
        summary.append(f"\n[dim]记录已保存到 {result.run_dir}[/dim]")
    console.print(Panel(summary, border_style=color))
    return 0 if result.ok else 2


if __name__ == "__main__":
    sys.exit(main())
