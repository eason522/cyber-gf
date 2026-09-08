import base64
import gzip
import json
import logging
import os
import uuid
from pathlib import Path

import websockets

logging.getLogger("tts_protocols").setLevel(logging.WARNING)

from tts_protocols import (
    CompressionBits,
    EventType,
    MsgType,
    SerializationBits,
    finish_connection,
    finish_session,
    receive_message,
    start_connection,
    start_session,
    task_request,
)

URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"


def _payload(event: EventType, req_params: dict) -> bytes:
    return json.dumps({
        "user": {"uid": "cyber-gf"},
        "event": int(event),
        "req_params": req_params,
    }).encode()


async def _recv(ws):
    msg = await receive_message(ws)
    if msg.compression == CompressionBits.Gzip and msg.payload:
        msg.payload = gzip.decompress(msg.payload)
    if msg.event in (EventType.ConnectionFailed, EventType.SessionFailed):
        raise RuntimeError(f"seed-tts failed: {msg}")
    return msg


async def _wait_event(ws, *events: EventType):
    while True:
        msg = await _recv(ws)
        if msg.event in events:
            return msg


async def synth(
    text: str,
    out_mp3: Path,
    *,
    context: "str | list[str]" = "",
    voice: str = "",
    speech_rate: int = 0,
    loudness: int = 0,
    pitch: int = 0,
    model: str = "",
) -> None:
    """豆包 seed-tts-2.0 双向流式合成（一次性整段文本）。失败抛异常，由调用方回退。

    context：语音指令/引用上文，走官方 additions.context_texts（JSON 字符串里的字段，
    不参与计费、不会被朗读；放 req_params 顶层会被服务端静默忽略）。
    指令必须写成纯粹的"声音描写"（用……的语气/哭腔说），对话式互动指令（"撩撩我"
    "你得跟我互怼"）会失效；context_texts 里混入指令以外的内容（引用上文、多条指令
    叠加）也会稀释效果——实测纯指令单条最稳。[#指令] 内联语法 API 不认识，会被念出来，
    已废弃。voice：留空用 DOUBAO_VOICE 环境变量。
    """
    key = os.environ["DOUBAO_API_KEY"]
    if not voice:
        voice = os.getenv("DOUBAO_VOICE", "zh_female_xiaohe_uranus_bigtts")
    base_ctx = os.getenv("DOUBAO_CONTEXT", "").strip()
    if not model:
        model = os.getenv("DOUBAO_TTS_MODEL", "")  # 默认 standard 子版本，官网页面同款
    headers = {"X-Api-Key": key, "X-Api-Resource-Id": "seed-tts-2.0"}
    audio = bytearray()
    async with websockets.connect(URL, additional_headers=headers, max_size=16 * 1024 * 1024) as ws:
        await start_connection(ws)
        await _wait_event(ws, EventType.ConnectionStarted)

        session_id = uuid.uuid4().hex
        audio_params = {"format": "mp3", "sample_rate": 24000}
        if speech_rate:
            audio_params["speech_rate"] = speech_rate
        if loudness:
            audio_params["loudness_rate"] = loudness
        additions = {
            "disable_emoji_filter": True,
            "disable_markdown_filter": True,
            "max_length_to_filter_parenthesis": 100,
        }
        if isinstance(context, str):
            ctx_list = [context.strip()] if context.strip() else []
        else:
            ctx_list = [c.strip() for c in context if c and c.strip()]
        contexts = ([base_ctx] if base_ctx else []) + ctx_list
        if contexts:
            additions["context_texts"] = contexts
        req_params = {
            "speaker": voice,
            "audio_params": audio_params,
            "additions": json.dumps(additions, ensure_ascii=False),
        }
        if model:
            req_params["model"] = model
        if pitch:
            req_params["post_process"] = {"pitch": pitch}
        await start_session(ws, _payload(EventType.StartSession, req_params), session_id)
        await _wait_event(ws, EventType.SessionStarted)

        await task_request(ws, _payload(EventType.TaskRequest, {"text": text}), session_id)
        await finish_session(ws, session_id)

        while True:
            msg = await _recv(ws)
            if msg.event == EventType.TTSResponse:
                if msg.type == MsgType.AudioOnlyServer or msg.serialization == SerializationBits.Raw:
                    audio += msg.payload
                else:
                    data = json.loads(msg.payload).get("data", "")
                    if data:
                        audio += base64.b64decode(data)
            elif msg.event == EventType.SessionFinished:
                break

        await finish_connection(ws)

    if not audio:
        raise RuntimeError("seed-tts returned no audio")
    out_mp3.write_bytes(bytes(audio))
