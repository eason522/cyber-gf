"""豆包录音文件识别 2.0（volc.seedasr.auc）：提交任务 + 轮询结果。

异步任务接口，只接受音频 URL（Telegram 文件链接 / Discord CDN 链接均可）。
支持四川话等方言（language 留空即自动覆盖 普通话/英语/上海话/闽南话/四川话/陕西话/粤语），
可返回情绪、语种、语速等语音标签。凭据与 seed-tts-2.0 共用 DOUBAO_API_KEY。
openspeech.bytedance.com 国内直连，不走代理（trust_env=False）。
"""

import asyncio
import json
import logging
import os
import uuid

import httpx

log = logging.getLogger("cyber-gf.asr")

SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
RESOURCE_ID = "volc.seedasr.auc"

EMOTION_ZH = {"angry": "生气", "happy": "开心", "sad": "难过", "surprise": "惊讶"}
LANG_ZH = {
    "speech_dia_xina": "四川话", "speech_dia_zgyu": "陕西话", "speech_dia_cant": "粤语",
    "speech_dia_wuu": "上海话", "speech_dia_nan": "闽南话", "speech_en": "英语",
    "singing_mand": "唱歌", "singing_en": "唱歌", "singing_dia_cant": "唱歌",
}

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(trust_env=False, timeout=15)
    return _client


def _extract_hints(result: dict, top: dict) -> list[str]:
    """从识别结果里提取情绪/语种标签，转成给 LLM 看的提示。"""
    utts = result.get("utterances") or []
    emotions = [
        (u.get("additions") or {}).get("emotion")
        for u in utts if isinstance(u, dict)
    ]
    emotions = [e for e in emotions if e and e != "neutral"]
    hints: list[str] = []
    if emotions:
        dominant = max(set(emotions), key=emotions.count)
        if dominant in EMOTION_ZH:
            hints.append(f"他说这段话时语气听起来有点{EMOTION_ZH[dominant]}")
    # 语种标签可能在顶层 additions 或分句 additions
    lang = ((top.get("additions") or {}).get("lid")
            or (result.get("additions") or {}).get("lid"))
    if not lang:
        for u in utts:
            if isinstance(u, dict) and (u.get("additions") or {}).get("lid"):
                lang = u["additions"]["lid"]
                break
    if lang in LANG_ZH:
        hints.append(f"他说的是{LANG_ZH[lang]}")
    return hints


async def transcribe_url(url: str, fmt: str = "ogg", codec: str = "",
                         timeout: float = 20.0) -> tuple[str, list[str]]:
    """识别音频 URL，返回 (文本, 提示列表)。识别失败抛异常，由调用方降级。"""
    key = os.environ["DOUBAO_API_KEY"]
    task_id = uuid.uuid4().hex
    headers = {
        "X-Api-Key": key,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": task_id,
        "X-Api-Sequence": "-1",
    }
    body = {
        "audio": {"url": url, "format": fmt},
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
            "show_speech_rate": True,
            "enable_emotion_detection": True,
            "enable_lid": True,
        },
    }
    if codec:
        body["audio"]["codec"] = codec
    client = _get_client()
    r = await client.post(SUBMIT_URL, headers=headers, json=body)
    code = r.headers.get("X-Api-Status-Code")
    if code != "20000000":
        raise RuntimeError(f"seedasr submit: {code} {r.headers.get('X-Api-Message')} {r.text[:200]}")

    qheaders = {"X-Api-Key": key, "X-Api-Resource-Id": RESOURCE_ID, "X-Api-Request-Id": task_id}
    async def _poll() -> dict:
        while True:
            q = await client.post(QUERY_URL, headers=qheaders, json={})
            qcode = q.headers.get("X-Api-Status-Code")
            if qcode == "20000000":
                return q.json()
            if qcode in ("20000001", "20000002"):  # 处理中 / 排队中
                await asyncio.sleep(0.5)
                continue
            raise RuntimeError(f"seedasr query: {qcode} {q.headers.get('X-Api-Message')} {q.text[:200]}")

    data = await asyncio.wait_for(_poll(), timeout=timeout)
    log.info("seedasr raw: %s", json.dumps(data, ensure_ascii=False)[:600])
    result = data.get("result") or {}
    if isinstance(result, list):  # 防御：文档写法与实际可能有出入
        text = "".join(x.get("text", "") for x in result if isinstance(x, dict))
        result = {"utterances": result}
    else:
        text = (result.get("text") or "").strip()
        if not text:
            text = "".join(u.get("text", "") for u in result.get("utterances") or []
                           if isinstance(u, dict)).strip()
    return text, _extract_hints(result, data) if text else []


if __name__ == "__main__":
    import asyncio
    import sys

    logging.basicConfig(level=logging.INFO)
    text, hints = asyncio.run(transcribe_url(sys.argv[1], fmt=sys.argv[2] if len(sys.argv) > 2 else "wav"))
    print("TEXT:", text)
    print("HINTS:", hints)
