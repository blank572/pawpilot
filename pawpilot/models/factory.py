"""根据配置构造模型实例。"""
from __future__ import annotations

from ..config import Config
from .base import VisionModel
from .openai_compat import OpenAICompatModel


def build_model(cfg: Config) -> VisionModel:
    # 目前所有 provider 都走同一套 OpenAI 兼容协议。
    # 将来某个厂商如果不兼容,在这里补一个分支返回它的 backend 就行。
    return OpenAICompatModel(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model=cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )
