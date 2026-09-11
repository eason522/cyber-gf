"""scheduler 插件：计划任务/提醒——他把事交给她（"明早8点叫我"），她到点执行。

注册三个工具：schedule_task（安排：一次性 at / 每天 daily / in_minutes 分钟后）、
list_scheduled、cancel_scheduled。任务持久化在 data/schedule.json，重启不丢。
调度循环每 20 秒扫一次到期任务；执行走心跳同款链路：
persona + 随身记忆 → 强制 reply 工具生成她语气的消息 → TTS 语音 → platform.send。
chat_id 优先用安排任务时 meta 传入的，否则回退 contact.json（最后聊过的会话）。
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from plugins.chat import REPLY_TOOL

log = logging.getLogger("cyber-gf.scheduler")

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEDULE_FILE = BASE_DIR / "data" / "schedule.json"
TZ = ZoneInfo("Asia/Shanghai")
TICK_SECONDS = 20

requires = ["llm", "persona", "tools", "sessions", "tts"]
provides = ["scheduler"]

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": "安排定时任务/提醒。他让你在某个时间提醒他或做某事时用"
                           "（比如「明早8点叫我起床」「半小时后提醒我关火」）。"
                           "相对时间用 in_minutes；绝对时间先调 get_current_time 确认现在几点再填 at；"
                           "每天重复的填 daily",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "到点要做的事，写完整具体（执行时只有这句话可参考）"},
                    "at": {"type": "string", "description": "一次性执行时间，格式 2026-09-12 08:00（北京时间）"},
                    "daily": {"type": "string", "description": "每天执行，格式 08:00（北京时间）"},
                    "in_minutes": {"type": "number", "description": "多少分钟后执行"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled",
            "description": "查看已经安排、还没执行的计划任务",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_scheduled",
            "description": "取消一个计划任务，id 用 list_scheduled 查",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "number", "description": "任务 id"}},
                "required": ["id"],
            },
        },
    },
]


def _parse_when(args: dict) -> tuple[datetime, bool] | str:
    """解析执行时间，返回 (时间, 是否每天重复)；失败返回错误说明字符串。"""
    now = datetime.now(TZ)
    if args.get("in_minutes"):
        return now + timedelta(minutes=float(args["in_minutes"])), False
    daily = (args.get("daily") or "").strip()
    if daily:
        try:
            h, m = daily.split(":")[:2]
            t = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        except (ValueError, AttributeError):
            return "daily 格式不对，要 HH:MM，比如 08:00"
        if t <= now:
            t += timedelta(days=1)
        return t, True
    at = (args.get("at") or "").strip()
    if at:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%m-%d %H:%M"):
            try:
                t = datetime.strptime(at, fmt)
                if fmt == "%m-%d %H:%M":
                    t = t.replace(year=now.year)
                return t.replace(tzinfo=TZ), False
            except ValueError:
                continue
        return "at 格式不对，要 2026-09-12 08:00 这样（北京时间）"
    return "要告诉我什么时候做：in_minutes（多少分钟后）、at（具体时间）或 daily（每天几点）至少填一个"


class Scheduler:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._model = cfg.llm_model
        self._platform_name = cfg.bot_platform
        self._tasks: list[dict] = []
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(SCHEDULE_FILE.read_text())
            self._tasks = data.get("tasks", [])
            self._next_id = data.get("next_id", 1)
            log.info("scheduler: loaded %d tasks", len(self._tasks))
        except Exception:
            pass

    def _save(self) -> None:
        SCHEDULE_FILE.parent.mkdir(exist_ok=True)
        SCHEDULE_FILE.write_text(json.dumps(
            {"next_id": self._next_id, "tasks": self._tasks}, ensure_ascii=False, indent=1))

    # ---- 工具入口 ----

    def tool(self, name: str, args: dict, meta: dict | None) -> str:
        user_id = (meta or {}).get("user_id", 0)
        chat_id = (meta or {}).get("chat_id")
        if name == "schedule_task":
            return self._add(user_id, chat_id, (args.get("task") or "").strip(), args)
        if name == "list_scheduled":
            return self._list(user_id)
        if name == "cancel_scheduled":
            return self._cancel(user_id, int(args.get("id") or 0))
        return f"没有 {name} 这个工具"

    def _add(self, user_id: int, chat_id, task: str, args: dict) -> str:
        if not task:
            return "没说要做什么事欸"
        parsed = _parse_when(args)
        if isinstance(parsed, str):
            return parsed
        when, daily = parsed
        if when <= datetime.now(TZ):
            return "这个时间已经过了欸"
        self._tasks.append({
            "id": self._next_id, "user_id": user_id, "chat_id": chat_id,
            "task": task, "at": when.isoformat(), "daily": daily,
            "created_at": time.time(),
        })
        self._next_id += 1
        self._save()
        when_s = when.strftime("%m月%d日 %H:%M")
        log.info("scheduled #%d for %s (daily=%s): %s", self._next_id - 1, when_s, daily, task[:40])
        return f"记好啦（任务 #{self._next_id - 1}）：{when_s}{'（每天）' if daily else ''}——{task}"

    def _list(self, user_id: int) -> str:
        tasks = [t for t in self._tasks if t["user_id"] == user_id]
        if not tasks:
            return "现在没有安排中的任务"
        lines = []
        for t in tasks:
            when = datetime.fromisoformat(t["at"]).strftime("%m月%d日 %H:%M")
            lines.append(f"#{t['id']} {when}{'（每天）' if t['daily'] else ''}：{t['task']}")
        return "\n".join(lines)

    def _cancel(self, user_id: int, task_id: int) -> str:
        for t in self._tasks:
            if t["id"] == task_id and t["user_id"] == user_id:
                self._tasks.remove(t)
                self._save()
                return f"已取消任务 #{task_id}：{t['task']}"
        return f"没找到任务 #{task_id}"

    # ---- 调度与执行 ----

    async def loop(self) -> None:
        await asyncio.sleep(10)  # 启动后先缓一下
        while True:
            try:
                now = datetime.now(TZ)
                due = [t for t in self._tasks if datetime.fromisoformat(t["at"]) <= now]
                for t in due:
                    if t["daily"]:
                        t["at"] = (datetime.fromisoformat(t["at"]) + timedelta(days=1)).isoformat()
                    else:
                        self._tasks.remove(t)
                    self._save()
                    try:
                        await self._execute(t)
                    except Exception:
                        log.exception("scheduled task #%s failed", t["id"])
            except Exception:
                log.exception("scheduler loop error")
            await asyncio.sleep(TICK_SECONDS)

    async def _execute(self, task: dict) -> None:
        """到点执行：她语气的消息 + 语音 → platform.send（心跳同款链路）。"""
        user_id = task["user_id"]
        sessions = self._ctx.inject("sessions")
        persona = self._ctx.inject("persona")
        llm = self._ctx.inject("llm")
        system = persona.system_prompt("")
        if self._ctx.has("memory_md"):
            mem_md = self._ctx.inject("memory_md").get()
            if mem_md:
                system += "\n\n# 她的随身记忆\n\n" + mem_md
        now = datetime.now(TZ).strftime("%Y年%m月%d日 %H:%M")
        msgs = [
            {"role": "system", "content": system},
            *sessions.recent(user_id),
            {"role": "user", "content": (
                f"（系统提示：现在是{now}，你之前答应他的事到点了：「{task['task']}」。"
                "调用 reply 工具，用你平时的语气给他发消息，自然地完成这个任务。）"
            )},
        ]
        resp = await llm.chat.completions.create(
            model=self._model, messages=msgs,
            tools=REPLY_TOOL, tool_choice={"type": "function", "function": {"name": "reply"}},
        )
        m = resp.choices[0].message
        if not m.tool_calls:
            log.warning("scheduled #%s: model did not reply, skip", task["id"])
            return
        args = json.loads(m.tool_calls[0].function.arguments)
        text = (args.get("text") or "").strip()
        emotion = args.get("emotion") or "温柔"
        if not text:
            return
        try:
            platform = self._ctx.inject("platform")
        except KeyError:
            log.warning("scheduled #%s: platform service not available, skip send", task["id"])
            return
        contact = sessions.load_contact()
        chat_id = task.get("chat_id") or (
            contact.get("chat_id") if contact.get("user_id") == user_id else user_id)
        log.info("scheduled #%s executing: (%s) %s", task["id"], emotion, text[:50])
        store = sessions.get_store(user_id)
        store["history"].append({"role": "assistant", "content": text, "ts": time.time()})
        sessions.save_store(user_id)
        ogg = None
        try:
            ogg = await self._ctx.inject("tts").synth_ogg(text, emotion=emotion)
        except Exception:
            log.exception("scheduled tts failed")
        try:
            await platform.send(chat_id, text, ogg)
        except Exception:
            log.exception("scheduled send failed")
        if ogg:
            ogg.unlink(missing_ok=True)
        contact = sessions.load_contact()
        sessions.save_contact(self._platform_name, user_id, contact.get("chat_id") or chat_id)


def apply(ctx) -> None:
    sched = Scheduler(ctx)
    tools = ctx.inject("tools")
    for schema in TOOL_SCHEMAS:
        name = schema["function"]["name"]
        tools.register(schema, lambda args, meta=None, _n=name: sched.tool(_n, args, meta))
    ctx.provide("scheduler", sched)
    ctx.create_task(sched.loop())
