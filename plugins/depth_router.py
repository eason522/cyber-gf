"""depth 插件：深度路由裁判 + 他的实时情绪感知（原 bot.py 的 judge_depth 改造）。

明显日常的短消息走关键词快捷路径（情绪用本地关键词轻嗅探）；拿不准的问硅基流动
Qwen3-8B（关思考，~1s），同时判断回复深度和他此刻的情绪（供 chat 注入"她的直觉"）。
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
    "你是回复规划器。判断这句话该用哪种回复深度，以及说话人此刻的情绪。\n"
    "第一行只输出 CHAT 或 DEEP："
    "CHAT = 日常闲聊，随性短回复即可；"
    "DEEP = 深情/走心/触景生情的时刻（表白、思念、倾诉心事、深夜emo、纪念日、人生话题），"
    "值得认真写一段较长较深情的回复。\n"
    "第二行只用一个词输出说话人此刻的情绪"
    "（从：开心/兴奋/平静/疲惫/烦躁/难过/低落/焦虑/思念/甜蜜/无聊 里挑最接近的，都不像就写 平静）。\n\n"
    "他说：%s"
)

# 短消息不走裁判模型，本地关键词轻嗅探情绪（第一个命中生效，顺序即优先级）
MOOD_KEYWORDS = [
    ("笑死", "开心"), ("哈哈", "开心"), ("开心", "开心"), ("太好了", "兴奋"), ("棒", "开心"),
    ("累", "疲惫"), ("疲", "疲惫"), ("困", "疲惫"),
    ("烦", "烦躁"), ("气死", "烦躁"), ("无语", "烦躁"),
    ("难过", "难过"), ("伤心", "难过"), ("emo", "低落"), ("低落", "低落"), ("哭", "难过"),
    ("焦虑", "焦虑"), ("紧张", "焦虑"), ("压力", "焦虑"),
    ("想你", "思念"), ("思念", "思念"), ("爱你", "甜蜜"), ("喜欢", "甜蜜"),
    ("无聊", "无聊"),
]


def _sniff_mood(text: str) -> str | None:
    for kw, mood in MOOD_KEYWORDS:
        if kw in text:
            return mood
    return None


class DepthService:
    def __init__(self, ctx):
        cfg = ctx.inject("config")
        self._key = cfg.judge_api_key
        self._model = cfg.judge_model

    async def judge(self, user_text: str) -> tuple[str, str | None]:
        """返回 (思考档位, 他此刻的情绪或 None)：minimal=闲聊关思考，high=深情长回复。"""
        t = user_text.strip()
        if len(t) <= 10 and not any(k in t for k in DEEP_KEYWORDS):
            return "minimal", _sniff_mood(t)
        if not self._key:
            return "minimal", _sniff_mood(t)
        try:
            sf = AsyncOpenAI(base_url="https://api.siliconflow.cn/v1", api_key=self._key)
            r = await sf.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": JUDGE_PROMPT % t}],
                max_tokens=20,
                extra_body={"enable_thinking": False},
            )
            lines = [l.strip() for l in (r.choices[0].message.content or "").splitlines() if l.strip()]
            head = lines[0].upper() if lines else ""
            result = "high" if "DEEP" in head else "minimal"
            mood = lines[1] if len(lines) > 1 else None
            if mood and (len(mood) > 4 or mood == "平静"):
                mood = None  # 裁判输出异常或无可感知情绪时不注入
            log.info("judge: %s -> %s, mood=%s", t[:20], result, mood)
            return result, mood or _sniff_mood(t)
        except Exception:
            log.exception("judge failed, default minimal")
            return "minimal", _sniff_mood(t)


def apply(ctx) -> None:
    ctx.provide("depth", DepthService(ctx))
