"""llm 插件：主模型 AsyncOpenAI 客户端单例（原 bot.py:53-60 的构建方式）。"""

from openai import AsyncOpenAI

requires: list[str] = []
provides = ["llm"]


def apply(ctx) -> None:
    cfg = ctx.inject("config")
    ctx.provide("llm", AsyncOpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key))
