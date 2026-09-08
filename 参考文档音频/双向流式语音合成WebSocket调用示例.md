输入示例
```python
import asyncio
import copy
import json
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import websockets

from protocols import (
    EventType,
    MsgType,
    finish_connection,
    finish_session,
    receive_message,
    start_connection,
    start_session,
    task_request,
    wait_for_event,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
TEXT = "你好，欢迎使用语音合成服务"

async def main():
    # Connect to server.
    headers = {
        "X-Api-Key": "your_api_key",
        "X-Api-Resource-Id": "seed-tts-2.0",
        "X-Api-Connect-Id": str(uuid.uuid4()),
        "X-Control-Require-Usage-Tokens-Return": "*",
    }

    logger.info(f"Connecting to {URL}")
    websocket = await websockets.connect(
        URL, additional_headers=headers, max_size=10 * 1024 * 1024
    )
    logger.info(
        f"Connected to WebSocket server, Logid: {websocket.response.headers['x-tt-logid']}",
    )

    try:
        # Start connection.
        await start_connection(websocket)
        await wait_for_event(
            websocket, MsgType.FullServerResponse, EventType.ConnectionStarted
        )

        audio_received = False

        # Session-level parameters. Subtitle/timestamp is not enabled by default.
        base_request = {
            "req_params": {
                "speaker": "zh_female_gaolengyujie_uranus_bigtts",
                "audio_params": {
                    "format": "mp3",
                    "sample_rate": 24000,
                    # "enable_subtitle": True,
                },
            },
        }

        # Start session.
        start_session_request = copy.deepcopy(base_request)
        start_session_request["event"] = EventType.StartSession
        session_id = str(uuid.uuid4())
        await start_session(
            websocket, json.dumps(start_session_request).encode(), session_id
        )
        await wait_for_event(
            websocket, MsgType.FullServerResponse, EventType.SessionStarted
        )

        # Send characters one by one.
        async def send_chars():
            for char in TEXT:
                synthesis_request = copy.deepcopy(base_request)
                synthesis_request["event"] = EventType.TaskRequest
                synthesis_request["req_params"]["text"] = char
                await task_request(
                    websocket, json.dumps(synthesis_request).encode(), session_id
                )
                await asyncio.sleep(0.005)  # 5ms delay between characters.

            await finish_session(websocket, session_id)

        send_task = asyncio.create_task(send_chars())

        # Receive audio data.
        audio_data = bytearray()
        while True:
            msg = await receive_message(websocket)

            if msg.type == MsgType.FullServerResponse:
                if msg.event == EventType.SessionFinished:
                    break
            elif msg.type == MsgType.AudioOnlyServer:
                audio_received = True
                audio_data.extend(msg.payload)
            else:
                raise RuntimeError(f"TTS conversion failed: {msg}")

        await send_task

        if audio_data:
            audio_format = base_request["req_params"]["audio_params"]["format"]
            filename = f"bidirectional_session_0.{audio_format}"
            with open(filename, "wb") as f:
                f.write(audio_data)
            logger.info(f"Audio received: {len(audio_data)}, saved to {filename}")

        if not audio_received:
            raise RuntimeError("No audio data received")

    finally:
        # Finish connection.
        await finish_connection(websocket)
        await wait_for_event(
            websocket, MsgType.FullServerResponse, EventType.ConnectionFinished
        )
        await websocket.close()
        logger.info("Connection closed")

if __name__ == "__main__":
    asyncio.run(main())
```
输出示例
```python
[
  {
    "MsgType": "FullServerResponse",
    "EventType": "ConnectionStarted",
    "ConnectId": "926b8fad-e2ef-453a-8f47-0497a88e7140",
    "Payload": {}
  },
  {
    "MsgType": "FullServerResponse",
    "EventType": "SessionStarted",
    "SessionId": "a61af389-4042-4a77-9958-0ff2ce8af537",
    "Payload": {}
  },
  {
    "MsgType": "FullServerResponse",
    "EventType": "TTSSentenceStart",
    "SessionId": "a61af389-4042-4a77-9958-0ff2ce8af537",
    "Payload": {
      "phonemes": [],
      "text": "",
      "words": []
    }
  },
  {
    "MsgType": "AudioOnlyServer",
    "EventType": "TTSResponse",
    "SessionId": "a61af389-4042-4a77-9958-0ff2ce8af537",
    "PayloadSize": 2349
  },
  {
    "MsgType": "AudioOnlyServer",
    "EventType": "TTSResponse",
    "SessionId": "a61af389-4042-4a77-9958-0ff2ce8af537",
    "PayloadSize": 3264
  },
  {
    "MsgType": "FullServerResponse",
    "EventType": "TTSSentenceEnd",
    "SessionId": "a61af389-4042-4a77-9958-0ff2ce8af537",
    "Payload": {
      "phonemes": [],
      "text": "你好，欢迎使用语音合成服务",
      "words": []
    }
  },
  {
    "MsgType": "FullServerResponse",
    "EventType": "SessionFinished",
    "SessionId": "a61af389-4042-4a77-9958-0ff2ce8af537",
    "Payload": {
      "usage": {
        "text_words": 13
      }
    }
  },
  {
    "MsgType": "FullServerResponse",
    "EventType": "ConnectionFinished",
    "ConnectId": "926b8fad-e2ef-453a-8f47-0497a88e7140",
    "Payload": {}
  }
]
```