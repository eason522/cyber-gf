"""heartbeat 插件：45 分钟主动关心循环（原 bot.py 的 heartbeat_loop，逻辑逐字保留）。

静默契约：没事就 NO_REPLY，绝不打扰；不想打扰但有话想说时，可以写进小本本（tinynote/）。
沉默判定用北京时间（HEARTBEAT_SILENCE_H、ACTIVE_HOURS 深夜免打扰），心跳时她可以写
小本本（HEARTBEAT_TOOLS：给时间工具好让日记日期写对；不给联网搜索，保持安静场景纯粹）。

平台发送改为延迟 inject "platform" 服务（第三阶段才提供）：apply 时不取，
等到循环第一次真正要发消息时才 inject；取不到记 warning 并跳过本轮——无平台环境也能挂载。
platform 协议：async send(chat_id, text, ogg)，ogg 为语音文件路径（TTS 失败时为 None）。
"""

import asyncio
import json
import logging
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
        self._silence_h = cfg.heartbeat_silence_h
        self._platform_name = cfg.bot_platform
        self._model = cfg.llm_model
        # 心跳时她可以写小本本（给时间工具好让日记日期写对；不给联网搜索，保持安静场景纯粹）
        tools = ctx.inject("tools")
        self._tools_def = [t for t in tools.defs()
                           if t["function"]["name"] in ("get_current_time", "list_directory", "read_file", "write_file")]

    def _platform(self):
        """平台服务第三阶段才有；真要发消息时才取，取不到返回 None。"""
        try:
            return self._ctx.inject("platform")
        except KeyError:
            return None

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
                store = sessions.get_store(uid)
                recalled = await self._ctx.inject("memory").recall("最近关心他、问候他、约定、他的近况")
                mem_block = recalled or "\n".join(f"- {m}" for m in store["memories"])
                system = self._ctx.inject("persona").system_prompt(mem_block)
                now = now_dt.strftime("%H:%M")
                msgs = [
                    {"role": "system", "content": system},
                    *sessions.recent(uid),
                    {"role": "user", "content": (
                        f"（系统提示：现在是{now}，他已经{silence_h:.1f}小时没和你说话了。"
                        "如果你想主动关心他，就调用 reply 工具发一条消息；"
                        "如果不想打扰他、但心里有话想说，可以写进你的小本本"
                        "（用 write_file 写到 ~/cyber-gf/tinynote/，比如日记、随笔，写之前可以先用 list_directory 看看以前写过什么）；"
                        "如果什么都不想做（比如刚聊过不久、没有理由打扰），就只回复 NO_REPLY，什么也别发。）"
                    )},
                ]
                text = ""
                emotion = "温柔"
                tools = self._ctx.inject("tools")
                llm = self._ctx.inject("llm")
                for _ in range(3):  # 行动工具（写/翻小本本）最多循环 3 轮，reply 或 NO_REPLY 收尾
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
                await asyncio.sleep(self._minutes * 60)


def apply(ctx) -> None:
    ctx.create_task(HeartbeatService(ctx).loop())
