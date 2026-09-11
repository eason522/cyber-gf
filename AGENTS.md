# cyber-gf

赛博女友「暖暖」（温以暖）：Telegram / Discord 双平台的陪伴型 AI agent，台湾妹人设，文字+语音双通道，有长期记忆和主动关心能力。

## 平台切换

`config.env` 里 `BOT_PLATFORM=telegram|discord` 二选一（默认 telegram），改完重启 `run.sh` 生效。Discord 模式需要填 `DISCORD_TOKEN`，且开发者门户里要开 **Message Content Intent**。心跳只在最后聊过的平台发（contact.json 带 platform 字段）。

## 运行环境

- 服务器：2核4G 低配，无 sudo。Python 3.14（系统无 ensurepip，建虚拟环境用 `python3 -m virtualenv`，不要用 `python3 -m venv`）
- ffmpeg 是项目根目录下的静态二进制（`./ffmpeg`），不是系统安装
- 网络走代理；本地服务（OpenViking）必须走 `NO_PROXY`，否则慢好几秒；**Discord 的 aiohttp 不读代理环境变量**，platform_discord.py 里已显式传 `proxy=`
- 无 GPU；ASR 已改走云端（方舟音频理解，`ASR_MODEL` 控制，ogg 需先转 mp3——`input_audio` 不吃 ogg），本地 whisper 只做兜底
- **主模型 doubao-seed-character 不支持音频输入**（实测 400 "audio input is not supported"），别再把用户语音直接喂给它；seed-tts-2.0 也只进文本（纯 TTS 接口）
- 无 GPU，本地 ASR 兜底用 faster-whisper base 跑 CPU（平常走云端）

## 启动 / 停止

已纳入 systemd 用户级服务（已 enable-linger，开机自启 + 故障 5 秒自动重启，代理环境在 unit 里显式注入）：

```bash
systemctl --user start|stop|restart cyber-gf    # 启停（pkill 会触发自动重启，别再用了）
systemctl --user status cyber-gf                # 状态
tail -f logs/cyber-gf.log                       # 日志：按天轮转保留 14 天，重启不丢
```

OpenViking 同样纳入了 `openviking.service`（开机自启 + 自动重启），cyber-gf 配置了 `After=openviking.service` 保证启动顺序。运维命令详见 `~/openviking/INTEGRATION.md`。OpenViking 挂了不影响聊天，记忆自动降级。

## 架构

cordis 风格插件架构：core 只提供服务容器/事件总线/生命周期，业务能力全部是插件。

```
core/app.py 入口：Config.from_env() → root Context → 按依赖序加载插件树 → emit "ready"
  → 平台插件在此刻启动轮询/连接 → 阻塞等待，退出时 ctx.dispose() 逆序回收
core/context.py：Context = provide/inject（沿 parent 链查找）+ on/emit 事件总线
  + on_dispose/create_task/load_plugin/dispose（插件与后台任务统一回收）+ fork（千人千面预留）
插件约定：plugins/foo.py 导出 apply(ctx)（可选 dispose(ctx)），顶部声明 requires/provides
插件树开关：PLUGINS_DISABLED / PLUGINS_EXTRA（逗号分隔模块短名；剔除被依赖的插件会在
  启动时报错并指明缺失的服务）
```

消息链路（平台无关核心在 chat 插件）：

