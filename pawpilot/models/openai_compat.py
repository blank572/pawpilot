"""走 OpenAI 协议的实现。

智谱、阿里通义、阶跃、硅基流动、Moonshot、本地 Ollama 乃至 OpenAI 自己,
现在都宣称兼容 OpenAI 的 chat/completions 协议。所以这里一份代码就能覆盖
绝大多数选择 —— provider 之间只差 base_url、model 名和 key。
真遇到不兼容的厂商,再单独写一个 backend 实现 VisionModel 即可。
"""
from __future__ import annotations

import time
from typing import Any

from openai import OpenAI

from .base import ModelError, Turn, VisionModel, extract_json, split_thought_action

MAX_RETRIES = 3


class OpenAICompatModel(VisionModel):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> None:
        if not base_url:
            raise ModelError("缺少 API 端点地址")
        if not api_key:
            raise ModelError("缺少 API Key")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.client = OpenAI(base_url=base_url.rstrip("/"), api_key=api_key, timeout=timeout)

    def predict(self, messages: list[dict]) -> Turn:
        turn = Turn(request_messages=messages)
        last_err: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            t0 = time.perf_counter()
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
            except Exception as e:  # 网络抖动、限流都值得重试一次
                last_err = e
                wait = min(2**attempt, 8)
                time.sleep(wait)
                continue

            turn.latency_ms = int((time.perf_counter() - t0) * 1000)
            if getattr(resp, "usage", None):
                turn.usage = {
                    "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
                    "completion_tokens": getattr(resp.usage, "completion_tokens", None),
                }

            try:
                content = resp.choices[0].message.content or ""
            except (AttributeError, IndexError) as e:
                raise ModelError(f"响应结构异常: {resp!r}") from e

            turn.raw_response = content
            obj, how = extract_json(content)
            thought, action, note = split_thought_action(obj)
            turn.thought, turn.action = thought, action
            turn.parse_note = "; ".join(x for x in (how, note) if x)
            return turn

        raise ModelError(f"连续 {MAX_RETRIES} 次调用失败:{last_err}")


def build_message(text: str, image_b64: str | None = None) -> dict[str, Any]:
    """组装一条带图(或不带图)的消息。"""
    if not image_b64:
        return {"role": "user", "content": text}
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            },
        ],
    }
