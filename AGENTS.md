# cyber-gf

赛博女友「暖暖」（温以暖）：Telegram / Discord 双平台的陪伴型 AI agent，台湾妹人设，文字+语音双通道，有长期记忆和主动关心能力。

## 平台切换

`config.env` 里 `BOT_PLATFORM=telegram|discord` 二选一（默认 telegram），改完重启 `run.sh` 生效。Discord 模式需要填 `DISCORD_TOKEN`，且开发者门户里要开 **Message Content Intent**。心跳只在最后聊过的平台发（contact.json 带 platform 字段）。

## 运行环境

- 服务器：2核4G 低配，无 sudo。Python 3.14（系统无 ensurepip，建虚拟环境用 `python3 -m virtualenv`，不要用 `python3 -m venv`）
- ffmpeg 是项目根目录下的静态二进制（`./ffmpeg`），不是系统安装
- 网络走代理；本地服务（OpenViking）必须走 `NO_PROXY`，否则慢好几秒；**Discord 的 aiohttp 不读代理环境变量**，discord_bot.py 里已显式传 `proxy=`
- 无 GPU；ASR 已改走云端（方舟音频理解，`ASR_MODEL` 控制，ogg 需先转 mp3——`input_audio` 不吃 ogg），本地 whisper 只做兜底
- **主模型 doubao-seed-character 不支持音频输入**（实测 400 "audio input is not supported"），别再把用户语音直接喂给它；seed-tts-2.0 也只进文本（纯 TTS 接口）
- 无 GPU，本地 ASR 兜底用 faster-whisper base 跑 CPU（平常走云端）

## 启动 / 停止

```bash
cd ~/cyber-gf && nohup ./run.sh > bot.log 2>&1 &   # 启动
pkill -f "[b]ot.py"                                # 停止（必须带 [b]，否则误杀自己的 shell）
```

依赖的外部服务：OpenViking（`~/openviking`，`127.0.0.1:1933`，运维命令见 `~/openviking/INTEGRATION.md`）。OpenViking 挂了不影响聊天，记忆自动降级。

## 架构

```
消息(文字/语音) → bot.py
  ├─ asr_transcribe     语音入口三级降级：seedasr.auc 录音识别2.0(URL直传,方言/情绪标签) → 方舟 doubao-seed-2-0-mini 音频理解(本地文件base64) → 本地 whisper
  ├─ ov_memory.recall   OpenViking 语义检索（peer 空间优先，8s 超时降级）
  ├─ judge_depth        深度路由：闲聊 minimal(关思考) / 走心 high(开思考)
  ├─ chat_stream        seed-character 流式 + reply 工具调用（emotion 先行）
  └─ seed-tts-2.0       按句并行合成，情绪→语气/语速/音调，语音先发文字后到
每 8 轮对话 commit 到 OpenViking 自动提炼长期记忆；心跳每 45 分钟主动关心（NO_REPLY 契约）
```

## 文件职责（改哪里）

| 文件 | 职责 |
|------|------|
| `soul/IDENTITY.md` | 她是谁：名字、存在形式、vibe、生日 |
| `soul/SOUL.md` | 性格、说话风格、小世界、边界。改人设只动这里，每条消息实时加载，改完不用重启 |
| `soul/USER.md` | 用户画像（指令式条目，带 observed/status 元数据） |
| `bot.py` | 核心流水线 + Telegram 接入：平台分发（main→run_telegram/discord_bot.run）、深度路由、流式编排、TTS 参数映射（EMOTIONS 表）、心跳（heartbeat_loop 接收平台 send 回调） |
| `discord_bot.py` | Discord 接入层：私信或 @机器人 触发，复用 bot.py 的 chat_stream/TTS/心跳；语音以 ogg 音频附件发送（Discord 机器人不能发原生语音条），收语音靠音频附件 |
| `ov_memory.py` | OpenViking 封装：recall / record_turn(commit) / healthy |
| `memory.py` | 本地兜底记忆（OV 不可用时）+ 历史持久化（`data/<uid>.json`） |
| `tts_seed.py` + `tts_protocols.py` | 豆包 seed-tts-2.0 WebSocket 双向流式协议实现 |
| `asr_seed.py` | 豆包录音文件识别 2.0（volc.seedasr.auc）：提交+轮询，吃音频 URL（TG 文件链接/Discord CDN），返回文本+情绪/方言提示。**需在语音控制台开通该服务**，否则报 45000030；`ASR_SEED=0` 可关闭。openspeech 直连不走代理 |
| `config.env` | 所有密钥和开关（已 gitignore，**绝不提交**） |
| `思考设置.md` / `调用指南.md` / `录音文件识别-*.md` | 方舟 thinking 文档 / seed-tts 协议文档 / seedasr 录音识别文档（参考用） |

## 关键约定与坑

- **密钥**：只放 `config.env` 和 `~/.openviking/ov.conf`，都在 gitignore。提交前确认 `git status` 不含 config.env。
- **情绪系统**：LLM 通过 `reply` 工具调用交出 `{emotion, voice?, text}`，emotion 字段在 schema 里排前面（流式时先到）。情绪表达**全靠语音指令**（context_texts 自然语言）驱动，不用 pitch/speech_rate/loudness 外部参数。context_texts 组装顺序（`tts_params_for`）：引用上文（用户原话，只引用不合成）→ EMOTIONS 情绪指令 → voice 演绎指令。音色用 `zh_female_vv_uranus_bigtts`；`DOUBAO_CONTEXT` 留空，**不要加"台湾腔"之类的音色基底设定**，会干扰模型情绪判断。**关键：必须用 `seed-tts-2.0-expressive` 模型**（DOUBAO_TTS_MODEL），默认的 standard 版会静默丢弃全部语音指令/标签（[官方文档](https://www.volcengine.com/docs/6561/1329505)），之前"指令无效/语境稀释"的排查全是这个引起的。
- **深度路由**：裁判模型用硅基流动 Qwen3-8B（关思考）。**不要用 seed-character 当裁判**——角色扮演模型做不了元分类，实测全判 CHAT。`reasoning_effort: high` 必须同时显式 `thinking: enabled`，否则 400。
- **seed-tts**：文本放 `req_params.text` 经 TaskRequest 事件发送；payload 必须带 `user`/`event` 字段。
- **OV 繁忙**：commit 提炼会占住 OV 服务器导致 recall 超时，这是预期行为（降级跳过，不阻塞回复）；频繁出现再考虑调队列。
- 测试产生的 `data/<假uid>.json` 和 OV 里的测试记忆要及时清掉，别污染她的记忆。
- pkill/pgrep 匹配进程名时用 `[b]ot.py` 这种写法，防止模式匹配到执行命令自身的 shell。

## Git 工作流

- 远端：git@github-cyber-gf:eason522/cyber-gf.git（deploy key: `~/.ssh/cyber_gf_deploy`，Host 别名 github-cyber-gf 在 `~/.ssh/config`）
- **每完成一次开发就 commit + push**（用户硬性要求），commit message 用中文写清楚改动
- 提交署名：`git -c user.name="eason" -c user.email="eason@cyber-gf.local"`