```
消息(文字/语音) → platform_telegram / platform_discord
  ├─ asr.transcribe     语音三级降级：seedasr.auc 录音识别2.0(URL直传,方言/情绪标签) → 方舟 doubao-seed-2-0-mini 音频理解(本地文件base64) → 本地 whisper
  └─ chat.process(user_id, text, ui)   ui 协议：send_text / send_voice(ogg_path) / status(kind) / pulse(kind)
       ├─ memory.recall      OpenViking 语义检索（peer 空间优先，30s 超时降级；期间 Discord 状态显示"正在回忆…"）
       ├─ depth.judge        深度路由：闲聊 minimal(关思考) / 走心 high(开思考)
       ├─ chat.stream        seed-character 流式 + 工具调用循环（tools：时间/web_search/web_read/文件读写 → reply 收尾，emotion 先行）
       │                     事件流：("status")/("emotion")/("voice")/("sentence")/("done")；emit message.received / reply.done
       └─ tts（seed-tts-2.0 → edge-tts 降级）  ≤350字(或悄悄话)整段一次合成，超长才按句并行；语音先发文字后到
heartbeat 插件：随机间隔主动关心（HEARTBEAT_MINUTES 为中枢、HEARTBEAT_JITTER 默认 ±60% 抖动，
  拟人化不做闹钟；NO_REPLY 契约；可写小本本 tinynote/；北京时间判定；
  综合判断素材全部注入 system：OV/本地记忆 + 随身记忆 + 兴趣手账 + 小本本近况 + 闺蜜与猫近况 + 多巴胺心情底色；
  工具含 web_search/web_read（可查他那边 HOME_LOCATION 的实时天气嘘寒问暖，查不查由她自己决定）；
  发消息走 ctx.inject("platform").send(chat_id, text, ogg)，无平台服务时记 warning 跳过）
surf 插件：每 3 小时（SURF_MINUTES）她自己上网刷八卦/新闻，方向由兴趣手账引导（interests.get()），
  刷到感兴趣的用 web_read 点进原文细读，
  再把发现连当时的心情/想跟他怎么讲一起写进 tinynote/（日记式，不记流水账）；聊天时小本本近况
  和兴趣手账都注入 system（persona.tinynote_block / chat 里 ctx.has("interests") 可选消费），
  她会主动分享；web_search 时 Discord 状态显示「正在刷小红书…」
每 8 轮对话 commit 到 OpenViking 自动提炼长期记忆（memory.record_turn，OV 挂自动降级本地提炼）
记忆分两层：soul/MEMORY.md（memory_md 插件，对话停 2 分钟后把这波对话批量喂给主模型更新一次（debounce），高频/重要/他明确要求记的，
  每条消息注入 system）是热层；OpenViking 是冷层（复杂/低频/远期，按需语义检索）。
  主动记忆（他说"记住…"）由 memory_md 的更新提示词保证优先进「重要约定与嘱托」
```

## 加一个新插件

1. 写 `plugins/foo.py`：顶部 `requires = ["chat", ...]`（服务名），导出 `def apply(ctx)`，在 apply 里 `ctx.provide("foo", svc)` 或 `ctx.create_task(后台循环)`；可选 `def dispose(ctx)`。
2. 挂载：加进 `core/app.py` 的 `DEFAULT_PLUGINS`（注意依赖序），或临时用 `PLUGINS_EXTRA=foo`。
3. 消费其他服务用 `ctx.inject("名字")`；跨插件通信用 `ctx.on/ctx.emit`；后台任务一律 `ctx.create_task`（dispose 自动取消）。

## 文件职责（改哪里）

