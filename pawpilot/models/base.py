"""模型层的抽象。

刻意做得很薄:只有一个 `predict` 方法。因为我们只想干一件事 ——
喂一张图和一段话进去,拿回一个动作 JSON。
加这么一层是为了将来接某个不支持 OpenAI 协议的厂商时,不用动上层代码。
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Turn:
    """一次完整的预测请求与结果,用于日志记录和 debug。"""

    request_messages: list[dict] = field(default_factory=list)
    raw_response: str = ""
    thought: str = ""
    action: dict = field(default_factory=dict)
    parse_note: str = ""
    latency_ms: int = 0
    usage: dict = field(default_factory=dict)


class ModelError(Exception):
    pass


class VisionModel(ABC):
    @abstractmethod
    def predict(self, messages: list[dict]) -> Turn:
        """messages 是 OpenAI 格式的对话列表,返回解析好的 Turn。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# JSON 提取。模型的输出很难看:可能裹 markdown 代码块,可能前后各加一句废话。
# 与其要求模型守规矩,不如我们宽容一点。
# ---------------------------------------------------------------------------
_CODE_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> tuple[dict, str]:
    """从模型输出里捞出 JSON。返回 (对象, 说明)。捞不出来抛 ModelError。"""
    if not text or not text.strip():
        raise ModelError("模型返回了空内容")

    candidates = [text.strip()]
    for m in _CODE_BLOCK.finditer(text):
        candidates.insert(0, m.group(1).strip())

    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj, "直接解析成功" if cand == text.strip() else "从代码块中提取"
        except json.JSONDecodeError:
            pass

    # 退化办法:截取最外层花括号
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        frag = text[start : end + 1]
        try:
            obj = json.loads(frag)
            if isinstance(obj, dict):
                return obj, "从花括号片段中抢救出来"
        except json.JSONDecodeError:
            pass

    raise ModelError(f"输出里找不到合法 JSON:\n{text[:400]}")


def split_thought_action(obj: dict) -> tuple[str, dict, str]:
    """把 {'thought':..., 'action':{...}} 拆开。容忍各种懒惰写法。"""
    note = ""
    thought = str(obj.get("thought", obj.get("reasoning", obj.get("observe", "")))).strip()

    action = obj.get("action")
    if isinstance(action, dict):
        return thought, action, note

    # 有些模型会把 type/x/y 平铺在顶层,省掉 action 这一层
    if "type" in obj:
        note = "模型省略了 action 层(已自动补全)"
        skip = {"thought", "reasoning", "observe", "explanation", "reason", "result"}
        action = {k: v for k, v in obj.items() if k not in skip}
        return thought, action, note

    raise ModelError("输出里没有 action 字段,也没有平铺的动作类型")
