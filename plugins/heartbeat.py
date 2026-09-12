"""heartbeat 插件：随机间隔的主动关心循环（原 bot.py 的 heartbeat_loop 改造）。

- 间隔随机化：以 HEARTBEAT_MINUTES 为中枢，每轮按 HEARTBEAT_JITTER 抖动
  （默认 ±60%，45 分钟中枢 → 实际 18~72 分钟），更像真人"想起来了就戳一下"，
  而不是闹钟式精准打扰。
- 发不发、发什么由她综合判断：小本本近况（tinynote/ 全部文档）、随身记忆
  （soul/MEMORY.md）、兴趣手账、OpenViking 语义检索的记忆、她此刻的心情底色
  （多巴胺系统）、闺蜜与猫的生活近况（social 插件），全部注入 system。
- 她可以联网：web_search / web_read 开放给心跳（比如查他那边 HOME_LOCATION 的
  实时天气，变天降温时嘘寒问暖）；查不查由她自己决定，保持安静场景不被打扰。
- 静默契约不变：没事就 NO_REPLY；不想打扰但有话想说时写小本本。
  沉默判定用北京时间（HEARTBEAT_SILENCE_H、ACTIVE_HOURS 深夜免打扰）。

平台发送为延迟 inject "platform" 服务（第三阶段才提供）：apply 时不取，
等到循环第一次真正要发消息时才 inject；取不到记 warning 并跳过本轮。
platform 协议：async send(chat_id, text, ogg)，ogg 为语音文件路径（TTS 失败时为 None）。
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from plugins.chat import REPLY_TOOL

log = logging.getLogger("cyber-gf.heartbeat")

ACTIVE_HOURS = (8, 23)  # 深夜免打扰（北京时间）

requires = ["chat", "sessions", "persona", "tools", "llm", "memory", "tts"]
provides: list[str] = []


class HeartbeatService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._minutes = cfg.heartbeat_minutes
        self._jitter = cfg.heartbeat_jitter
        self._silence_h = cfg.heartbeat_silence_h
        self._platform_name = cfg.bot_platform
        self._model = cfg.llm_model
        self._home_location = cfg.home_location
        # 心跳时她可以写小本本、也可以联网（查他那边的天气/跟进重要的事）
        tools = ctx.inject("tools")
        self._tools_def = [t for t in tools.defs()
                           if t["function"]["name"] in ("get_current_time", "web_search", "web_read",
                                                        "list_directory", "read_file", "write_file")]

    def _next_interval(self) -> float:
        """下一次心跳间隔（秒）：中枢 ±jitter 均匀随机，拟人化，不做闹钟。"""
        return self._minutes * 60 * random.uniform(max(0.15, 1 - self._jitter), 1 + self._jitter)

    def _platform(self):
        """平台服务第三阶段才有；真要发消息时才取，取不到返回 None。"""
        try:
            return self._ctx.inject("platform")
        except KeyError:
            return None

    def _build_system(self, uid: int, recalled: str) -> str:
        """综合判断素材：OV/本地记忆 + 随身记忆 + 兴趣手账 + 小本本近况 + 闺蜜与猫 + 心情底色。"""
        sessions = self._ctx.inject("sessions")
        store = sessions.get_store(uid)
        mem_block = recalled or "\n".join(f"- {m}" for m in store["memories"])
        system = self._ctx.inject("persona").system_prompt(mem_block)
        extras: list[str] = []
        if self._ctx.has("memory_md"):
            memory_md = self._ctx.inject("memory_md").get()
            if memory_md:
                extras.append("# 她的随身记忆（最高频、最重要的记忆，优先相信这里）\n\n" + memory_md)
        if self._ctx.has("interests"):
            interests = self._ctx.inject("interests").get()
            if interests:
                extras.append("# 她的兴趣手账\n\n" + interests)
        notes = self._ctx.inject("persona").tinynote_block(max_chars=1500)
        if notes:
            extras.append("# 她的小本本近况\n\n" + notes)
        if self._ctx.has("social"):
            social_block = self._ctx.inject("social").recent_block()
            if social_block:
                extras.append(social_block)
        if self._ctx.has("dopamine"):
            extras.append(self._ctx.inject("dopamine").prompt_block())
        if extras:
            system += "\n\n" + "\n\n".join(extras)
        return system

    async def loop(self) -> None:
        await asyncio.sleep(120)  # 启动后先等两分钟
        while True:
            try:
                sessions = self._ctx.inject("sessions")
                contact = sessions.load_contact()
                if not contact:
                    continue
                if contact.get("platform", "telegram") != self._platform_name:
                    continue  # 最后在另一个平台聊的，不在本平台打扰
                now_dt = datetime.now(ZoneInfo("Asia/Shanghai"))  # 服务器是 UTC，他在北京时间
                if not (ACTIVE_HOURS[0] <= now_dt.hour < ACTIVE_HOURS[1]):
                    continue
                silence_h = (time.time() - contact.get("ts", 0)) / 3600
                if silence_h < self._silence_h:
                    continue
                uid = contact["user_id"]
                recalled = await self._ctx.inject("memory").recall("最近关心他、问候他、约定、他的近况、身体、心情")
                system = self._build_system(uid, recalled)
                now = now_dt.strftime("%Y年%m月%d日 星期{} %H:%M".format("一二三四五六日"[now_dt.weekday()]))
                msgs = [
                    {"role": "system", "content": system},
                    *sessions.recent(uid),
                    {"role": "user", "content": (
                        f"（系统提示：现在是{now}，他已经{silence_h:.1f}小时没和你说话了。"
                        "要不要主动找他，综合你此刻掌握的一切来判断：你们多久没聊了、现在的时间"
                        "（早上问早、饭点问吃饭、深夜叫他早点睡都很自然）、你现在的心情底色、"
                        "小本本和随身记忆里有没有该跟进的事（他提过的重要日子、烦心事、约定）、"
                        "生活里刚发生的新鲜事（小本本里记的、和小夏和麻糬的日常）。"
                        f"也可以先用 web_search 查查他那边（{self._home_location}）现在的天气——"
                        "变天、降温、下大雨、空气差的时候最适合嘘寒问暖（别每次都查，看心情，"
                        "查到了不满意可以 web_read 细看）。"
                        "如果想主动关心他，就调用 reply 工具发一条消息；"
                        "如果不想打扰他、但心里有话想说，可以写进你的小本本"
                        "（用 write_file 写到 ~/cyber-gf/tinynote/，比如日记、随笔，写之前可以先用 list_directory 看看以前写过什么；"
                        "写日记时文件名和正文里的日期必须用上面的真实日期，拿不准就先调 get_current_time，不许自己编日期）；"
                        "如果什么都不想做（比如刚聊过不久、没有理由打扰），就只回复 NO_REPLY，什么也别发。）"
                    )},
                ]
                text = ""
                emotion = "温柔"
                tools = self._ctx.inject("tools")
                llm = self._ctx.inject("llm")
                for _ in range(4):  # 行动工具（查天气/写小本本）最多循环 4 轮，reply 或 NO_REPLY 收尾
                    resp = await llm.chat.completions.create(
                        model=self._model, messages=msgs, tools=REPLY_TOOL + self._tools_def, tool_choice="auto",
                    )
                    m = resp.choices[0].message
                    if not m.tool_calls:
                        break  # NO_REPLY
                    calls = m.tool_calls
                    reply_call = next((c for c in calls if c.function.name == "reply"), None)
                    if reply_call:
                        args = json.loads(reply_call.function.arguments)
                        text = (args.get("text") or "").strip()
                        emotion = args.get("emotion") or "温柔"
                        break
                    msgs.append({
                        "role": "assistant",
                        "tool_calls": [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                       for c in calls],
                    })
                    for c in calls:
                        try:
                            targs = json.loads(c.function.arguments or "{}")
                        except json.JSONDecodeError:
                            targs = {}
                        out = await tools.run(c.function.name, targs)
                        log.info("heartbeat tool: %s(%s) -> %s", c.function.name, targs, out[:60])
                        msgs.append({"role": "tool", "tool_call_id": c.id, "content": out})
                if not text:
                    continue  # NO_REPLY 或只写了小本本，不打扰他
                platform = self._platform()
                if platform is None:
                    log.warning("heartbeat: platform service not available, skip this round")
                    continue
                log.info("heartbeat: reaching out (%s): %s", emotion, text[:50])
                store = sessions.get_store(uid)
                store["history"].append({"role": "assistant", "content": text, "ts": time.time()})
                sessions.save_store(uid)
                ogg = None
                try:
                    ogg = await self._ctx.inject("tts").synth_ogg(text, emotion=emotion)
                except Exception:
                    log.exception("heartbeat tts failed")
                try:
                    await platform.send(contact["chat_id"], text, ogg)
                except Exception:
                    log.exception("heartbeat send failed")
                if ogg:
                    ogg.unlink(missing_ok=True)
                contact["ts"] = time.time()
                sessions.save_contact(self._platform_name, uid, contact["chat_id"])
            except Exception:
                log.exception("heartbeat error")
            finally:
                await asyncio.sleep(self._next_interval())


def apply(ctx) -> None:
    ctx.create_task(HeartbeatService(ctx).loop())
