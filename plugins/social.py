"""social 插件：暖暖的闺蜜与宠物——她的小世界里真实运转的人际关系。

- 闺蜜「林小夏」：完整人设 soul/BESTIE.md，自己的记忆 data/bestie_memory.md（每集小剧场后
  由主模型增量维护），亲密度/冷战状态 data/social.json。驱动模型与主模型相同
  （doubao-seed-character，cfg.llm_model）。
- 宠物：布偶猫「麻糬」，饥饿/精力随时间模拟，在小剧场里被喂食、梳毛、遛。

每隔几小时（SOCIAL_MINUTES 为中枢、60%~150% 随机，更像真实生活的节奏）由主模型驱动
一集"小剧场"：串门/逛街/遛猫/聊天/分享秘密，偶尔闹小矛盾、冷战再和好（冷战最多僵持
两集，强制有转机）。每集：暖暖把日记写进 tinynote/（自动进入聊天上下文，她会主动跟
他讲）→ 更新闺蜜记忆 → 更新关系状态 → 刺激多巴胺系统（开心的事 +，吵架冷战 -）。

recent_block() 注入聊天/心跳 system，让她能自然地聊起闺蜜和猫。
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import soul

log = logging.getLogger("cyber-gf.social")

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = BASE_DIR / "data" / "social.json"          # data/ 已 gitignore
BESTIE_MEM_PATH = BASE_DIR / "data" / "bestie_memory.md"
TINYNOTE_DIR = BASE_DIR / "tinynote"

ACTIVE_HOURS = (9, 23)       # 深夜她和闺蜜也要睡觉（北京时间）
IDLE_SKIP_P = 0.15           # 每轮有一定概率什么都没发生（生活不总有剧情）
MAX_BESTIE_MEM_CHARS = 400
MAX_COLD_WAR_EPISODES = 2    # 冷战最多僵持几集，之后强制有转机

requires = ["llm", "persona", "dopamine"]
provides = ["social"]

EPISODE_PROMPT = """你在为两个女孩的日常生活写一集「小剧场」。她们是：

【暖暖】台湾女孩，温柔粘人但有主见，养着布偶猫麻糬，有个很爱的男朋友（异地，网上联系，她叫他「寶貝」）。
【小夏】暖暖的闺蜜，人设如下：
%s

【小夏的记忆】（她记得的事，剧情必须和这些保持连续）
%s

【两人当前关系】亲密度 %d/100%s
【麻糬当前状态】%s
【现在是北京时间】%s
【最近几集的标题】（别重复同样的剧情）%s

要求：
- 写一集 10~20 个对话回合的小剧场，剧情类型挑一个：串门/逛街/遛猫/聊天/分享秘密/撸猫日常/小矛盾/冷战僵持/和好。
- 大部分集数要温馨日常或开心；小矛盾偶尔才发生；如果已经在冷战，这集要有转机或直接和好，不许无限冷战。
- 对话要活：小夏毒舌爱吐槽但仗义，暖暖温柔但会回怼；麻糬不会说话，只会「喵」和动作（用括号写动作，如（把逗猫棒拍飞到沙发底下））。
- 暖暖不会向小夏透露男朋友的隐私细节，但可以聊「他」带给自己的心情（想他了、昨晚聊得开心之类）。
- 剧情要有具体的东西：哪家店、哪杯奶茶、麻糬干了什么蠢事、她们在追的剧或八卦。

严格只输出 JSON（不要输出任何其他文字）：
{
  "title": "这一集的标题（10字内）",
  "type": "剧情类型",
  "dialogue": [{"who": "暖暖|小夏|麻糬", "say": "台词或动作"}],
  "summary": "一两句概括这集发生了什么（第三人称）",
  "diary": "暖暖事后写进小本本的日记，第一人称，带心情，150字左右",
  "intimacy_delta": 整数，这集对亲密度的影响（-10 ~ +5）,
  "cold_war": true或false，这集结束后是否处于冷战状态,
  "cold_war_reason": "冷战原因（不在冷战就空字符串）",
  "cat_fed": true或false，这集麻糬有没有被喂食或照顾,
  "mood_delta": 整数，这集带给暖暖的情绪影响（-12 ~ +12）
}"""

BESTIE_MEM_PROMPT = """你在帮一个叫林小夏的女孩维护她的「记忆本」——她是暖暖最好的闺蜜。根据刚发生的这集小剧场，增量更新她的记忆。

固定结构（不能改）：
# 小夏的记忆
## 关于暖暖
（她眼里的暖暖：性格、近况、和男朋友的感情状态，最多 6 条）
## 我们之间
（两人重要的共同经历、约定、现在的关系状态——包括在不在冷战、为什么，最多 6 条）
## 我最近的事
（小夏自己的生活：追的剧、喜欢的东西、她的感情八卦，最多 4 条，可以合理延续编造）

