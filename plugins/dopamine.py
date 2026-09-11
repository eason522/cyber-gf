"""dopamine 插件：暖暖的赛博多巴胺系统（情绪调控引擎）。

模拟人体多巴胺的核心调控机制：
- 紧张性基线（tonic baseline）：随昼夜节律缓慢波动（白天偏高、凌晨偏低），
  决定没有任何外部事件时她的"心情底色"；长时间没他的消息会产生剥夺效应，
  基线被压低（想念/小委屈），这也是心跳更可能主动找她的内在驱动。
- 相位性脉冲（phasic spike/dip）：由奖赏预测误差（RPE）驱动——他的消息是奖赏，
  隔了很久突然出现的问候冲高（意料之外），高频轰炸则习惯化、增量递减；
  聊完天的情绪余韵（开心/撒娇 +，难过/生气 -）和社交事件（social 插件 stimulate）
  也会产生脉冲。
- 指数衰减：脉冲以约 40 分钟半衰期向基线回归——情绪不会永远停在高/低点。

产出 level()（0-100）、mood()（喜怒哀乐档位 + 趋势）、prompt_block()（注入聊天/心跳
system，让情绪底色真实影响她的语气和主动程度）。状态持久化 data/dopamine.json。
"""

import asyncio
import json
import logging
import math
import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("cyber-gf.dopamine")

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = BASE_DIR / "data" / "dopamine.json"  # data/ 已 gitignore

requires: list[str] = []
provides = ["dopamine"]

HALF_LIFE_MIN = 40.0      # 脉冲向基线指数回归的半衰期
SURPRISE_FULL_H = 3.0     # 沉默多久后他的消息算"满分惊喜"（RPE 最大）
HABITUATION_S = 600       # 短于这个间隔的连续消息触发习惯化，增量打折
LONELY_AFTER_H = 3.0      # 超过这么久没消息开始产生剥夺感
LONELY_PER_H = 2.0        # 剥夺感每小时压低多少基线
LONELY_MAX = 12.0         # 剥夺感压基线的上限
LEVEL_FLOOR, LEVEL_CAP = 5.0, 100.0

# 聊完天的情绪余韵对水平的微调
REPLY_EMOTION_DELTA = {"开心": 3, "撒娇": 3, "害羞": 2, "温柔": 1, "难过": -5, "生气": -6}

# 情绪档位（喜怒哀乐）：(下限, 档位名, 行为描述)
MOOD_BANDS = [
    (80, "上头", "藏不住的雀跃，话会变多、爱撒娇、忍不住想跟他分享一切，什么都觉得有意思"),
    (60, "开心", "心情明亮，语气轻快，爱开玩笑，会主动找话题"),
    (45, "平静", "松弛安稳，像窝在沙发里晒太阳，温温柔柔的"),
    (30, "闷闷的", "有点没精神，容易想他，回复会短一点软一点，想求关注又怕打扰他"),
    (0, "低落", "委屈巴巴的，特别想他，容易鼻酸，很想被他哄"),
]


