信息已经充分，下面给出完整的整理回答。

---

## Seed-TTS 2.0（豆包语音合成 2.0）语音指令与标签使用说明

豆包语音合成 2.0（Doubao-Seed-TTS 2.0）是火山引擎新一代语音合成大模型，相比旧版本新增**语音指令、引用上文、语音标签**三大核心能力，支持更精细的情感控制、上下文语境承接、肢体/表情细节描述。[豆包语音合成2.0-能力介绍](https://www.volcengine.com/docs/6561/1871062)

---

### 一、接入前提

调用 API 时请求头需携带 2.0 专属资源 ID：

| 参数名 | 示例值 | 说明 |
|---|---|---|
| `X-Api-App-Id` | `123456789` | 控制台获取的 APP ID |
| `X-Api-Access-Key` | `your-access-key` | 控制台获取的 Access Token |
| `X-Api-Resource-Id` | `seed-tts-2.0` | 2.0 字符版专属资源 ID |
| （声音复刻表现力增强版） | `seed-tts-2.0-expressive` | 声音复刻 2.0 表现力增强版 |

---

### 二、语音指令（context_texts）——控制整段音频表现

**语音指令**是合成时的辅助输入参数，通过 `context_texts` 字段传入（2.0 模型专属），用于主动指定整段音频的合成效果，支持自然语言描述。指令内容**不会被朗读**，会覆盖接口直接设置的对应参数。([WebSocket 双向流式API-V3](https://www.volcengine.com/docs/6561/1329505?lang=zh))

#### 1. 支持的控制维度

- **情绪**：悲伤、生气、开心、犹豫、温柔、绝望、崩溃哭腔等
- **方言**：四川话、北京话等
- **语气**：撒娇、暧昧、吵架、夹子音、ASMR 悄悄话、试探、质问等
- **语速**：快慢调节（如"说慢一点"）
- **音量 / 音调**：大小、高低调节（如"声音小一点""音调高一点"）
- **复杂复合情感**：可叠加多重描述，如"用颤抖沙哑、带着崩溃与绝望的哭腔，夹杂着质问与心碎的语气说"[语音指令与标签](https://docs.volcengine.com/docs/6561/1871062)

#### 2. API 调用示例（WebSocket / HTTP）

`context_texts` 放在 `req_params.additions` 中，`additions` 是**字符串类型**（需把 JSON 对象序列化后传入）：

```json
{
  "req_params": {
    "text": "既然这样，就不要怪我不客气。",
    "speaker": "zh_female_vv_uranus_bigtts",
    "additions": "{\"context_texts\":[\"请用非常生气的语气朗读\"]}",
    "audio_params": {
      "format": "mp3",
      "sample_rate": 24000
    }
  }
}
```

Python Demo 运行示例：
```bash
python .\examples\volcengine\bidirection.py \
  --appid 797***** \
  --access_token aEn8Bx81AB****** \
  --voice_type zh_female_vv_uranus_bigtts \
  --resource_id seed-tts-2.0 \
  --section_id aaabbbccc \
  --text "行吧行吧 都是我的错" \
  --context_texts "请用非常生气语气朗读"
```
([WebSocket 双向流式API-V3](https://www.volcengine.com/docs/6561/1329505?lang=zh))

#### 3. 示例库（精选）

| 指令示例 | 合成文本示例 | 效果 |
|---|---|---|
| `[#你得跟我互怼！就是跟我用吵架的语气对话]` | 那你另请高明啊，你找我干嘛！我告诉你，你也不是什么好东西！ | 吵架 |
| `[#用asmr的语气来试试撩撩我]` | 当然可以啦，每次听到你的声音，我都觉得心里暖暖的。 | 暧昧/悄悄话 |
| `[#用试探性的犹豫、带点害羞又藏着温柔期待的语气说]` | 哎，能……能一起撑伞不？这雨突然就大了！ | 复杂情感 |
| `[#用颤抖沙哑、带着崩溃与绝望的哭腔，夹杂着质问与心碎的语气说]` | 我逆转时空九十九次救你，你却次次死于同一支暗箭…… | 崩溃哭腔 |

[语音指令与标签](https://docs.volcengine.com/docs/6561/1871062)

---

### 三、引用上文（section_id）——让模型承接对话语境

**引用上文**是指输入合成文本的上文（只引用不合成），模型会理解并承接语境的情绪进行合成，适用于多轮对话场景。[语音指令与标签](https://docs.volcengine.com/docs/6561/1871062)

- 通过 `additions.section_id` 字段传入会话分段标识，相同 `section_id` 下模型会关联历史上下文
- 示例场景：先合成"行吧行吧 都是我的错"（生气语气），下一轮带相同 `section_id` 发送"你为什么这样，太过分了"，模型会延续上轮生气语境

```bash
python .\examples\volcengine\bidirection.py \
  --appid 797****** --access_token aEn8Bx81AB****** \
  --voice_type zh_female_vv_uranus_bigtts \
  --resource_id seed-tts-2.0 \
  --section_id aaabbbccc \
  --text "你为什么这样，太过分了"
```
([WebSocket 双向流式API-V3](https://www.volcengine.com/docs/6561/1329505?lang=zh))

---

### 四、语音标签（内嵌文本标签）——句粒度精细控制

语音标签是在**合成文本内部**嵌入的控制标记，可针对单句/局部内容调整语速、情感等，覆盖接口直接设置的对应参数。根据模型不同有两种标签语法：

#### 1. 语音合成大模型 2.0（官方 TTS 2.0 音色）：`{{ }}` 双大括号标签

**语法格式**（注意末尾 `}}` 前**必须留一个空格**，否则会解析失败）：

```
{{"additions":{"context_texts":["自然语言描述"]} }}
```

**标签位置与生效范围**：
- 标签须置于所要作用句子的前面
- 标签作用于**所在的整个子句单元及之后所有子句**，直到遇到新标签
- 为优化首帧速度（TTFT），**回复第一句话建议不携带标签**，从第二句开始按需插入[通过指令控制语音表现（如情绪）](https://docs.volcengine.com/docs/6348/2139328)

**示例**（第一句中性 → 第二句兴奋期待 → 第三句温馨亲切）：
```
没问题，我已经为你预订好了周六去海边的行程。{{"additions":{"context_texts":["语气变得非常兴奋，充满期待"]} }} 那里的沙滩非常漂亮，你一定会玩得非常开心的！{{"additions":{"context_texts":["语速放慢，语气变得温馨亲切"]} }} 不过记得带上防晒霜和遮阳帽，海边的阳光可能会有些强烈。
```

在实时音视频（RTC）双流式场景下，通过 `StartVoiceChat` 开启标签解析：
```json
"TTSConfig": {
  "Provider": "volcano_bidirection",
  "Context": {
    "TagParse": true,            // 开启标签解析
    "QuoteUserQuestion": true    // 把用户问题作为上下文（仅 2.0 支持）
  }
}
```
也可通过 `UpdateVoiceChat` 接口 + `Command=SetTTSContext` 为下一轮**动态注入**全局标签：
```json
{
  "Command": "SetTTSContext",
  "Message": "{\"Tag\":{\"additions\":{\"context_texts\":[\"语气欢乐一点\"]}}}"
}
```
[通过指令控制语音表现（如情绪）](https://docs.volcengine.com/docs/6348/2139328)

#### 2. 声音复刻 2.0 表现力增强版（seed-tts-2.0-expressive）：`<cot>` 标签

需在 `additions` 中开启 `use_tag_parser: true`，并使用 `model=seed-tts-2.0-expressive`，然后在文本中用 `<cot>` 标签包裹指定内容：

```
<cot text=风格描述>文本内容</cot>
```

- **生效范围**：仅限于 `<cot>` 与 `</cot>` 之间的文字（局部生效）
- **长度限制**：包含标签在内单句不可超过 64 个字符
- 标签**不会被自动过滤**，会出现在字幕里，如需纯净字幕业务端需自行移除
([通过指令控制语音表现（如情绪）](https://docs.volcengine.com/docs/6348/2139328))

**示例**：
```
<cot text=急促难耐>工作占据了生活的绝大部分</cot>，只有去做自己认为伟大的工作，才能获得满足感。
```
([HTTP单向流式语音合成](https://www.volcengine.com/docs/6561/2528925?lang=zh))

---

### 五、生效优先级（RTC 双流式场景）

系统按以下优先级应用指令，高优先级覆盖低优先级：[通过指令控制语音表现（如情绪）](https://docs.volcengine.com/docs/6348/2139328)

1. `UpdateVoiceChat` 接口通过 `SetTTSContext` 发送的指令（最高）
2. LLM 在文本中自动生成的嵌入式 `{{ }}` 标签
3. `QuoteUserQuestion`（将用户问题作为上下文传递）

---

### 六、使用注意事项

1. **语音指令/标签属于模型能力，并非 100% 生效**，建议业务上线前充分测试效果是否符合预期。
2. **首句避免带标签**：为获得最优首帧音频生成时间（TTFT），首句用纯文本，第二句起再加标签。
3. **标签粒度建议以完整句子为单位**，不要在句子中间插入，以保证韵律连贯。
4. `additions` 字段是**字符串**类型，传入时要把 JSON 对象序列化成字符串。
5. 双大括号 `{{ }}` 标签结尾 `}}` **前必须留一个空格**，否则会解析失败。
6. 错误码 `45000292`（`quota exceeded for types: text_words_lifetime`）表示试用版额度用尽，需在控制台开通正式版或创建新应用使用新的免费额度。