| 文件 | 职责 |
|------|------|
| `soul/IDENTITY.md` | 她是谁：名字、存在形式、vibe、生日 |
| `soul/SOUL.md` | 性格、说话风格、小世界、边界。改人设只动这里，每条消息实时加载，改完不用重启 |
| `soul/USER.md` | 用户画像（指令式条目，带 observed/status 元数据） |
| `soul/MEMORY.md` | 随身记忆（热层）：高频/重要/他明确要求记的事，每条消息注入 system。memory_md 插件实时维护，**已 gitignore，不要手改**（手改会被下一轮对话覆盖） |
| `soul/interests.md` | 兴趣手账：interests 插件定期维护，已 gitignore |
| `soul/BESTIE.md` | 闺蜜「林小夏」的完整人设（social 插件用），改人设只动这里，随剧集实时加载 |
| `core/app.py` | 入口：日志配置（按天轮转 14 天）、插件树解析（PLUGINS_DISABLED/EXTRA + 依赖静态校验）、按序加载、emit ready、阻塞与干净退出 |
| `core/context.py` | Context：服务注册/注入、事件总线、插件生命周期、fork |
| `core/config.py` | Config.from_env()：集中全部 env key（34 个），默认值与旧代码逐字一致 |
| `plugins/llm.py` | 服务 llm：主模型 AsyncOpenAI 客户端单例 |
| `plugins/persona.py` | 服务 persona：soul/*.md 系统提示（委托 soul.py）+ tinynote 近况块 |
| `plugins/sessions.py` | 服务 sessions：会话内存态、`data/<uid>.json` 持久化（委托 memory.py）、contact.json 读写 |
| `plugins/memory_local.py` | 服务 memory（本地兜底提供者）：定期 LLM 提炼 |
| `plugins/memory_md.py` | 服务 memory_md：随身记忆（soul/MEMORY.md）实时维护。note() 每轮进缓冲 + 重置计时器（UPDATE_DELAY=120s debounce，一波对话只更新一次），失败留旧文件、缓冲保留下次再试，on_dispose 退出前强制落盘；get() 供 chat 注入 system |
| `plugins/scheduler.py` | 服务 scheduler：计划任务/提醒。注册 schedule_task/list_scheduled/cancel_scheduled 三个工具（meta 带 user_id/chat_id），任务持久化 data/schedule.json，20s 轮询到期执行；执行走心跳同款链路（persona+随身记忆 → 强制 reply → TTS → platform.send），支持一次性（at）/每天（daily）/多少分钟后（in_minutes） |
| `plugins/dopamine.py` | 服务 dopamine：赛博多巴胺系统。模拟人体机制——昼夜节律紧张性基线 + 剥夺效应（他太久没来基线下压）、RPE 相位脉冲（他的消息是奖赏，久别惊喜冲高、连珠炮习惯化打折、聊天情绪余韵微调）、40 分钟半衰期指数衰减。监听 message.received / reply.done（带 emotion），social 插件可 stimulate()。mood() 输出喜怒哀乐档位，prompt_block() 注入聊天/心跳 system 当心情底色。状态 data/dopamine.json |
| `plugins/social.py` | 服务 social：闺蜜+宠物系统。闺蜜「林小夏」（人设 soul/BESTIE.md、记忆 data/bestie_memory.md 每集后由主模型维护、亲密度/冷战状态 data/social.json）+ 布偶猫「麻糬」（饥饿/精力随时间模拟）。SOCIAL_MINUTES 中枢 60%~150% 随机间隔驱动一集"小剧场"（串门/逛街/遛猫/聊天/分享秘密/偶尔小矛盾冷战再和好，冷战最多僵持 2 集强制转机），模型用主模型 doubao-seed-character；日记写进 tinynote/ 自动进聊天上下文，mood_delta 刺激多巴胺；recent_block() 注入聊天/心跳 system |
| `plugins/memory_openviking.py` | 服务 memory（OpenViking 提供者，override 本地）：recall/record_turn，OV 挂自动降级 |
| `plugins/asr.py` | 服务 asr：语音转文字三级降级链（委托 asr_seed.py，whisper 惰性单例兜底） |
| `plugins/tts.py` | 服务 tts：seed-tts-2.0 → edge-tts 降级（委托 tts_seed.py）、EMOTIONS/音色映射、safe_ogg |
| `plugins/tools_builtin.py` | 服务 tools：工具注册表 ToolRegistry（defs/register/run），内置 6 个工具：时间 / web_search / web_read（点进链接细读正文，Tavily extract 主、直连剥 HTML 兜底）/ list_directory / read_file / write_file。文件操作限制在 /home/eason 下，拒绝 config.env/.ssh/.git 等敏感路径；工具出错只返回错误字符串 |
| `plugins/depth_router.py` | 服务 depth：judge_depth（硅基流动 Qwen3-8B 关思考 + DEEP_KEYWORDS 快捷路径） |
| `plugins/interests.py` | 服务 interests：兴趣手账（`soul/interests.md`，已 gitignore——系统反复重写不进仓库），定期（INTERESTS_HOURS，默认 6h）用主模型综合长期记忆+小本本+近期对话重写；固定四分区（长期热爱/最近上头/冷却中/想探索的新领域）+ 小步更新规则防兴趣过拟合 |
| `plugins/chat.py` | 服务 chat：核心流水线。stream() 事件流 + REPLY_TOOL schema + 工具调用循环（最多4轮、末轮强制 reply）；process() 统一 TG/Discord 的消息派发（攒句/整段≤350字/超长分句并行/语音先发文字后到） |
| `plugins/heartbeat.py` | 后台任务：随机间隔心跳（HEARTBEAT_MINUTES 中枢 ±HEARTBEAT_JITTER 抖动，NO_REPLY 契约、北京时间沉默判定）；system 注入 OV/本地记忆 + 随身记忆 + 兴趣手账 + 小本本近况 + social 生活近况 + 多巴胺心情底色综合判断发不发/发什么；工具含 web_search/web_read（可查 HOME_LOCATION 天气嘘寒问暖），platform 服务延迟 inject |
| `plugins/surf.py` | 后台任务：3 小时冲浪循环，写 tinynote |
| `plugins/platform_telegram.py` | 服务 platform（TG）：ptb 手动生命周期（initialize/start/updater.start_polling，"ready" 事件触发启动）；文字/语音入口；TelegramUI（pulse→RECORD_VOICE，status no-op） |
| `plugins/platform_discord.py` | 服务 platform（Discord）：client.start(token) 手动生命周期；私信或 @机器人 触发；语音 ogg 附件收发；DiscordUI（status→"正在回忆…/正在刷小红书…"，pulse→typing） |
| `tinynote/` | 暖暖的私人小本本（日记/涂鸦），她自己用 write_file 写、read_file 翻看，SOUL.md 里有设定；已 gitignore（她的私人内容不进仓库） |
| `ov_memory.py` | OpenViking 封装库：recall / record_turn(commit) / healthy |
| `memory.py` | 本地兜底记忆库（OV 不可用时）+ 历史持久化 |
| `soul.py` | 人设加载库（soul/*.md → system prompt） |
| `tts_seed.py` + `tts_protocols.py` | 豆包 seed-tts-2.0 WebSocket 双向流式协议实现库 |
| `asr_seed.py` | 豆包录音文件识别 2.0（volc.seedasr.auc）：提交+轮询，吃音频 URL（TG 文件链接/Discord CDN），返回文本+情绪/方言提示。**需在语音控制台开通该服务**，否则报 45000030；`ASR_SEED=0` 可关闭。openspeech 直连不走代理 |
| `config.env` | 所有密钥和开关（已 gitignore，**绝不提交**） |
| `参考文档音频/` | 方舟官方文档（thinking / seed-tts 协议 / 语音指令与标签 / seedasr 录音识别）+ 官网效果参考 wav（对照测试用） |

## 关键约定与坑

- **密钥**：只放 `config.env` 和 `~/.openviking/ov.conf`，都在 gitignore。提交前确认 `git status` 不含 config.env。
- **情绪系统**：LLM 通过 `reply` 工具调用交出 `{emotion, voice?, text}`，emotion 字段在 schema 里排前面（流式时先到）。情绪表达靠**语音指令**，走 `additions.context_texts`（官方字段是 **additions JSON 字符串里的 `context_texts`**，放 `req_params` 顶层会被静默忽略；不计费、不朗读）。指令必须写成纯"声音描写"（`用……的语气/哭腔说`），对话式互动指令（"撩撩我""你得跟我互怼"）实测失效；**context_texts 里只放一条纯指令**——混入引用上文/多条叠加/section_id 都会稀释效果（隔离实验实测）。`[#指令]` 内联和 `{{ }}` 句内标签 API 都不认识、会被当台词念出来，均已废弃。生气/哭腔/纯气声悄悄话三条指令措辞经过官网参考音频对照验证（`test_tts_official.py`）。**音色分工**：默认小和 `zh_female_xiaohe_uranus_bigtts`（自带台湾腔）；voice 提示命中"耳语/悄悄话/气声/asmr"时切 vv `zh_female_vv_uranus_bigtts` + 纯气声指令（小和情绪表现力实测弱于 vv）。**合成策略**（`WHOLE_TTS_MAX=350`）：回复 ≤350 字或悄悄话场景整段一次合成（分句并行会让气声/情绪逐句漂移，且整段能保住省略号等情绪细节），超长回复才按句并行抢首音速度。`DOUBAO_CONTEXT` 留空。
- **括号舞台指示**：模型偶发违反人设写「（笑到声音都在颤）」这类动作/神态描写。chat 插件的 `_strip_stage` 会把它们从文字层剥掉（不进历史/不给用户看/不给 TTS），并用括号里的情绪词做情绪升级线索——emotion=平静但括号在笑/哭，语音按括号暗示的情绪合成（逐句生效）。REPLY_TOOL 的 emotion/text 描述里也加了硬约束，源头减少这种行为。
- **深度路由**：裁判模型用硅基流动 Qwen3-8B（关思考）。**不要用 seed-character 当裁判**——角色扮演模型做不了元分类，实测全判 CHAT。`reasoning_effort: high` 必须同时显式 `thinking: enabled`，否则 400。
- **seed-tts**：文本放 `req_params.text` 经 TaskRequest 事件发送；payload 必须带 `user`/`event` 字段。
- **OV 繁忙**：commit 提炼会占住 OV 服务器导致 recall 超时，这是预期行为（降级跳过，不阻塞回复）；频繁出现再考虑调队列。
- 测试产生的 `data/<假uid>.json` 和 OV 里的测试记忆要及时清掉，别污染她的记忆。
- pkill/pgrep 匹配进程名时用 `[c]ore.app` 这种写法，防止模式匹配到执行命令自身的 shell。

## Git 工作流

- 远端：git@github-cyber-gf:eason522/cyber-gf.git（deploy key: `~/.ssh/cyber_gf_deploy`，Host 别名 github-cyber-gf 在 `~/.ssh/config`）
- **每完成一次开发就 commit + push**（用户硬性要求），commit message 用中文写清楚改动
- 提交署名：`git -c user.name="eason" -c user.email="eason@cyber-gf.local"`
