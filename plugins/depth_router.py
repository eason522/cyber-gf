"""depth 插件：深度路由裁判（原 bot.py 的 judge_depth，逻辑逐字保留）。

明显日常的短消息走关键词快捷路径；拿不准的问硅基流动 Qwen3-8B（关思考，~1s）。
不要用 seed-character 当裁判——角色扮演模型做不了元分类，实测全判 CHAT。
配置（JUDGE_API_KEY / JUDGE_MODEL）从 config 服务读，不再每次调用读 env。
"""

import logging

from openai import AsyncOpenAI

log = logging.getLogger("cyber-gf.depth")

requires: list[str] = []
provides = ["depth"]

# 深度路由：明显日常的短消息走快速通道，拿不准的问裁判模型
DEEP_KEYWORDS = ("爱", "想你", "思念", "难过", "伤心", "哭", "emo", "分手", "纪念日",
                 "永远", "害怕", "孤独", "委屈", "感动", "心跳", "未来", "嫁给", "梦见")
JUDGE_PROMPT = (
    "你是回复规划器。判断这句话该用哪种回复深度："
    "CHAT = 日常闲聊，随性短回复即可；"
    "DEEP = 深情/走心/触景生情的时刻（表白、思念、倾诉心事、深夜emo、纪念日、人生话题），"
    "值得认真写一段较长较深情的回复。只输出 CHAT 或 DEEP 一个词。\n\n他说：%s"
)


class DepthService:
    def __init__(self, ctx):
        cfg = ctx.inject("config")
        self._key = cfg.judge_api_key
        self._model = cfg.judge_model

    async def judge(self, user_text: str) -> str:
        """返回思考档位：minimal（闲聊，关闭思考）或 high（深情长回复）。"""
        t = user_text.strip()
        if len(t) <= 10 and not any(k in t for k in DEEP_KEYWORDS):
            return "minimal"
        if not self._key:
            return "minimal"
        try:
            sf = AsyncOpenAI(base_url="https://api.siliconflow.cn/v1", api_key=self._key)
            r = await sf.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": JUDGE_PROMPT % t}],
                max_tokens=20,
                extra_body={"enable_thinking": False},
            )
            ans = (r.choices[0].message.content or "").upper()
            result = "high" if "DEEP" in ans else "minimal"
            log.info("judge: %s -> %s", t[:20], result)
            return result
        except Exception:
            log.exception("judge failed, default minimal")
            return "minimal"


def apply(ctx) -> None:
    ctx.provide("depth", DepthService(ctx))