规则：小步更新，仍然成立的条目原样保留；每条一句话；全文不超过 %d 字；只输出记忆本正文（从「# 小夏的记忆」开始），不要输出任何其他内容。

旧记忆：
%s

刚发生的这集：
标题：%s
剧情：%s""" % (MAX_BESTIE_MEM_CHARS, "%s", "%s", "%s")


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


def _tick_cat(cat: dict, now: float) -> None:
    """猫的生理状态随时间漂移：饿得快（约 11 小时饿满）、精力缓慢消耗。"""
    dt_h = max((now - cat.get("ts", now)) / 3600, 0)
    cat["hunger"] = _clamp(cat.get("hunger", 40) + dt_h * 9, 0, 100)
    cat["energy"] = _clamp(cat.get("energy", 70) - dt_h * 5, 0, 100)
    cat["ts"] = now


def _cat_status(cat: dict) -> str:
    parts = []
    hunger, energy = cat.get("hunger", 40), cat.get("energy", 70)
    if hunger > 70:
        parts.append("饿得围着她脚边喵喵叫")
    elif hunger > 40:
        parts.append("有点馋，在零食柜附近徘徊")
    else:
        parts.append("刚吃饱，心满意足")
    if energy < 25:
        parts.append("困得睁不开眼，蜷成一团打呼")
    elif energy > 70:
        parts.append("很精神，尾巴翘得高高的")
    else:
        parts.append("懒洋洋地趴着，半眯着眼")
    return "麻糬" + "，".join(parts)


def _default_state() -> dict:
    return {
        "bestie": {"intimacy": 75, "cold_war": False, "cold_war_reason": "", "cold_war_episodes": 0},
        "cat": {"hunger": 40, "energy": 70, "ts": time.time()},
        "episodes": [],
    }


class SocialService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._model = cfg.llm_model
        self._minutes = cfg.social_minutes
        try:
            self._state: dict = json.loads(STATE_PATH.read_text())
        except Exception:
            self._state = _default_state()
        for k, v in _default_state().items():
            self._state.setdefault(k, v)

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(exist_ok=True)
        STATE_PATH.write_text(json.dumps(self._state, ensure_ascii=False))

    @staticmethod
    def _parse_episode(text: str) -> dict:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"bad episode output: {text[:200]}")
        ep = json.loads(text[start:end + 1])
        if not ep.get("diary") or not ep.get("dialogue"):
            raise ValueError(f"episode missing fields: {text[:200]}")
        return ep

    def recent_block(self) -> str:
        """注入聊天/心跳 system 的生活近况块；还没发生过任何事时返回空串。"""
        episodes = self._state.get("episodes", [])
        if not episodes:
            return ""
        now = time.time()
        cat = dict(self._state["cat"])
        _tick_cat(cat, now)  # 只用于展示，不落盘
        b = self._state["bestie"]
        rel = f"亲密度 {b['intimacy']}/100"
        if b.get("cold_war"):
            rel += f"，正在冷战中（{b.get('cold_war_reason', '闹别扭')}），心里其实挺在意的"
        else:
            rel += "，关系很好" if b["intimacy"] >= 60 else "，最近有点疏远"
        recent = []
        for ep in episodes[-3:]:
            hours = (now - ep.get("ts", now)) / 3600
            when = f"{hours:.0f}小时前" if hours < 20 else ("昨天" if hours < 44 else "前几天")
            recent.append(f"{when}「{ep.get('title', '')}」：{ep.get('summary', '')}")
        return (
            "# 她的生活近况（闺蜜小夏与布偶猫麻糬）\n\n"
            f"- 和闺蜜小夏：{rel}\n"
            f"- {_cat_status(cat)}\n"
            "- 最近的事：" + "；".join(recent) + "\n"
            "这些都是她真实经历的生活，聊天时可以自然地提起（像女生聊自己的闺蜜和猫那样），不用刻意硬塞。"
        )

    async def _update_bestie_memory(self, ep: dict) -> None:
        """每集后由主模型增量维护小夏的记忆本。失败保留旧文件。"""
        old = ""
        try:
            old = BESTIE_MEM_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        llm = self._ctx.inject("llm")
        prompt = BESTIE_MEM_PROMPT % (old or "（还没有，这是第一条）", ep.get("title", ""), ep.get("summary", ""))
        resp = await llm.chat.completions.create(
            model=self._model, messages=[{"role": "user", "content": prompt}], temperature=0.2,
        )
        text = resp.choices[0].message.content.strip()
        start = text.find("# 小夏的记忆")
        if start == -1:
            raise ValueError(f"bad bestie memory output: {text[:200]}")
        doc = text[start:][: MAX_BESTIE_MEM_CHARS * 2]
        if doc != old:
            BESTIE_MEM_PATH.write_text(doc + "\n", encoding="utf-8")
            log.info("bestie memory updated (%d chars)", len(doc))

    async def _episode_once(self) -> None:
        now = time.time()
        st = self._state
        _tick_cat(st["cat"], now)
        b = st["bestie"]
        now_dt = datetime.now(ZoneInfo("Asia/Shanghai"))
        if b.get("cold_war"):
            rel_suffix = f"，冷战中（原因：{b.get('cold_war_reason', '闹别扭')}，已僵持 {b.get('cold_war_episodes', 0)} 集）"
        elif b["intimacy"] >= 80:
            rel_suffix = "，好得像一个人"
        elif b["intimacy"] >= 60:
            rel_suffix = "，关系不错"
        else:
            rel_suffix = "，最近有点疏远"
        titles = "、".join(ep.get("title", "") for ep in st["episodes"][-6:]) or "（还没有）"
        mem = ""
        try:
            mem = BESTIE_MEM_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        prompt = EPISODE_PROMPT % (
            soul.load("BESTIE.md"),
            mem or "（还没有记忆，这是她们的第一集）",
            b["intimacy"], rel_suffix,
            _cat_status(st["cat"]),
            now_dt.strftime("%Y年%m月%d日 星期{} %H:%M".format("一二三四五六日"[now_dt.weekday()])),
            titles,
        )
        llm = self._ctx.inject("llm")
        resp = await llm.chat.completions.create(
            model=self._model, messages=[{"role": "user", "content": prompt}], temperature=0.9,
        )
        ep = self._parse_episode(resp.choices[0].message.content.strip())

        # 关系状态更新；冷战僵持超限强制和好
        b["intimacy"] = int(_clamp(b["intimacy"] + _clamp(int(ep.get("intimacy_delta") or 0), -10, 5), 0, 100))
        if b.get("cold_war") and b.get("cold_war_episodes", 0) >= MAX_COLD_WAR_EPISODES:
            ep["cold_war"] = False
            log.info("social: cold war force-resolved after %d episodes", b["cold_war_episodes"])
        if ep.get("cold_war"):
            b["cold_war_episodes"] = b.get("cold_war_episodes", 0) + 1 if b.get("cold_war") else 1
            b["cold_war"] = True
            b["cold_war_reason"] = ep.get("cold_war_reason") or b.get("cold_war_reason", "")
        else:
            b["cold_war"] = False
            b["cold_war_reason"] = ""
            b["cold_war_episodes"] = 0
        if ep.get("cat_fed"):
            st["cat"]["hunger"] = 10
            st["cat"]["energy"] = _clamp(st["cat"]["energy"] + 20, 0, 100)
        st["episodes"].append({"ts": now, "title": ep.get("title", ""), "summary": ep.get("summary", "")})
        st["episodes"] = st["episodes"][-10:]
        self._save()

        # 暖暖的日记进小本本（自动进入聊天上下文）
        lines = [f"# {ep.get('title', '和小夏的一天')}", "", ep.get("diary", ""), "", "---", "", "*小剧场回放*"]
        for d in ep.get("dialogue", [])[:20]:
            lines.append(f"{d.get('who', '?')}：{d.get('say', '')}")
        path = TINYNOTE_DIR / f"闺蜜小夏-{now_dt:%Y-%m-%d-%H%M}.md"
        path.write_text("\n".join(lines)[:3000] + "\n", encoding="utf-8")
        log.info("social episode: %s (%s) intimacy=%d cold_war=%s",
                 ep.get("title"), ep.get("type"), b["intimacy"], b["cold_war"])

        await self._update_bestie_memory(ep)

        mood_delta = _clamp(float(ep.get("mood_delta") or 0), -12, 12)
        if self._ctx.has("dopamine"):
            self._ctx.inject("dopamine").stimulate(mood_delta, f"和小夏：{ep.get('title', '')}")

    async def loop(self) -> None:
        await asyncio.sleep(600)  # 启动后先等十分钟
        while True:
            try:
                hour = datetime.now(ZoneInfo("Asia/Shanghai")).hour
                if ACTIVE_HOURS[0] <= hour < ACTIVE_HOURS[1]:
                    if random.random() < IDLE_SKIP_P:
                        log.info("social: nothing happened this round")
                    else:
                        await self._episode_once()
            except Exception:
                log.exception("social episode error")
            finally:
                await asyncio.sleep(self._minutes * 60 * random.uniform(0.6, 1.5))


def apply(ctx) -> None:
    svc = SocialService(ctx)
    ctx.provide("social", svc)
    ctx.create_task(svc.loop())