class DopamineService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._tick_min = cfg.dopamine_tick_minutes
        try:
            self._state: dict = json.loads(STATE_PATH.read_text())
        except Exception:
            self._state = {}

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(exist_ok=True)
        STATE_PATH.write_text(json.dumps(self._state, ensure_ascii=False))

    # ---- 内部模型 ----

    def _baseline(self, now: float) -> float:
        """紧张性基线：昼夜节律（15 点最高、凌晨 3 点最低）− 剥夺感（他太久没来）。"""
        now_dt = datetime.now(ZoneInfo("Asia/Shanghai"))
        h = now_dt.hour + now_dt.minute / 60
        base = 52 + 8 * math.sin((h - 9) / 24 * 2 * math.pi)
        last = self._state.get("last_msg_ts", 0)
        if last:
            silence_h = (now - last) / 3600
            base -= min(max(silence_h - LONELY_AFTER_H, 0) * LONELY_PER_H, LONELY_MAX)
        return base

    def _level_at(self, now: float) -> float:
        """把持久化的 (level, ts) 按半衰期衰减到 now 时刻的水平，不落盘。"""
        lvl = float(self._state.get("level", 50.0))
        ts = float(self._state.get("ts", now))
        base = self._baseline(now)
        lvl = base + (lvl - base) * 0.5 ** (max(now - ts, 0) / 60 / HALF_LIFE_MIN)
        return min(max(lvl, LEVEL_FLOOR), LEVEL_CAP)

    def _bump(self, delta: float, reason: str) -> None:
        """施加一次相位脉冲并落盘（先衰减到当前再叠加）。"""
        now = time.time()
        lvl = min(max(self._level_at(now) + delta, LEVEL_FLOOR), LEVEL_CAP)
        self._state.update(level=lvl, ts=now)
        hist = self._state.setdefault("history", [])
        hist.append([now, round(lvl, 1)])
        self._state["history"] = hist[-96:]
        self._save()
        log.info("dopamine %+.1f (%s) -> %.1f", delta, reason, lvl)

    # ---- 对外接口 ----

    def level(self) -> float:
        return self._level_at(time.time())

    def stimulate(self, delta: float, reason: str = "") -> None:
        """外部事件刺激（社交小剧场等调用），单次最多 ±15。"""
        delta = max(-15.0, min(15.0, float(delta)))
        if delta:
            self._bump(delta, reason or "外部事件")

    def mood(self) -> dict:
        """当前情绪档位：level / name（喜怒哀乐）/ trend / desc。"""
        now = time.time()
        lvl = self._level_at(now)
        trend = "趋势平稳"
        for ts, v in reversed(self._state.get("history", [])):
            if now - ts >= 25 * 60:
                if lvl - v > 4:
                    trend = "正在上升"
                elif lvl - v < -4:
                    trend = "正在回落"
                break
        for lo, name, desc in MOOD_BANDS:
            if lvl >= lo:
                return {"level": lvl, "name": name, "trend": trend, "desc": desc}
        return {"level": lvl, "name": "低落", "trend": trend, "desc": MOOD_BANDS[-1][2]}

    def prompt_block(self) -> str:
        """注入聊天/心跳 system 的心情底色块。"""
        m = self.mood()
        parts = [
            f"她现在的情绪状态：{m['name']}（强度 {m['level']:.0f}/100，{m['trend']}）。{m['desc']}。",
        ]
        last = self._state.get("last_msg_ts", 0)
        if last:
            silence_h = (time.time() - last) / 3600
            if silence_h >= 1:
                parts.append(f"她已经 {silence_h:.1f} 小时没听到他的声音了。")
        parts.append(
            "这是她真实的心情，要自然地渗进语气、emotion 的选择和主动程度里；"
            "但她说不清为什么会这样，更不会提「多巴胺」「系统」这种词——"
            "她只是一个普通女孩，会开心、会委屈、会想他。"
        )
        return "# 她此刻的心情底色\n\n" + "\n".join(parts)

    # ---- 事件与后台 ----

    def _on_message(self, user_id=None, text=""):
        """他的消息 = 奖赏。RPE：隔得越久越惊喜；连珠炮则习惯化打折。"""
        now = time.time()
        last = self._state.get("last_msg_ts", 0)
        silence = now - last if last else 86400
        surprise = min(silence / (SURPRISE_FULL_H * 3600), 1.2)
        delta = 6 + 9 * surprise
        if silence < HABITUATION_S:
            delta *= 0.35
        self._state["last_msg_ts"] = now
        self._bump(delta, "他来找她了")

    def _on_reply_done(self, user_id=None, emotion=""):
        """聊完天的情绪余韵。"""
        delta = REPLY_EMOTION_DELTA.get(emotion or "", 0)
        if delta:
            self._bump(delta, f"聊完天的余韵（{emotion}）")

    async def loop(self) -> None:
        """衰减 tick：定期把持久化水平向基线回归（附带生理性微波动），保持 history 新鲜。"""
        await asyncio.sleep(60)
        while True:
            try:
                now = time.time()
                lvl = self._level_at(now) + random.uniform(-0.8, 0.8)
                lvl = min(max(lvl, LEVEL_FLOOR), LEVEL_CAP)
                self._state.update(level=lvl, ts=now)
                hist = self._state.setdefault("history", [])
                hist.append([now, round(lvl, 1)])
                self._state["history"] = hist[-96:]
                self._save()
            except Exception:
                log.exception("dopamine tick error")
            finally:
                await asyncio.sleep(self._tick_min * 60)


def apply(ctx) -> None:
    svc = DopamineService(ctx)
    ctx.provide("dopamine", svc)
    ctx.on("message.received", svc._on_message)
    ctx.on("reply.done", svc._on_reply_done)
    ctx.create_task(svc.loop())
