# CLAUDE.md — 全双工语音助手项目规范

> 本文件是项目的唯一事实来源（Source of Truth）。任何架构、目录结构、开发顺序的调整，
> 先改本文件，再改代码。代码与本文件冲突时，以本文件为准，并立即修正代码或修正文档。

---

## 1. 项目愿景

**一句话描述**：对着麦克风说话，就能控制 Windows 电脑——打开软件、查系统状态、操作文件、执行自动化任务，不需要按任何键。

**命名说明**：项目是真正的全双工——播报期间用户说话，能立刻打断正在播放的 TTS，转去处理新的语音输入。这是核心需求，架构从一开始就按双通道并发设计。

### 架构图（文字版）

```
┌─────────────────────────────────────────────────────────────┐
│                    主事件循环 (asyncio, 双通道并发)              │
└─────────────────────────────────────────────────────────────┘

  【监听通道】始终运行                    【播报通道】按需运行
       │                                        │
       ▼                                        ▼
┌──────────────┐                        ┌──────────────┐
│  音频采集模块  │                        │  音频播放模块  │
│ (sounddevice  │                        │ (sounddevice, │
│  持续采集)     │                        │  可随时中断)   │
└──────┬───────┘                        └──────▲───────┘
       │                                        │
       ▼                                        │
┌──────────────┐                        ┌──────────────┐
│  VAD 检测     │──播报中检测到人声──────▶│ 状态协调器：   │
│ (silero-vad)  │      判定为"打断"       │ 收到打断信号   │
└──────┬───────┘                        │ → stop 播放流  │
       │ 人声结束                        │ → 丢弃剩余队列 │
       ▼                                └──────────────┘
┌──────────────┐
│   ASR 模块    │
│(faster-whisper)│
└──────┬───────┘
       │ 识别出文本
       ▼
┌──────────────┐
│  意图解析模块  │
│  (LLM 调用)   │
└──────┬───────┘
       │ 解析为结构化指令
       ▼
┌──────────────┐
│  执行模块      │
│ (白名单校验    │
│  + 实际执行)   │
└──────┬───────┘
       │ 执行结果
       ▼
┌──────────────┐
│  TTS 合成      │──────▶ 进入【播报通道】播放
│ (Edge TTS)    │
└──────────────┘
```

**关键约束**：监听通道不因播报而暂停，两条通道并发运行，用状态协调器传递打断信号。打断只能取消"播报"和"等待语音确认"这两种交互状态，**不能取消已经开始执行的系统操作**（比如文件删除命令已经发出，打断不会撤回，见第 6 节）。播报中的打断判定不追求完美的声学回声消除（AEC），MVP 用简化能量/音色阈值判断，误判率高于工业级方案，这是明确的取舍，见第 4 节和第 8 节。

**耳机/外放两套阈值**：打断判定的可靠度和输出设备强相关（耳机场景基本无播报声音泄漏进麦克风，外放场景泄漏严重）。启动时自动检测当前输出设备类型（设备名关键词匹配），选用对应阈值；用户可在 `config.yaml` 里手动覆盖检测结果。自动检测不保证 100% 准确，这是有意为之的取舍，见第 8 节。

---

## 2. 技术栈清单

| 模块 | 选型 | 说明 |
|---|---|---|
| ASR | faster-whisper | CPU/GPU 均可，Windows 友好 |
| ASR 后处理 | opencc-python-reimplemented | 繁→简字符转换（t2s 模式）。Whisper 中文模型识别大陆普通话时有时输出繁体字符（如「打開瀏覽器」），用 opencc t2s 做字符级 1:1 映射还原简体。**选 t2s 而不是 tw2sp**：tw2sp 做词组级替换会把大陆词「退出程序」错误转成「退出进程」，t2s 只改字形不改词汇，对大陆普通话零副作用。 |
| TTS | edge-tts | 免费，中文自然，需联网（微软云端服务） |
| 录音/播放 | sounddevice | 基于 PortAudio |
| VAD | silero-vad（自研 onnx 运行时） | 神经网络 VAD；**修订 21 起用 onnxruntime + numpy 直跑 `.onnx` 模型，不再依赖 torch**（torch 曾占打包体积 470MB，纯为跑这个 VAD），见 §2 VAD 选型 + §8.10 |
| 意图解析 | Claude API 或 OpenAI 兼容 API，默认 provider `anthropic`，默认模型 `claude-haiku-4-5-20251001` | 用 tool use / function calling 解析固定指令集；若解析准确率不达标（见第 7 节验收标准），升级到更强模型，不默认上限制更高的模型；支持通过 `provider` 字段切换 Anthropic / OpenAI 协议，见第 2.1 节 |
| 主程序 | Python 3.10+，asyncio | 异步事件循环 |

### VAD 选型建议

不要两个都集成，选一个：

- **webrtcvad**：轻量、无需下载模型、纯 C 扩展速度快，但只做"有没有声音"的粗判断，噪音环境下误触发率较高。
- **silero-vad**：基于神经网络，准确率明显更高，能更好区分人声和背景噪音（键盘声、风扇声）。模型仅 2.3MB，**修订 21 起 vendor 进仓库（`audio/silero_vad.onnx`）随包走，不需首次下载**。

**建议：直接用 silero-vad**。这是一个持续监听 + 全双工打断的场景，误触发直接影响体验：静默时误触发浪费算力甚至误操作，播报时误触发会导致"随便一点杂音就打断正在说话的助手"。准确率优先于轻量化。webrtcvad 只在你后续发现 CPU 资源紧张时作为降级选项。

**运行时改自研 onnx（修订 21）**：silero-vad 的官方 pip 包（`silero_vad`）深度绑定 torch——`model.py`/`utils_vad.py` 顶部就 `import torch, torchaudio`，连它的 `OnnxWrapper` 都用 torch 张量管 LSTM 状态，所以**即便走包的 `load_silero_vad(onnx=True)` 也照样把 torch 拖进来**。而 torch 在本项目里唯一的用途就是跑这个 VAD（faster-whisper 走 CTranslate2、不碰 torch），代价是打包体积里 470MB 的 torch（`torch_cpu.dll` 单文件 291MB）。决策：**绕开整个 `silero_vad` 包，把 2.3MB 的 `silero_vad.onnx` 模型文件 vendor 进仓库（`audio/silero_vad.onnx`），运行时用 `onnxruntime` + `numpy` 直接喂模型**，自行复刻 `OnnxWrapper` 的状态机（16kHz：512 samples/帧、context 64、state `(2,1,128)` float32，逐帧 `session.run` 拿概率 + 滚动更新 state/context）。**可行性已实测（2026-07-15）**：纯 numpy onnx 与原 jit 路径在同一音频流上逐帧概率一致（最大差 1.4e-5，纯浮点噪声），是真·drop-in，检测行为零变化。收益：打包去掉 torch + torchaudio，exe 从 ~770MB 降到 ~300MB，且**仍然离线**（onnx 随包走，不需首次下载）。webrtcvad 作为极端降级选项的定位不变。

**全双工场景下 VAD 有两套阈值**：静默监听时用标准阈值判断"有没有人说话"；播报进行中用更高阈值判断"是否是用户主动打断"（区分用户说话 vs 播报声音本身泄漏进麦克风）。这两套打断阈值又进一步按**耳机/外放**两种输出模式各自配置，共四组参数（静默-标准、播报-耳机、播报-外放，加一组静默阈值实际是共用的，所以是 1+2 共 3 组），在 `config.yaml` 中分别写清楚，不要复用同一套参数，见第 8 节坑说明。

**耳机/外放判定方式**：启动时通过 `sounddevice.query_devices()` 获取当前输出设备名称，用关键词匹配（如包含"headphone"、"耳机"、"airpods"、"buds"等）粗判是否为耳机模式，匹配失败默认按外放模式处理（更保守，避免高估打断能力）。用户可在 `config.yaml` 中显式设置 `output_mode: headphone / speaker / auto`，`auto` 走自动检测，显式设置则跳过检测直接生效。**自动检测不可靠是已知且接受的事实**，不追求覆盖所有设备命名规则，第 8 节会给出关键词表的规避思路。

### 2.1 LLM 多 Provider 支持（Anthropic / OpenAI 兼容）

**背景**：部分中转站不兼容 Anthropic Messages API 格式，只提供 OpenAI 兼容端点（`/v1/chat/completions`）。项目要在这两种协议间自由切换，不绑死某一家。

**架构**：新增抽象层，`intent_parser.py` 不直接依赖任何厂商 SDK，只依赖统一接口 `LLMClient`：

```
nlu/
├── llm_client.py              # 抽象基类 LLMClient，定义统一接口 parse_intent(text, schema) -> dict
├── providers/
│   ├── __init__.py
│   ├── anthropic_provider.py  # AnthropicClient(LLMClient)，内部用 anthropic SDK + tool use
│   └── openai_provider.py     # OpenAIClient(LLMClient)，内部用 openai SDK + function calling / json_schema
├── intent_parser.py           # 只 import LLMClient，按 config 里 provider 字段实例化具体 provider
└── schema.py
```

**统一接口约定**：
- `LLMClient.parse_intent(text: str, schema: dict) -> dict`：输入用户文本，输出符合 `schema.py` 定义的结构化字典，业务层不感知底层是 tool use 还是 function calling
- 两个 provider 各自负责把统一 schema 转换成自己协议要求的格式（Anthropic 的 tool 定义 vs OpenAI 的 function/json_schema 定义），转换逻辑封装在各自 provider 内部，不外泄
- 两边错误处理行为不一致是既定事实（见第 8.9 节），`llm_client.py` 层统一抛自定义异常（如 `LLMParseError`、`LLMTimeoutError`），屏蔽掉厂商差异，`intent_parser.py` 只处理这几种自定义异常

**约定**：
- `config.yaml` 的 `llm` 块新增 `provider` 字段，取值 `anthropic` / `openai`，决定实例化哪个 provider
- `base_url` 字段两边通用：Anthropic 场景传给 `Anthropic(base_url=...)`，OpenAI 场景传给 `OpenAI(base_url=...)`，留空则各自走官方默认端点
- `api_key_env` 约定不变，从环境变量读取，两个 provider 共用同一套读取逻辑
- **模型名不做跨协议映射**：`model` 字段直接透传给对应 SDK，比如 `provider: openai` 时 `model` 就填中转站要求的模型标识符（可能是 `gpt-4o`，也可能是中转站自定义的别名），项目不做"自动选模型"这种智能行为
- **不做的事**：不支持 OpenAI 和 Anthropic 之外的第三种协议（比如 Google Gemini 原生格式），不做运行时自动探测协议类型——协议由 `provider` 字段显式声明，探测行为增加不确定性且不在需求范围内

**换 provider 前必须验证的事（对应第 8.9 节）**：两个协议都支持 tool use / function calling 是硬性前提（第 5 节意图解析要求 LLM 只输出结构化数据），换任何一个 provider 或中转站之前，先用一次性脚本单独测试目标端点是否真的支持结构化输出，不要等集成进 `intent_parser.py` 才发现不支持。

### Windows 安装说明

```powershell
# 1. 创建虚拟环境（务必做，见第 8 节坑说明）
python -m venv venv
.\venv\Scripts\activate

# 2. 安装核心依赖
pip install faster-whisper
pip install edge-tts
pip install sounddevice
pip install onnxruntime        # VAD 走 onnxruntime 直跑 silero_vad.onnx（修订 21，不再装 silero-vad/torch）
pip install numpy

# 3. faster-whisper 依赖 ffmpeg（部分场景需要），确保系统已安装
#    未安装则用 winget 装：
winget install ffmpeg

# 4. LLM 调用 SDK（意图解析用，见第 2.1 节，两个 SDK 都装，按 config 的 provider 字段切换）
pip install anthropic
pip install openai

# 5. GUI 控制台（修订 19，仅跑原生控制台 / 打 GUI exe 时需要；Windows 经 pythonnet 驱动 WebView2）
pip install pywebview
```

**API Key 管理**：不写入代码或 `config.yaml` 明文，从环境变量读取（Windows 下用"系统属性→环境变量"设置，变量名自定，如 `ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY`），`config.yaml` 里只写 `api_key_env: ANTHROPIC_API_KEY` 这样的变量名引用，具体读哪个变量名由 `provider` 字段对应的实际部署决定，不强制变量名格式。**修订 19 放宽**：GUI 控制台可把 key 存进 gitignore 的本地文件 `voxctl.local.yaml`（机器写入），语音启动时由 `core/local_settings.py` 的 helper 读出、注入到该环境变量再供 `intent_parser` 读取——读取路径不变（仍从 env var 读），key 仍**不进 `config.yaml` 明文**；但该本地文件随 exe 文件夹走，**勿分享 / 传 git**，否则 key 泄露。见 §4、§6 与第 9 节修订 19。

**GPU 加速（可选，非 MVP 必需）**：faster-whisper 若要用 GPU，需要安装 CUDA 版 PyTorch/CTranslate2，Windows 上配置较繁琐，MVP 阶段一律用 CPU 模式跑，模型默认用 `small`（约 500MB）。实测 `base` 模型（约 150MB）在安静环境下 10 句清晰指令中仅 1 句完全正确，CER 远超 10% 阈值，已按升级触发条件切换到 `small`（Step 2 实测验收，见第 9 节变更记录）。若 `small` 仍不达标，再考虑 `medium`。GPU 加速留到性能优化阶段再做。

### 启动与退出方式

- **启动**：项目根目录提供 `run.bat`，内容为激活虚拟环境 + 运行 `python main.py`，用户双击即可启动，不需要手动敲命令：
  ```bat
  @echo off
  call venv\Scripts\activate
  python main.py
  ```
- **退出**：两种方式都要支持，不要求用户必须精通某一种：
  - 控制台按 `Ctrl+C`（开发调试用）
  - 语音说"退出程序"/"关闭助手"类指令，走正常的意图解析→执行流程，执行动作是"结束主循环"，退出前 TTS 播报确认音（"再见"）再真正退出
- 语音退出指令加入白名单（见第 6 节），不需要二次确认（退出程序不是破坏性操作）。

---

## 3. 模块划分与文件结构

```
voice-assistant/
├── CLAUDE.md                  # 本文件
├── requirements.txt           # 依赖清单，版本锁定
├── config.yaml                # 配置文件：模型选择、音频设备、白名单等
├── run.bat                    # 一键启动脚本（激活虚拟环境 + 运行 main.py）
├── main.py                    # 入口，启动 asyncio 主循环；日志配置调 core/logger.py 的 configure_root_logging()（修订 25 起）
├── app.py                     # 统一入口（修订 19）：默认起 GUI 控制台，`--voice` 跑语音；GUI exe 打包入口
│
├── audio/
│   ├── __init__.py
│   ├── recorder.py            # sounddevice 录音封装
│   ├── player.py               # sounddevice 播放封装
│   ├── vad.py                  # VAD 封装，判断人声起止（含打断判定逻辑）；onnxruntime 直跑 onnx（修订 21）
│   ├── silero_vad.onnx         # vendor 的 silero-vad 模型（2.3MB，打包资产，修订 21）
│   └── output_mode.py          # 耳机/外放自动检测 + 手动覆盖
│
├── asr/
│   ├── __init__.py
│   └── whisper_engine.py      # faster-whisper 封装
│
├── nlu/
│   ├── __init__.py
│   ├── llm_client.py           # 抽象基类 LLMClient，统一接口 parse_intent()，见第 2.1 节
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── anthropic_provider.py  # AnthropicClient(LLMClient)，anthropic SDK + tool use
│   │   └── openai_provider.py     # OpenAIClient(LLMClient)，openai SDK + function calling
│   ├── intent_parser.py       # 按 config 的 provider 字段实例化对应 LLMClient，只依赖抽象接口
│   └── schema.py               # 定义指令的数据结构（用 pydantic）
│
├── executor/
│   ├── __init__.py
│   ├── app_control.py         # 打开/关闭应用
│   ├── app_index.py           # 已安装应用索引（开始菜单 + App Paths，修订 15）
│   ├── system_query.py        # 查内存/磁盘/CPU 等状态
│   ├── file_ops.py            # 文件操作
│   ├── shell_runner.py        # 执行自定义命令
│   └── whitelist.py           # 白名单校验 + 二次确认逻辑
│
├── tts/
│   ├── __init__.py
│   └── edge_tts_engine.py     # Edge TTS 封装
│
├── core/
│   ├── __init__.py
│   ├── event_loop.py           # 主循环：并发调度监听通道与播报通道
│   ├── interrupt_coordinator.py # 状态协调器：打断信号判定与分发
│   ├── state.py                 # 全局状态机（空闲/监听/识别/执行/播报/等待确认）
│   ├── logger.py               # 共用日志配置 configure_root_logging()（修订 25 真正实现）：建 logs 目录 + RotatingFileHandler(UTF-8) + windowless-safe StreamHandler；main.py / webui/gui.py 各传自己的参数调用
│   └── local_settings.py       # voxctl.local.yaml 读写 + LLM 覆盖注入（修订 19）
│
├── webui/                      # 本地管理控制台（修订 24：只剩 PyWebView GUI 版，浏览器版已删）
│   ├── __init__.py
│   ├── gui.py                  # PyWebView 原生窗口 + js_api：别名管理/解析/打开/审计日志 + LLM/ASR/TTS 设置 + 启停语音；含 ConsoleState / 别名读写 / validate_alias（修订 24 从已删的 server.py 内联进来）；日志配置调 core/logger.py 的 configure_root_logging()（修订 25 起）
│   └── static/
│       └── index.html          # 控制台前端（单文件，无构建步骤）
│
├── build_voice.bat             # 语音主程序 exe 打包脚本（PyInstaller onedir → dist/voxctl/，修订 18）
├── build_gui.bat               # GUI 控制台 exe 打包脚本（PyInstaller onedir --windowed → dist/voxctl/，修订 19）
│
├── user_apps.yaml              # webui 维护的用户别名表（机器写入；启动时合并进 whitelist.apps，config.yaml 优先）
├── voxctl.local.yaml           # GUI 维护的本地覆盖：llm.{provider,model,base_url,api_key} + asr.model_size + tts.voice（机器写入，gitignore，修订 19/23）
│
├── design/                     # UI 设计稿/原型（修订 22，纯参考物：不参与打包、不被代码 import）
│   └── settings-mockup.html    # 设置界面视觉原型（命名约定：<页面>-mockup.html）
│
├── tests/
│   ├── test_vad.py
│   ├── test_asr.py
│   ├── test_intent_parser.py
│   └── test_executor.py
│
└── logs/
    └── actions.log              # 执行动作审计日志（不进 git）
```

### 命名与存放约定

- 配置一律进 `config.yaml`，禁止在代码里硬编码路径、模型名、白名单。
- 每个模块自己的异常，用自定义 Exception 类，不要裸用 `Exception`。
- 日志文件、模型缓存文件不进 git，写入 `.gitignore`。
- 新增执行能力（比如以后加"发邮件"）在 `executor/` 下新建文件，注册进白名单，不要塞进已有文件。
- `design/` 存 UI 设计稿/原型（修订 22）：命名 `<页面>-mockup.html`，纯参考物——不参与打包、不被任何代码 import、可随时删除；设计稿允许用 CDN（如 Tailwind CDN），但**生产界面（`webui/static/`）必须离线自足**（frozen exe 无网也要能渲染，不引任何 CDN 资源）。设计定稿并落入 `webui/static/` 后，mockup 保留作视觉基准。

### config.yaml 骨架

以下是字段结构约定，不是最终值，实际数值在开发中按第 7 节验收标准调优：

```yaml
audio:
  input_device_index: null      # null = 启动时打印设备列表引导用户选择
  output_mode: auto             # auto / headphone / speaker，见第 8.5 节
  sample_rate: 16000

vad:
  silence_threshold: 0.5        # 静默监听阈值
  interrupt_threshold_headphone: 0.6   # 播报中打断判定阈值（耳机模式）
  interrupt_threshold_speaker: 0.8     # 播报中打断判定阈值（外放模式，更保守）

asr:
  model_size: small              # 默认 small（base 实测 CER 不达标，已升级，见第 9 节）；仍不达标再评估 medium
  device: cpu                    # cpu / cuda，MVP 固定 cpu

llm:
  provider: anthropic             # anthropic / openai，决定用哪个 provider 实现，见第 2.1 节
  model: claude-haiku-4-5-20251001  # provider=openai 时填对应模型标识（如 gpt-4o 或中转站别名）
  api_key_env: ANTHROPIC_API_KEY  # 从环境变量读取，不写明文 key；换 provider 时同步换变量名
  base_url: ""                    # 留空 = 走对应 provider 官方端点；非空指向中转站，见第 2.1 节

tts:
  voice: zh-CN-XiaoxiaoNeural     # Edge TTS 中文音色，可替换

whitelist:
  apps:
    chrome: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    notepad: "C:\\Windows\\System32\\notepad.exe"
  commands: []                    # 每条需标注 dangerous: true/false，见第 6 节
  paths:
    - "%USERPROFILE%\\Desktop"
    - "%USERPROFILE%\\Documents"
```

---

## 4. MVP 定义

### 必须做（MVP 范围内）

1. 麦克风持续监听，VAD 检测人声自动开始录音，静默自动结束
2. ASR 把录音转文字（中文）
3. LLM 把文字解析为结构化指令（意图 + 参数）
4. 执行以下四类操作：
   - 打开/关闭指定应用程序
   - 查询系统状态（内存/磁盘/CPU）
   - 打开指定路径下的文件
   - 执行白名单内的自定义命令
5. 执行结果通过 TTS 播报
6. **播报期间检测到用户说话，能打断播报**（简化能量/音色阈值判断，非完美 AEC，区分耳机/外放两套阈值）
7. **启动时自动检测耳机/外放模式，用户可手动覆盖**（关键词匹配，非 100% 准确，见第 8 节）
8. 所有执行动作写审计日志
9. 危险操作（删除、关机、执行白名单外命令）需要语音二次确认；**打断只能取消播报或取消等待确认的状态，已发出的执行动作不可撤回**

### 明确延后（不在 MVP 里，不要提前做）

- **工业级声学回声消除（AEC）**：MVP 用简化阈值判断打断，不追求完美区分"用户说话"和"播报声音泄漏"，误判（漏判或误触发）在 MVP 阶段可接受，作为独立的后续优化项。**实测依据（2026-07-13）**：外放场景下 TTS 播报声被麦克风拾取后 silero-vad 概率稳定在 0.88-0.97，调阈值方案已证明无效（见 §8.4 实测结论），外放全双工打断在 MVP 阶段标记为已知限制，建议用耳机模式验证核心功能。
- **更精确的设备类型识别**：MVP 用设备名关键词匹配，检测不准时依赖用户手动覆盖。更可靠的方式（如检测音频接口类型、蓝牙协议判断）留到后续优化。
- **多轮对话上下文**：每次指令独立处理，不记住上一句说了什么。打断后重新说的内容视为全新指令，不延续被打断前的上下文。
- **唤醒词**（如"你好小助手"）：MVP 用纯 VAD 激活，不用唤醒词。
- **GPU 加速**：CPU 模式够用就行。
- **多说话人区分**：不做声纹识别，也不做"是不是同一个人打断"的判断。
- **图形界面**：语音是唯一的主交互通道，不做语音流程相关的 GUI。修订 16 松动一条口子：允许一个**本地管理控制台**（`webui/`）用于别名注册、解析测试和审计日志查看。**修订 19 再松一条**：把该控制台做成 **PyWebView 原生窗口**（`webui/gui.py`，无浏览器 / 无 localhost，走 js_api 桥）作为 GUI exe 的"应用正脸"——除维护功能外，新增**改 LLM 配置**（base_url / model / api_key，写入 `voxctl.local.yaml`）和**启停语音交互**（子进程方式）。**修订 24**：删掉早期的浏览器版（`webui/server.py` 的 stdlib http 服务 + `webui.bat` + `build_console.bat`），只保留 PyWebView GUI 版——两版长期并存是重复维护，GUI 版已完全覆盖浏览器版功能；GUI 复用的底层（`ConsoleState` / 别名读写 / `validate_alias` / frozen 路径解析）从 server.py 内联进 `webui/gui.py`。定位仍是配置 / 生命周期 / 维护工具，**不替代语音**：控制电脑仍只走语音 + 二次确认，语音主循环不依赖 GUI（GUI 只负责拉起 / 杀掉语音子进程），不跑 GUI 语音功能完全不受影响。系统托盘状态图标仍属延后项。
- **跨设备/远程控制**：仅本机操作。

### 边界情况（做，但明确降级策略）

- ASR 识别失败或空文本 → TTS 提示"没听清，请再说一次"，不进入执行流程。
- LLM 解析不出明确意图 → TTS 提示"不确定你想做什么"，不猜测执行。
- 执行报错（如应用不存在）→ TTS 播报具体错误，不静默失败。

---

## 5. 分步开发计划

严格按顺序做，每一步都要能独立跑通并有对应测试，不要跳步骤。

### Step 1：录音闭环
- 实现 `audio/recorder.py`，能录制并保存一段 wav 文件
- 实现 `audio/vad.py`，能从一段音频里标出人声起止时间戳
- 验收：说一句话，能自动截取出干净的人声片段（掐头去尾）

### Step 2：ASR 接入
- 实现 `asr/whisper_engine.py`，输入音频片段，输出中文文本
- 验收：Step 1 截出的人声片段，能正确转成文字，误差在可接受范围（人工核对 10 句）

### Step 3：意图解析
- 定义 `nlu/schema.py` 里的指令结构（用 pydantic，明确字段：intent 类型、参数）
- 实现 `nlu/llm_client.py` 抽象基类，再实现 `nlu/providers/anthropic_provider.py` 和 `nlu/providers/openai_provider.py`，最后实现 `nlu/intent_parser.py`（只依赖抽象接口，按 config 的 `provider` 字段选实现），见第 2.1 节
- **LLM 在这一步只输出结构化数据（tool use / function calling 结果），不生成任何自然语言**——用协议自带的结构化输出能力强制约束，不依赖 prompt 里"请用 JSON 回答"这种软约束。这样从根上避免"LLM 用英文回复"或"LLM 输出格式跑偏"的问题，不需要靠 system prompt 提示语言
- 解析不出明确意图时，返回预定义的 `unknown` 类型，不是让 LLM 自由发挥解释
- 验收：给定 10 条典型指令（"打开浏览器"、"C盘还有多少空间"等），**Anthropic 和 OpenAI 两个 provider 各自独立跑一遍**，解析正确率均为 100%（因为是固定的小范围指令集，不该有歧义）；只验收其中一个 provider 不算完成本步骤

### Step 4：执行模块
- 逐个实现 `executor/` 下四类操作
- 实现白名单机制（见第 6 节）
- 验收：每类操作至少 3 个真实用例跑通，白名单外的操作被正确拦截

### Step 5：TTS 反馈
- 实现 `tts/edge_tts_engine.py`
- 执行结果转成播报文本用**模板化拼接**，不是让 LLM 生成自然语言反馈——比如"内存查询"结果直接套模板"当前内存剩余 X GB，共 Y GB"，"打开应用成功"套模板"已为你打开 XX"。这样播报内容可控、可测试、不会出现语言或语气跑偏
- 验收：执行结果、错误提示都能正确播报

### Step 6：串联主循环（单向串行版本，先跑通功能）
- 实现 `core/event_loop.py`、`core/state.py`，把 Step 1-5 串成完整状态机
- 这一步**先不做打断**，播报期间监听通道正常采集但不触发新一轮处理，先保证"说话 → 执行 → 语音反馈"全流程功能正确
- 验收：端到端跑通全流程，不需要按任何键，功能层面无 bug

### Step 7：叠加全双工打断能力
- 实现 `audio/output_mode.py`：启动时检测耳机/外放，写入运行时状态；支持 `config.yaml` 手动覆盖
- 实现 `core/interrupt_coordinator.py`
- 播报进行中，监听通道根据当前输出模式选用对应阈值，用 VAD 持续判断是否有人打断
- 判定为打断后：立即 stop 播放流、清空播报队列、状态切回监听、新一轮语音走完整流程（ASR→意图→执行→播报）
- 明确处理：打断发生时如果正在等待危险操作的二次确认，打断视为取消该操作，不执行；如果操作已经开始执行（已发出系统调用），打断不影响该操作继续完成
- 验收：耳机模式下打断响应灵敏且误触发少；外放模式下打断可用但允许更保守（漏判率高于耳机模式属预期）；手动覆盖 `output_mode` 配置能生效并跳过自动检测

### Step 8：加固
- 补全日志、异常处理
- 调优打断阈值，平衡"打断不灵敏"和"误触发打断"
- 验收：连续跑 30 分钟不崩溃，打断响应及时，误触发率人工评估可接受

**顺序不能反**：先做通 Step 6 的单向流程再叠加 Step 7 的并发打断，不要一开始就在未验证的单向流程上直接堆并发逻辑——并发 bug 排查成本远高于串行 bug，前置流程不稳，打断逻辑无从验证是打断本身的问题还是底层流程的问题。

---

## 6. 安全注意事项

这是本项目风险最高的部分，语音指令直接控制系统，误触发/误解析的代价可能是删错文件或执行危险命令。

### 白名单机制（强制）

- `config.yaml` 中 `whitelist` 下维护三类白名单（字段名与第 3 节 config.yaml 骨架一致）：
  - `whitelist.apps`：应用别名 → 实际路径映射。修订 15 起语义变化：对**打开**是"优先别名表"（先查这里，查不到再走应用索引，见下条）；对**关闭**仍是唯一边界（不在表里的应用不能关）
  - `whitelist.commands`：允许执行的 shell 命令（精确匹配或受限的参数模式，禁止任意命令拼接）
  - `whitelist.paths`：允许操作文件的目录范围（如仅限桌面、文档目录，禁止系统目录）
- **打开应用不再限于白名单（修订 15）**：打开是非破坏性操作（最坏结果是开错应用，说一句"关闭它"即可挽回），放开为"可打开本机任何已安装应用"。实现：启动时扫描开始菜单快捷方式（`%ProgramData%` 和 `%AppData%` 下的 `Start Menu\Programs`，递归 `.lnk`）+ 注册表 App Paths（HKLM/HKCU `...\CurrentVersion\App Paths`）建立"应用名 → 启动方式"索引（`executor/app_index.py`）。约束：
  - 匹配规则是**确定性的，不是模糊放行**：先精确匹配（归一化大小写/空格后全等），再唯一子串匹配（查询词只命中一个候选才放行）；命中多个 → 拒绝并播报候选让用户说得更具体；命中零个 → 播报"没有找到"。不做"选相似度最高的"这类猜测执行
  - 索引**排除卸载类快捷方式**（名称含 uninstall / 卸载），防止"打开 XX"唯一子串命中"卸载 XX"
  - `whitelist.apps` 优先级最高（用户自定义别名，如 chrome → 指定路径），索引只在别名表查不到时兜底
  - **`user_apps.yaml`（修订 16）**：webui 控制台维护的用户别名扩展表（应用名 → exe/.lnk 路径），`Executor.from_config` 启动时合并进 `whitelist.apps`；同名冲突时 config.yaml 优先，user_apps.yaml 不覆盖。独立成文件是因为 config.yaml 有大量手写注释，机器写入会毁掉注释；user_apps.yaml 是纯机器读写文件
  - `config.yaml` 开关 `whitelist.allow_open_any_app`（默认 true），置 false 恢复"打开也只限白名单"的旧行为
  - 审计日志记录解析来源（`resolved_via: whitelist / index`）和实际启动的目标路径
- **关闭应用、命令、文件路径仍 100% 白名单强制**：关闭 = 杀进程，可能丢未保存数据，不放开；`commands` / `paths` 是高危面，不放开。任何不在白名单内的这三类操作，一律拒绝执行，并 TTS 告知用户"这个操作不在允许范围内"，不做智能猜测或模糊匹配放行。
- **GUI 控制台边界（修订 19）**：`webui/gui.py` 的 PyWebView 原生控制台**只暴露非破坏性操作**——改 LLM 配置（base_url / model / api_key，写 `voxctl.local.yaml`）、启停语音子进程、别名注册、解析测试、打开应用；**绝不**暴露关闭应用 / 删文件 / 跑命令（这三类仍只走语音 + 二次确认，边界同上条）。启停语音只是拉起 / 杀掉语音子进程，不是系统破坏性操作，不需二次确认。api_key 存 `voxctl.local.yaml` 明文（用户明确选择，§2 已记录放宽），语音仍经环境变量读取（`core/local_settings.py` 启动时注入），key **不进 `config.yaml`**；该本地文件随 exe 文件夹走，勿分享 / 传 git。
- **启停必须给出失败反馈（修订 20）**：`Popen` 成功 ≠ 语音初始化成功——子进程可能在加载 vad/whisper 后因缺 key / 换错 base_url / 设备异常而崩溃退出，GUI **不得静默**把开关拨回"已停止"，须回读 `logs/voxctl.log` 尾部异常行把原因告知用户。落实为四道防线：启动前预检 key（本地文件或 env 都没有就直接拒绝，不白起必崩进程）、子进程 stdout/stderr 重定向到 `logs/voxctl-voice.out`（兜早期崩溃）、启动后短等探活（源码模式快速崩当场回读）、`voice_status` 轮询区分"用户主动停"与"意外崩溃"（frozen 导入慢崩在探活窗口之后的兜底）。

### 二次确认机制（强制）

以下操作即使在白名单内，也必须语音二次确认（"确定要关闭 XX 吗？"，等待"是/确定"类回答）：

- 关闭正在运行且可能有未保存内容的应用（如 Word、代码编辑器）
- 删除文件（无论是否在 MVP 里做，规则先立）
- 执行任何 `whitelist.commands` 中标记为 `dangerous: true` 的命令
- 关机、重启、注销

### 明确禁止（不做白名单，永久拒绝）

- 修改系统关键目录（`C:\Windows`、`C:\Program Files` 下的文件删改）
- 执行未经白名单校验的任意 shell 命令拼接（防止 LLM 解析出错或被提示注入时执行任意代码）
- 网络请求类的破坏性操作（如任意 URL 的 POST/DELETE 请求）不在本项目范围内，不做

### 打断与执行状态的边界（强制，全双工特有）

- 打断能取消的状态：**播报中**、**等待二次确认中**。用户在这两个状态下说话并被判定为打断，视为用户主动放弃当前交互，对应操作按"未确认/未执行"处理。
- **打断退出播报 = 取消退出**：用户说"退出程序"后，退出确认音（"再见"）播报期间被打断，视为用户反悔，取消退出，回到监听状态处理新指令。理由：用户开口打断说明还有事要说，此时继续退出违背打断的语义；且退出本身不是已发出的系统调用（只是主循环 break 的标志位），取消它没有物理障碍。修订 10 中"exit_requested 保留不强制退出"的表述有歧义且与实际代码行为矛盾（保留标志位会导致主循环 while 条件直接 break，程序照样退出），以本条为准。
- **`exit_requested` 的置位时机 = 退出播报正常播完之后**（修订 14）：EXECUTE 阶段拿到 `should_exit=True` 的结果时**不**置位，只切到 TTS 阶段；`_phase_tts` 正常播完"再见"后检查 `result.should_exit` 再置 `exit_requested=True`。理由：主循环 `while not exit_requested` 在每个阶段之间都会检查条件，EXECUTE 阶段提前置位会导致循环在 TTS 阶段执行前就 break，"再见"永远播不出来，"打断取消退出"也就无从触发（live 实测 2026-07-13 暴露）。播报被打断时标志位从未置上，退出自然取消，无需清除动作。
- 打断不能取消的状态：**已发出系统调用、正在执行中**的操作。比如删除文件的命令已经调用，此时哪怕检测到用户说话，也不会、也不能撤回已发出的调用。这不是选择，是操作系统层面的物理限制，文档里写清楚是为了避免以后有人误以为"打断"是万能撤销键。
- 审计日志必须记录打断事件本身：什么时候发生了打断、打断前处于什么状态、打断取消了什么（如果有，含"取消了退出"）。排查"为什么该执行的没执行"或"为什么误触发打断"都要靠这条记录。

### 审计日志（强制）

`logs/actions.log` 记录每一次执行动作：时间戳、原始语音转文字内容、解析出的意图、执行的具体操作、执行结果、是否发生打断及打断详情。这是排查误触发的唯一依据，不能省。

---

## 7. 测试验收标准

| 测试项 | 标准 |
|---|---|
| VAD 准确性 | 安静环境下人声检出率 > 95%，静默环境误触发 < 1 次/10 分钟 |
| ASR 准确率 | 标准普通话指令，字错误率（CER）< 10% |
| 意图解析准确率 | 固定指令集（打开应用/查状态/开文件/执行命令）解析正确率 100% |
| 执行成功率 | 白名单内操作执行成功率 > 95%（失败需明确报错原因） |
| 白名单拦截率 | 白名单外操作拦截率 100%，不允许任何绕过 |
| 端到端延迟 | 说完话到开始播报反馈，< 5 秒（CPU 模式下） |
| 连续运行稳定性 | 连续运行 30 分钟不崩溃、不内存泄漏 |
| 打断响应延迟 | 用户开口打断到播报实际停止，< 500ms（两种模式一致，判定后停止速度不受模式影响） |
| 打断误触发率（耳机模式） | 播报期间无人说话，误判为打断 < 1 次/20 分钟 |
| 打断误触发率（外放模式） | 播报期间无人说话，误判为打断 < 1 次/10 分钟（外放场景阈值更保守，容忍度略低） |
| 打断成功率（耳机模式） | 用户明确尝试打断，成功率 > 95% |
| 打断成功率（外放模式） | 用户明确尝试打断，成功率 > 90%（简化阈值方案下外放场景的合理目标，非 100%） |
| 输出模式检测准确率 | 常见设备（品牌耳机、笔记本内置扬声器、外接音箱）识别正确率 > 80%，识别失败时手动覆盖必须生效 |
| 危险操作边界 | 打断发生在"等待二次确认"阶段，操作 100% 不执行；打断发生在"已执行中"阶段，操作 100% 不受影响继续完成 |

每个 Step 完成后跑对应测试，不要等到 Step 6 才一起测。

---

## 8. Windows 常见坑与规避方案

### 8.1 虚拟环境
- **坑**：不用虚拟环境，全局装依赖，版本冲突后很难排查。
- **规避**：项目第一件事就是 `python -m venv venv`，`requirements.txt` 锁定版本号（`pip freeze > requirements.txt`），不要用模糊版本号。

### 8.2 音频设备选择
- **坑**：Windows 上有多个音频设备（内置麦克风、耳机、虚拟声卡等），sounddevice 默认设备不一定是你想要的，容易出现"程序没报错但录不到声音"。
- **规避**：启动时用 `sounddevice.query_devices()` 列出所有设备，在 `config.yaml` 里显式指定设备索引，不要依赖默认设备。首次运行打印设备列表引导用户选择。

### 8.3 faster-whisper 首次加载慢
- **坑**：首次运行会下载模型（几百 MB 到几 GB，取决于模型大小），用户会以为程序卡死。
- **规避**：
  - MVP 阶段默认用 `base` 模型（见第 2 节），别一上来就用 `large`
  - 启动时打印明确的进度提示（"首次运行需下载模型，请稍候..."）
  - 模型下载完成后本地缓存，后续启动是秒级加载，这一点要在文档里告诉用户，避免每次启动都以为很慢

### 8.4 播报时自己唤醒自己 vs 真正的用户打断（全双工核心难点）
- **坑**：TTS 播放声音时，麦克风还在监听，会把自己的播报声当成人声输入。在单向架构里这是要完全避免的死循环；但在全双工架构里，播报期间麦克风必须保持监听（不能像非全双工方案那样直接挂起），问题变成"如何区分这是自己的播报声漏进麦克风，还是用户真的在说话打断"。
- **规避（MVP 简化方案，非完美 AEC）**：
  - 播报中用**独立的、更高的 VAD 阈值**（见第 2 节 VAD 选型说明），只有明显更响、持续时间更长的人声才判定为打断，过滤掉播报声音本身的泄漏
  - 按输出模式（耳机/外放）分别配置阈值，耳机场景基本无泄漏可以适当调低，外放场景必须保守
  - 外放场景下打断阈值必然要保守（宁可漏判打断，不能频繁误触发），这是简化方案的固有取舍，第 7 节测试标准里外放模式打断成功率目标定的是 90% 而非 100%，就是承认这一点
  - 真正的解决方案是声学回声消除（AEC），MVP 不做，作为后续独立优化项
- **实测结论（2026-07-13 live 验证）**：调阈值方案在外放场景下**已证明无效**。干净静音对照测试显示，TTS 播报声音被麦克风拾取后，silero-vad 输出的语音概率稳定在 **0.88-0.97**，远超任何合理阈值——这不是卡在临界点的抖动，是 VAD 模型非常确信地误判为人声。把 `interrupt_threshold_speaker` 从 0.8 调到 0.85 + `speech_confirm_frames` 从 2 调到 3 仍无法过滤；继续调高会牺牲正常打断的灵敏度，失去全双工意义。
  - **MVP 阶段结论**：外放场景下全双工打断不可用，属已知限制。建议外放场景用耳机模式验证核心功能，或接受外放模式下"播报期间无法打断"的退化体验。
  - **后续方向**：要么做真正的 AEC（WebRTC AEC3 / SpeexDSP），要么退化成"播报期间不监听打断"的单向模式（牺牲全双工）。两者都不在 MVP 范围。

### 8.5 输出设备类型自动检测不可靠
- **坑**：`sounddevice.query_devices()` 拿到的设备名称五花八门，不同品牌、不同驱动命名规则不统一，关键词匹配（如"headphone"、"耳机"）不可能覆盖所有设备，会出现明明插着耳机却被判定为外放（阈值过保守，打断不灵敏）或反过来（阈值过松，外放场景疯狂误触发）。
- **规避**：
  - 关键词表覆盖常见情况即可（"headphone"、"headset"、"耳机"、"airpods"、"buds"、"耳麦"等），不追求穷尽，这是有意识的取舍而非疏漏
  - 检测失败或不确定时**默认按外放模式处理**（更保守的阈值），不要默认按耳机模式——错误地按耳机模式跑会导致外放场景下疯狂误触发打断，用户体验损失更大；错误地按外放模式跑最多是耳机场景下打断不够灵敏，可接受
  - `config.yaml` 里 `output_mode` 支持显式设置为 `headphone` 或 `speaker`，一旦显式设置就跳过自动检测逻辑，用户发现自动检测判断错了可以直接改配置解决，不用等代码更新关键词表
  - 启动时把检测到的模式打印/播报出来（"检测到当前使用外放模式"），让用户知道系统怎么判断的，而不是静默生效，这样用户能快速发现"判断错了"并去手动覆盖

### 8.6 中文编码
- **坑**：Windows 控制台默认编码不是 UTF-8，print 中文或写日志时可能乱码或报错。
- **规避**：程序开头显式设置 `sys.stdout.reconfigure(encoding='utf-8')`，日志文件写入时显式指定 `encoding='utf-8'`。

### 8.7 管理员权限
- **坑**：部分系统操作（如关闭某些受保护进程、修改系统设置）需要管理员权限，普通权限运行会静默失败或报权限错误。
- **规避**：MVP 范围内的操作（开关应用、查状态、开文件、白名单命令）不应该需要管理员权限。如果发现某个白名单操作必须要管理员权限，说明这个操作本身该被重新评估是否该在白名单里，而不是直接提权运行整个程序（提权运行语音控制程序风险太高，不建议）。

### 8.8 路径处理
- **坑**：Windows 路径用反斜杠，且用户口头说的路径（"桌面上的 test.txt"）需要转换成实际路径，中间容易出错（比如用户名里有中文、路径拼接用错分隔符）。
- **规避**：统一用 `pathlib.Path`，不要手写字符串拼接路径。常用位置（桌面、文档、下载）用 `os.path.expanduser` 或 Windows 专用 API 动态获取，不要硬编码 `C:\Users\xxx`。

### 8.9 LLM 中转站的坑
- **坑**：中转站声称"兼容 Anthropic API"，但实际可能只兼容部分字段（比如不支持 `tool_use` / 结构化输出，这恰好是第 5 步意图解析的核心依赖），或者错误码、限流行为跟官方不一致，直接换 `base_url` 跑起来可能在 Step 3 验收时才暴露问题。
- **规避**：
  - 换中转前先用一次性脚本单独测试该中转是否支持 function calling / tool use（这是本项目对 LLM 的唯一硬需求），不要等集成进 `intent_parser.py` 才发现不支持
  - 中转站的 401/429/5xx 行为可能和官方不同，`intent_parser.py` 里的异常处理不要假设错误码语义和官方完全一致
  - 中转站本质是你不完全信任的第三方服务，见第 6 节安全原则：LLM 输出仍然只做结构化解析，不因为换了中转就放松"不生成自然语言"、"不做智能猜测"这些约束

### 8.10 exe 打包
- **原始判断（MVP 阶段，保留作背景）**：主程序打包为独立 exe 曾是明确延后事项——faster-whisper + torch 体积大（torch CPU 包约 2GB），PyInstaller 容易漏依赖或体积暴增，MVP 用 `python main.py`（`run.bat`）方式运行。修订 18（语音主程序）、修订 19（GUI 控制台）先后落地打包，以下是踩坑后的实际方案。
- **修订 17：webui 浏览器版控制台打包**（`build_console.bat`，**修订 24 已删除**）。曾用 PyInstaller `--onefile` 打成 ~30MB 的 `voxctl-console.exe`（不含 whisper/torch，不踩体积坑）；浏览器版整体在修订 24 删除，此条留作历史。
- **修订 18：语音主程序打包为独立 exe**（用户需求：拷到没装 Python 的电脑上跑），关键决策：
  - **只能 onedir，不能 onefile**：打包产物本体约 730MB（`_internal/` 里 torch_cpu.dll 291MB + ctranslate2.dll 57MB + onnxruntime 等；CPU torch，装 CUDA torch 会更大）。onefile 每次启动都要把这几百 MB 重新解压到临时目录，明显拖慢启动，不划算。onedir 产出 `dist/voxctl/voxctl.exe` + `_internal/` 文件夹，拷**整个文件夹**到目标机，启动秒级。
  - **native / data 依赖必须显式 collect**，漏一个运行时才炸：ctranslate2（native DLL）、faster_whisper、onnxruntime（native DLL，跑 VAD 的 `.onnx`）、opencc（转换字典）、sounddevice（PortAudio DLL）、anthropic/openai（tokenizer/证书数据）。两个 provider 用 `--hidden-import` 兜底（`build_client` 经抽象层选 provider，静态分析可能跟不到）。全部写在 `build_voice.bat` 里。**修订 21 起去掉 torch/silero_vad 的 collect，改 `--add-data audio\silero_vad.onnx` + `--exclude-module torch --exclude-module torchaudio`**——VAD 已改 onnxruntime 直跑 onnx（见 §2），torch 不再被任何代码 import，显式 exclude 防它经某条残留路径被静态分析拖回来（-470MB）。
  - **whisper 模型不打进 exe，运行时下载**：small int8 权重 ~500MB，首次在新机器运行时从 HF 下载缓存到 `~/.cache/huggingface`——**目标机首次运行需联网**。要真离线可把模型目录 `--add-data` 进包、frozen 模式指向本地路径（+500MB），本次未做，留作增强项。
  - **config.yaml / user_apps.yaml / logs 不打进 exe**：与 webui 同理（config 要用户可改、user_apps 要可写、logs 运行时写）。`main.py` frozen 模式 `os.chdir` 到 exe 所在目录，这些文件按相对路径读写；`build_voice.bat` 打包后把 `config.yaml` 拷到 `dist/voxctl/` 旁边，目标机改这一份即可。
  - **API key 仍只从环境变量读（§6 铁律，不因"独立分发"松动）**：拷到目标机后必须在目标机 `setx ANTHROPIC_API_KEY <key>` 设好，否则启动即报 key 缺失。绝不把 key 写进 config 明文。
  - **console 模式（非 windowed）**：语音助手要在窗口里显示识别文本/日志，保留控制台窗口，与原 PowerShell 运行体验一致。
- **多 provider 的额外坑**：`nlu/providers/` 下两个 provider 文件只要都在代码里被 import（哪怕运行时只用一个），PyInstaller 静态分析就会把 `anthropic` 和 `openai` 两个 SDK 全部打进 exe，不会因为 `config.yaml` 里 `provider: anthropic` 就自动裁掉没用到的 `openai` 相关依赖。届时如果确定用户只会用固定的一种 provider，考虑在打包脚本层面用条件 import 或单独的打包配置排除掉另一个 SDK；如果两种都要保留给用户切换，接受体积代价即可，不用为了省这点体积牺牲切换灵活性。这个决策留到真正打包时再做，MVP 阶段不用管。

---

## 9. 变更记录

| 日期 | 变更内容 |
|---|---|
| 初版 | 建立项目规范，定义 MVP 范围与分步开发计划（当时误判为非全双工，架构为单向串行） |
| 修订 1 | 确认项目为真正全双工需求，架构改为双通道并发；打断能力移入 MVP 必做范围；新增状态协调器模块；明确打断与危险操作执行状态的边界规则（打断不可撤销已发出的系统调用）；开发计划拆分为"先单向跑通（Step 6）→ 再叠加打断（Step 7）"，顺序不可反 |
| 修订 2 | 打断阈值区分耳机/外放两种输出模式；耳机/外放判定改为自动检测（设备名关键词匹配）+ 用户手动覆盖；新增 `audio/output_mode.py` 模块；检测失败默认按外放（更保守）处理；测试标准拆分为耳机/外放两套独立指标 |
| 修订 3 | LLM 选型定死为 Claude API（默认 Haiku，验收不达标再升级）；补充 `config.yaml` 完整骨架示例；意图解析明确只输出结构化数据不生成自然语言（结构化输出而非 prompt 软约束）；TTS 播报文本改为模板化拼接而非 LLM 自由生成；ASR 模型默认值定为 `base`，明确升级触发条件；补充启动脚本 `run.bat` 与语音退出指令说明 |
| 修订 4 | 修正第 6 节白名单字段名与第 3 节 config.yaml 骨架不一致的问题，统一为 `whitelist.apps` / `whitelist.commands` / `whitelist.paths` |
| 修订 5 | 新增第 2.1 节，支持通过 `base_url` 接入第三方 LLM 中转（仅限 Anthropic 协议兼容，不做多厂商 SDK 抽象）；`config.yaml` 的 `llm` 块新增 `base_url` 字段；新增第 8.9 节中转站坑说明（重点：中转对 tool use 的兼容性需提前单独验证，不要等 Step 3 集成时才发现） |
| 修订 6 | 部分中转站不兼容 Anthropic 协议，只提供 OpenAI 兼容端点，推翻修订 5 中"不做多厂商 SDK 抽象"的决定；第 2.1 节重写为多 provider 抽象架构：新增 `nlu/llm_client.py` 抽象基类 + `nlu/providers/` 下 Anthropic / OpenAI 两个实现，`intent_parser.py` 只依赖抽象接口；`config.yaml` 的 `llm.provider` 字段决定实例化哪个 provider；Step 3 验收标准改为两个 provider 必须分别独立跑通，不能只测一个；`requirements.txt` 同时装 `anthropic` 和 `openai` 两个 SDK |
| 修订 7 | 第 8.10 节补充多 provider 对 exe 打包体积的影响：两个 provider 文件同时存在会导致 `anthropic` 和 `openai` 两个 SDK 都被 PyInstaller 打进最终 exe，与 `config.yaml` 里实际选用哪个 provider 无关；决策（是否精简单一 provider 打包）留到打包阶段再做，MVP 阶段不处理 |
| 修订 8 | Step 2 实测触发升级条件：`base` 模型（150MB）对安静环境下清晰中文指令 CER 远超 10%（10 句仅 1 句完全正确），按 CLAUDE.md §2 升级触发条件切换到 `small`（500MB）；ASR 默认模型从 `base` 改为 `small`，config.yaml 同步更新；新增 ASR 后处理层：引入 `opencc-python-reimplemented`（t2s 模式字符级繁→简转换），解决 Whisper 识别大陆普通话时输出繁体字符的问题（如「打開瀏覽器」→「打开浏览器」）；选 t2s 而非 tw2sp 的原因：tw2sp 词组级映射会把大陆词「退出程序」错误转换为「退出进程」，t2s 只做字形映射不改词汇；技术栈表格补充 opencc 行 |
| 修订 9 | Step 6 单向串行主循环实现完成。新增 `core/state.py`（State 枚举 IDLE/LISTEN/ASR/EXECUTE/TTS/AWAIT_CONFIRM + LoopState 容器 + PendingConfirmation 上下文）和 `core/event_loop.py`（MainLoop 类，分阶段处理 `_phase_listen`/`_phase_asr`/`_phase_execute`/`_phase_tts`/`_phase_await_confirm`）；ASR/LLM/executor 同步阻塞调用统一用 `asyncio.to_thread` 包装避免阻塞事件循环；二次确认流程：`needs_confirmation=True` → 保存 PendingConfirmation → TTS 播报"确定要...吗？" → AWAIT_CONFIRM → ConfirmIntent 用 `confirmed=True` 重调 execute，CancelIntent 视为放弃，用户改主意则按新意图走；退出机制：`ExitProgramIntent` → `should_exit=True` → 主循环 break；错误兜底：单轮异常不崩主循环，播报"出错了，请再说一次"后回 LISTEN；新增 `tests/test_main_loop.py` 含 MockMainLoop + PureStateMachineLoop 两层测试（5 场景全通过） |
| 修订 10 | Step 7 全双工打断实现完成。`audio/vad.py` 新增 `process_chunk_interrupt(chunk)` 方法 + 独立的 `_interrupt_confirm_counter` 计数器：不等完整 segment 结束，连续 `speech_confirm_frames` 帧概率超 `interrupt_threshold_*` 即返回 True，响应延迟约 64ms（2 帧 × 32ms）；新增 `core/interrupt_coordinator.py` 的 `InterruptCoordinator` 类：TTS 期间通过 `asyncio.create_task(monitor())` 并发监听音频队列，检测到打断立即 `player.stop()`（sd.stop 让 sd.wait 返回，TTS 协程结束），TTS 结束后 cancel monitor；`_phase_tts` 重写为并发模型 + 打断处理分支：drain_queue 清空带 TTS 泄漏的剩余音频块（避免回 LISTEN 后误触发）、vad.reset()、若 `state.pending` 存在视为取消待确认操作；新增 `tests/test_interrupt.py` 12 个测试用例全通过（含 monitor 检测/正常完成/打断取消 pending/exit 保留等）；边界正确性：should_exit 的退出意图被打断 → `exit_requested` 保留不强制退出，回 LISTEN |
| 修订 11 | Step 8 加固完成，§6 强制的打断事件审计日志落地。`core/event_loop.py` 新增 `_log_interrupt_event` 方法：打断事件单独写 `logs/actions.log`，记录 `event=interrupt` + `before_state` + `drained_chunks` + `cancelled_pending`（含 intent + raw_text）+ `result_being_played`；executor 的 `_log_action` 原有的 `interrupted`/`interrupt_detail` 预留字段保留（执行动作本身的审计与打断事件审计是两条独立记录，前者由 executor 写、后者由 MainLoop 写，排查时按时间戳关联）；异常处理加固：ASR 调用失败 → `action_type=asr_error`、LLM 解析失败 → `action_type=intent_error`、executor 异常 → 兜底成失败结果，各阶段独立捕获单点故障不废整轮；主循环关键路径 INFO 日志补全（ASR 文本+置信度+耗时、EXECUTE intent+success+needs_confirm+should_exit）；config.yaml 加打断阈值调优指南注释（症状→方向 + 耳机/外放/笔记本内置麦克风三档实测建议 + 调优依据指向 `VAD interrupt triggered` 日志行）；新增 `tests/test_stability.py` 3 个稳定性测试：30 轮连续运行 0 崩溃 + RSS 增长 0 KB、20 次连续打断 0 状态泄漏、50+ 条混合指令长链正确收敛退出；`tests/test_main_loop.py` 修复 PureStateMachineLoop 防死循环上限（动态按输入数 × 8 计算，避免长链场景被误杀）；§3 目录树已有 `core/interrupt_coordinator.py`/`core/state.py`，但 `core/logger.py` 实际未独立实现（日志配置在 `main.py` 的 `setup_logging` 函数里），目录树描述与代码不一致留待后续修订 |
| 修订 12 | Live 模式实测修复 + 外放自激已知限制记录。修两处 live 才暴露的 bug：(1) `core/event_loop.py` 第 88 行 `self._tts = tts_engine` 命名与调用点 `self._tts_engine` 不一致导致 `_phase_tts` 阶段 AttributeError，统一为 `self._tts_engine`；(2) config.yaml `llm.base_url` 为空串导致中转站 key 打到官方端点 401，改为 `https://cdn.keeps.cc` 并验证 SDK 实例 `base_url` 生效。config.yaml 补 `llm.timeout: 30`（中转站比官方慢，原默认 15s 单次不够）。外放场景 TTS 自激实测（2026-07-13）：干净静音对照测试显示 silero-vad 对 TTS 泄漏声的语音概率稳定 0.88-0.97，远超阈值，调 `interrupt_threshold_speaker` 0.8→0.85 + `speech_confirm_frames` 2→3 仍无效，确认调阈值方案不可行；§8.4 补充实测结论，延后事项 AEC 条目补充实测依据，外放全双工打断标记为 MVP 已知限制，建议用耳机模式验证核心功能 |
| 修订 13 | 代码审查发现两处问题并修复（2026-07-13）。(1) **打断退出播报的行为矛盾**：修订 10 表述"exit_requested 保留不强制退出，回 LISTEN"有歧义且与真实代码矛盾——`_phase_execute` 在 TTS 前已置 `exit_requested=True`，打断分支不清除该标志，主循环 `while not exit_requested` 下一轮直接 break，用户打断"再见"程序照样退出（mock 测试无外层 while，覆盖不到此链路）。决策：**打断退出播报 = 取消退出**，打断分支清掉 `exit_requested`，§6 打断边界新增此规则，以 §6 为准；审计日志打断事件新增 `cancelled_exit` 字段。(2) `core/event_loop.py` 第 475 行类型标注用了 `PendingConfirmation` 但未 import，仅因 `from __future__ import annotations` 不炸，补 import。测试同步：`tests/test_interrupt.py` 的 MainLoopForTest 同步取消退出逻辑，新增"打断退出播报 → exit_requested 被清除、回 LISTEN"用例。遗留待办（Step 8 补充项）：真实 MainLoop 的分阶段方法零测试覆盖（现有测试全是 mock 平行实现），`_tts` typo 和本次问题 (1) 均属此盲区，建议用构造函数注入 stub 模块 + 预制音频队列跑真实 `_main_loop` 的集成冒烟测试补上 |
| 修订 14 | Live 实测（2026-07-13）暴露退出流程 bug：说"退出程序"后**"再见"确认音从未播报**，程序在 EXECUTE 完成的同一秒直接退出（日志显示 EXECUTE exit_program 与主循环退出时间戳相同，中间无任何 TTS 活动）。根因：`_phase_execute` 在切到 TTS 阶段**之前**就置 `exit_requested=True`，而主循环 `while not exit_requested` 在阶段之间检查条件，本轮迭代结束即 break，`_phase_tts` 永远执行不到——这同时导致修订 13 的"打断取消退出"在真实主循环里不可达（要打断的播报本身不存在）。修订 13 只修了打断分支清标志位，没发现置位时机本身就错，属同一 mock 盲区（mock 测试直接调 `_phase_tts`，无外层 while 条件竞争）。修复：**`exit_requested` 置位时机从 EXECUTE 阶段挪到 TTS 正常播完之后**——`_phase_execute` 和 `_handle_confirm_response` 不再置位，`_phase_tts` 正常播完后按 `result.should_exit` 置位；打断分支改按 `result.should_exit` 判定 `cancelled_exit`（标志位从未置上，无需清除），审计日志 `cancelled_exit` 字段语义不变。§6 新增"exit_requested 置位时机"条目。测试同步：`tests/test_interrupt.py` MainLoopForTest 平行实现 + 两个 exit 相关用例改为验证新时序（正常播完才置位 / 打断则从未置位）；`tests/test_main_loop.py` MockMainLoop 与 PureStateMachineLoop 同步挪置位时机（EXPECTED_TRANSITIONS 状态序列不变，TTS 阶段体从"被跳过"变为真正执行）。修订 13 遗留的真实 MainLoop 集成冒烟测试待办仍未做，本 bug 再次佐证其必要性 |
| 修订 15 | 打开应用放开为全机已安装应用（用户需求：不想被限死在白名单里）。§6 白名单机制重写：**打开非破坏性，放开；关闭/命令/文件路径仍 100% 白名单强制**。新增 `executor/app_index.py`（`AppIndex` 类）：启动时扫描开始菜单 `.lnk`（`%ProgramData%` + `%AppData%` 下 `Start Menu\Programs` 递归）+ 注册表 App Paths（HKLM/HKCU），建"应用名 → 启动目标"索引；匹配确定性（精确 → 唯一子串，命中多个拒绝并列候选，命中零个播报没找到），排除 uninstall/卸载 类快捷方式防误命中。`executor/app_control.py` 的 `open_app` 改为：先查 `whitelist.apps`（用户自定义别名优先），未命中再查 `AppIndex`，都没有才拒绝；`close_app` 逻辑不变（仍只认 `whitelist.apps`）。`config.yaml` 新增 `whitelist.allow_open_any_app`（默认 true，false 恢复旧行为）。审计日志 open_app 记录 `resolved_via`（whitelist/index）+ 实际启动路径。`nlu/schema.py` 的 open_app 意图描述更新：`app_name` 不再要求是白名单别名，可为任意应用名（用户口语说什么填什么），路径解析交给执行层。§3 目录树加 `app_index.py`。测试：新增 `tests/test_app_index.py`（索引构建 + 匹配规则单测），`tests/test_executor.py` 的 open_app 用例调整（"白名单外应用被拦截"改为"白名单外但已安装应用可打开 / 完全不存在的应用才拒绝"）。关闭仍走白名单+二次确认，边界不变 |
| 修订 16 | 新增本地管理控制台 `webui/`（用户需求：图形界面里注册"应用名 → exe 路径"给语音助手用）。§4 延后项"图形界面"松动一条口子：允许一个**仅 127.0.0.1** 的本地管理控制台，定位是维护工具而非交互通道——语音仍是唯一主通道，主循环不依赖 webui，不启动它不影响任何语音功能。`webui/server.py` 用 stdlib `http.server`（不引 Flask/FastAPI，零新依赖），提供接口：列出/新增/删除用户别名、测试应用名解析（复用 `AppIndex.resolve` + `whitelist`）、打开应用（复用 `Executor.execute`，与语音同一审计路径）、读取审计日志尾部。别名写入独立的 `user_apps.yaml`（不是 config.yaml——后者有大量手写注释，机器写入会毁注释），`Executor.from_config` 启动时合并进 `whitelist.apps`，config.yaml 同名优先。前端 `webui/static/index.html` 单文件无构建步骤。**安全边界**：控制台只能新增别名和触发"打开"（非破坏性）；不暴露关闭应用/删文件/跑命令等任何危险操作（这些仍只走语音 + 二次确认）；服务绑定 127.0.0.1 拒绝外部访问。§3 目录树加 `webui/` 和 `user_apps.yaml`。新增 `tests/test_webui.py`（别名增删 + 合并优先级 + 解析接口）。**遗留待办承接**：修订 13/14 提的真实 MainLoop 集成冒烟测试仍未做，本次不涉及 |
| 修订 17 | 控制台启动方式补充（用户需求：不用敲命令 / 打成 exe）。新增 `webui.bat`（激活 venv → 起 `webui.server` → 2 秒后自动开浏览器，关窗即停），零构建零新依赖，日常开发用。新增 `build_console.bat` + `webui/server.py` 的 frozen 适配：PyInstaller `--onefile` 打包为 `dist/voxctl-console.exe`（约 20-40MB，不含 whisper/torch 大件，不踩 §8.10 模型体积坑）。frozen 模式下 `server.py` 用 `sys._MEIPASS/webui_static` 读打包进去的 index.html，用 `sys.executable` 所在目录作项目根（`os.chdir` 过去）读写 `config.yaml`/`user_apps.yaml`/`logs/`——**exe 仍必须放在项目根目录运行**，因为这些运行时文件不打进 exe（config 要用户可改、user_apps 要可写、logs 是主程序写的），"独立分发"不成立，exe 只是"免敲命令启动"的等价物。PyInstaller 不进 requirements.txt（打包期工具，非运行期依赖），build_console.bat 里按需 `pip install`。§3 目录树加两个 bat |
| 修订 18 | 语音主程序打包为独立 exe（用户需求：拷到没装 Python 的电脑上跑，不是 webui 那种双击开网页）。§8.10 从"延后事项"改写为落地方案。关键决策：**只能 onedir 不能 onefile**（打包本体约 730MB——torch_cpu.dll 291MB + ctranslate2.dll 57MB 等，CPU torch；onefile 每次启动都要把这几百 MB 重新解压到临时目录拖慢启动；onedir 产出 `dist/voxctl/voxctl.exe` + `_internal/`，拷**整个文件夹**到目标机秒级启动）；**native/data 依赖显式 collect**（torch/ctranslate2/faster_whisper/silero_vad 的 `.jit`/opencc 字典/sounddevice 的 PortAudio DLL/anthropic/openai，两个 provider 加 `--hidden-import` 兜底，全写进 `build_voice.bat`；sounddevice 是单文件模块 `--collect-all` 抓不到 DLL，靠 PyInstaller 自带 `hook-sounddevice.py` 收 `_sounddevice_data`）；**whisper 模型运行时下载不打进包**（small int8 ~500MB，目标机首次运行需联网从 HF 拉取，真离线留作 `--add-data` 增强项）；**config.yaml/user_apps.yaml/logs 不打进 exe**（`main.py` frozen 模式 `os.chdir` 到 exe 所在目录按相对路径读写，`build_voice.bat` 打包后把 `config.yaml` 拷到 `dist/voxctl/` 旁边）；**API key 仍只从环境变量读**（§6 铁律，目标机需 `setx ANTHROPIC_API_KEY`，不写 config 明文）；**console 模式**（保留窗口显示识别文本/日志，与原 PowerShell 体验一致）。新增 `build_voice.bat`，`main.py` 加 frozen chdir 适配，§3 目录树加 `build_voice.bat`。**实测（2026-07-14）**：打包产物 734MB，frozen 冒烟测试通过——torch/silero-vad/faster-whisper/ctranslate2/opencc/PortAudio 录放/anthropic SDK/edge-tts/config 路径全部在 frozen 下真实跑通（用假 key 触发的 401 是预期，主循环优雅接住未崩）。修订 13/14 遗留的真实 MainLoop 集成冒烟测试仍未做 |
| 修订 19 | 语音助手做成 PyWebView 原生 GUI 控制台（用户需求：不要黑终端窗口、不要网页版，把 webui 融进 exe 首页 + 加控制台分页改 LLM 配置和启停语音）。**Step 0 可行性门先过**：pywebview 6.2.1 + pythonnet 3.1.0 在 Python 3.14.6 上装成功、原生窗口真实渲染并干净关闭。关键决策：**PyWebView + js_api 桥**（无浏览器 / 无 localhost，复用 `webui/static/index.html`，`fetch` 改 `pywebview.api`）；**统一入口 `app.py`**（默认起 GUI，`--voice` 委托 `main.main()` 跑语音；GUI 用 `subprocess.Popen([sys.executable,'--voice'])` 启停语音，语音仍独立进程不依赖 GUI，崩溃不拖垮 GUI）；**LLM 设置存 `voxctl.local.yaml`**（gitignore 机器写入，覆盖 `llm.{provider,model,base_url,api_key}`，不碰 config.yaml 手写注释，同 user_apps.yaml 分家逻辑）；**api_key 明文存本地文件**（用户在 AskUserQuestion 明确选择明文，落实为独立 gitignore 文件而非污染 config.yaml，§2/§6 记录放宽）+ `core/local_settings.py` 启动注入到环境变量，**intent_parser 零改动**（仍读 env var）；**打包 `--windowed`**（无黑窗），语音子进程 `CREATE_NO_WINDOW` 隐藏、日志回显到窗口。安全不变量保持：GUI 只暴露非破坏性（设置 / 启停 / 别名 / 打开），关闭 / 删除 / 命令仍只走语音 + 二次确认。新增 `app.py` / `webui/gui.py` / `core/local_settings.py` / `voxctl.local.yaml` / `.gitignore` / `build_gui.bat`，requirements 加 pywebview；`core/event_loop.py` 的 `from_config_file` 叠加本地覆盖、`main.py` `setup_logging` 兼容 windowless（`sys.stdout` 为 None 跳过 StreamHandler）；`index.html` 加 tab（首页 / 控制台）+ 控制台面板 + `fetch`→`pywebview.api`。webui 浏览器版（`server.py` / `webui.bat` / `build_console.bat`）保留不动。**实测（2026-07-14）全链路通过**：(1) 源码 GUI 真窗口冒烟——PyWebView 窗口开、桥注入、11 个 js_api 方法全暴露、`get_settings()` 经桥真实返回 config.yaml 生效值、`voice_status()` 返回未运行；(2) `core/local_settings` 进程内往返——`save_local_llm` 写 `voxctl.local.yaml`（含明文 key）→ `effective_llm_settings` 报告 has_local_key/overridden → `apply_local_llm_overrides` 把 provider/model/base_url 叠进 config 并将 api_key 注入 `os.environ[api_key_env]`（intent_parser 读取路径不变）；(3) `build_gui.bat` 打包——`dist/voxctl/voxctl.exe`（45MB stub + `_internal` 694MB ≈ 740MB），PyWebView 原生栈 webview/clr_loader/pythonnet/bottle 全进 `_internal`；(4) frozen 双角色冒烟——默认 GUI 角色：窗口开、`_internal/webui_static/index.html` 正确加载、前端经桥调 `get_state()` 触发 ConsoleState/AppIndex 构建（201 条），无缺依赖崩溃；`--voice` 角色（voice_start 拉起的同一 exe）：app.py 路由 → main.main() → 音频设备检测 → `core.event_loop` output mode 初始化，torch/whisper/audio 栈全加载无 import 错误。torch.distributed._sharding_spec 的 hidden-import ERROR 是 CPU torch 缺可选分布式模块，无害（build_voice 同现象）。修订 13/14 遗留的真实 MainLoop 集成冒烟测试仍未做 |
| 修订 20 | GUI 语音开关"神秘自动关闭"修复（用户 live 实测暴露）。**现象**：点启动后短暂显示"运行中" → 用不了 → 一段时间后开关自动拨回"已停止"，GUI 零提示。**根因**：`--voice` 子进程初始化时因 `ANTHROPIC_API_KEY` 空/未设直接崩溃退出（`voxctl.local.yaml` 未存 api_key 且系统 env 未设，[nlu/intent_parser.py](nlu/intent_parser.py) `from_config` 抛 `RuntimeError` → [main.py](main.py) `return 1` → 进程退出），但 `voice_start` 的 `Popen` 一成功就返回 `running:true`（进程确实起来了，崩在 1-2s 后加载 vad/whisper 之后），且 Popen 继承了 windowed GUI 的 devnull stdio，子进程 traceback 只落 `logs/voxctl.log`、GUI 一无所知，只靠 3s 轮询默默把开关拨回。属"启动成功≠初始化成功"的失败反馈缺失，任何启动期异常（缺 key / 换错 base_url / 设备问题）都会复现。**修复**（`webui/gui.py` + `webui/static/index.html`，§6 GUI 边界补"启停必须给出失败反馈"条）：(1) **启动前预检 API Key**——`voice_start` 先经 `effective_llm_settings` 查本地文件或 env 有无可用 key，没有直接返回明确错误"未配置 API Key，请在上方填写并保存或设置环境变量 X"，不白起必崩进程；(2) **子进程 stdout/stderr 重定向到 `logs/voxctl-voice.out`**，捕获连自身日志都没配好的早期崩溃；(3) **启动后短等 ~2.5s 探活**，若立即退出（源码模式快速崩）则回读日志尾部异常行、把原因随 `ok:false` 回传前端 toast；(4) **`voice_status` 区分主动停 vs 意外退出**——新增 `_voice_user_stopped` 标志，轮询发现进程非预期死亡返回 `crashed:true` + 原因，前端 `pollVoice` toast 一次（覆盖 frozen 导入慢、崩在探活窗口之后的场景）；前端 `toast` 加 `ms` 时长参数，语音类错误延长到 8s 便于阅读。安全不变量不变：仍只暴露非破坏性操作，key 仍不进 config.yaml。**实测（2026-07-15）**：源码模式空 key 点启动 → 预检当场拒绝并 toast"未配置 API Key"，不再起进程；填入有效 key 后启停正常。修订 13/14 遗留的真实 MainLoop 集成冒烟测试仍未做 |
| 修订 21 | VAD 改自研 onnx 运行时，砍掉 torch 依赖瘦身打包（用户需求：为什么 exe 有 770MB / 能不能变小）。**背景**：拆包发现 exe 体积大头是代码库不是模型——torch 470MB（`torch_cpu.dll` 291MB）居首，而 whisper 模型早已是运行时下载（不在 exe 里），torch 在本项目**唯一用途就是跑 silero-vad**（faster-whisper 走 ctranslate2、不碰 torch）。**关键发现**：silero-vad 官方包深度绑 torch——`model.py`/`utils_vad.py` 顶部 `import torch, torchaudio`，连 `OnnxWrapper` 都用 torch 张量管状态，走 `load_silero_vad(onnx=True)` 照样拖入 torch，白搭。**决策**：绕开整个 `silero_vad` 包，vendor 2.3MB 的 `silero_vad.onnx` 进仓库（`audio/silero_vad.onnx`），运行时用 `onnxruntime`+`numpy` 直接喂模型，自行复刻 `OnnxWrapper` 状态机（16kHz：512 samples/帧、context 64、state `(2,1,128)` float32，逐帧 `session.run` + 滚动 state/context）。**可行性门先过（2026-07-15）**：纯 numpy onnx 与原 jit 在同一音频流逐帧概率一致（最大差 1.4e-5，纯浮点噪声），真·drop-in，检测行为零变化。改动：重写 `audio/vad.py`（去 `import torch`/`from silero_vad`，改 onnxruntime；新增 frozen 感知的 onnx 路径解析 `sys._MEIPASS`/`__file__`）；`requirements.txt` 去 silero-vad 加 onnxruntime；`build_gui.bat`+`build_voice.bat` 去 `--collect-all torch`/`--collect-data silero_vad`、加 `--add-data audio\silero_vad.onnx` + `--exclude-module torch --exclude-module torchaudio`；§2 VAD 选型 + 技术栈表 + §8.10 collect 清单同步。收益：去掉 torch+torchaudio，exe 从 ~770MB 预期降到 ~300MB，且**仍离线**（onnx 随包走）。安全/功能不变量：VAD 阈值/状态机/打断逻辑一律不动，只换底层推理引擎。回归 pytest 全绿（torch.jit on 3.14 的 DeprecationWarning 一并消失）。**exe 重打包留给用户按需触发**（用户先不急着打包）。修订 13/14 遗留的真实 MainLoop 集成冒烟测试仍未做 |
| 修订 22 | GUI 控制台视觉重做为亮色毛玻璃风格（用户需求：采用新设计稿视觉，暂不重打包 exe）。设计流程：先出独立设计稿 `design/settings-mockup.html`（Tailwind CDN 原型，山景暮色渐变背景 + 毛玻璃侧栏 + 白色圆角卡片），用户确认采用后重写 `webui/static/index.html` 的**视觉层**。关键约束：(1) **js_api 桥与全部 11 个方法调用零改动**——只换 CSS 和 DOM 结构，`window.pywebview.api.*` 调用、轮询、toast、修订 20 的崩溃反馈逻辑原样保留；(2) **生产界面离线自足**——设计稿里的 Tailwind CDN 不进 `webui/static/`，新视觉用手写 CSS 实现（frozen exe 无网也要能渲染），Google Fonts 引用一并去掉改系统字体栈；(3) 布局从"琥珀色终端风（tabbar 分页）"改为"左侧毛玻璃导航栏（260px，橙色渐变胶囊选中态）+ 右侧白色圆角卡片"，原有功能面板按导航项归组（语音控制 / 大模型配置 / 应用别名 / 审计日志），服务状态灯移到侧栏底部；(4) 配色体系：主色橙 #FF6B35（导航选中）、强调蓝 #2563EB（主按钮/数值）、成功绿（状态点），背景纯 CSS 多层渐变模拟山景暮色（不用图片资产）。新增 §3 `design/` 目录约定（mockup 纯参考物，不打包不 import，CDN 仅限设计稿）。webui 浏览器版 `server.py` 直接吃同一份 index.html（`_serve_index` 原样读文件），浏览器场景下 pywebview 桥不存在、API() 返回 null 各函数静默跳过——与改版前行为一致。exe 重打包留给用户按需触发。修订 13/14 遗留的真实 MainLoop 集成冒烟测试仍未做 |
| 修订 23 | GUI 控制台补齐三页表单（用户需求：大模型配置页要有 LLM / ASR / TTS 三个模型选择框，应用别名和审计日志页沿用原有控件、只换新视觉）。改动：(1) **本地覆盖从"仅 llm"扩展到 asr/tts**——`core/local_settings.py` 新增 `asr.model_size`（tiny/base/small/medium/large-v3）和 `tts.voice`（Edge 中文音色）两类覆盖，与 llm 覆盖同一套"写 voxctl.local.yaml → apply_local_*_overrides 叠进 config"机制；`apply_local_llm_overrides` 更名保留兼容别名，实际由新的 `apply_local_overrides` 统一处理三块（llm/asr/tts），`event_loop.from_config_file` 调用点不变（仍在 yaml load 后、from_config 前叠加）。(2) **`webui/gui.py` `get_settings` / `save_settings` 扩展**：返回值加 `asr_model` / `tts_voice` 生效值 + 各自 `overridden` 标记，保存接受两个新参数；语音子进程读取路径不变（asr/tts 覆盖同样在子进程 `from_config_file` 时从本地文件叠加，与 llm 一致）。(3) **`index.html` 大模型配置页**加 LLM 模型 / ASR 模型 / TTS 音色三个选择框（模型选项是常见值下拉 + LLM model 保留自由文本因中转站别名不固定），应用别名页（注册 / 解析打开 / 别名表）和审计日志页沿用修订 22 的控件与逻辑，仅确认在新视觉下渲染正确。安全不变量不变：GUI 仍只暴露非破坏性操作，api_key 仍不进 config.yaml；asr/tts 覆盖是纯配置项、非破坏性。**首次打包实测（2026-07-15）**：`build_gui.bat` 产出 `dist\voxctl` 共 **271MB**（rev21 去 torch 后从 ~740MB 降约 470MB，达成 ~300MB 目标），无 torch 本体残留（仅几个 0MB 的 onnxruntime 子模块 py stub 名字含 torch，无害）；frozen 双角色冒烟通过——GUI 角色弹窗 + 界面从 `_internal/webui_static/index.html`（rev22/23 新毛玻璃视觉）加载 + AppIndex 201 条经桥构建；`--voice` 角色 onnxruntime 从 `_internal/silero_vad.onnx` 加载 VAD 成功（rev21 onnx drop-in 在 frozen 下证成）+ whisper/ctranslate2/opencc/audio 栈无缺依赖，最终缺 `ANTHROPIC_API_KEY` 优雅退出（预期，非打包问题，同 rev20 失败反馈路径）。修订 13/14 遗留的真实 MainLoop 集成冒烟测试仍未做 |

| 修订 24 | 删掉 webui 浏览器版，只保留 PyWebView GUI 版（用户需求：不需要 webui 网页版）。背景：修订 16 的浏览器版（stdlib `http.server`）和修订 19 的 GUI 版长期并存，GUI 版已完全覆盖浏览器版全部功能（别名管理 / 解析 / 打开 / 审计日志 + 额外的 LLM/ASR/TTS 设置 + 启停语音），两版并存是纯重复维护。改动：(1) **删 `webui/server.py`**——它是两层混装：HTTP 服务层（`ConsoleHandler` / `do_GET` / `do_POST` / `main` / `serve_forever`，浏览器专属）+ 共享底层（`ConsoleState` / `read_user_apps` / `write_user_apps` / `validate_alias` / frozen 路径解析 `_PROJECT_ROOT`/`_STATIC_DIR`/`_AUDIT_LOG`），后者被 `webui/gui.py` import 复用。删除时把共享底层**内联进 `webui/gui.py`**（唯一消费者），HTTP 层整体丢弃。(2) **删 `webui.bat`**（浏览器版一键启动）和 **`build_console.bat`**（浏览器版 onefile 打包）。(3) **`tests/test_webui.py`**：删掉"3. HTTP 接口（真实起服务打 localhost）"整个 `TestWebuiEndpoints` 类（浏览器专属）；纯函数测试 `validate_alias` / `read/write_user_apps` 的 import 从 `webui.server` 改为 `webui.gui`；`TestUserAppsMerge`（executor 层合并优先级）不动。安全不变量不变：GUI 仍只暴露非破坏性操作，边界与修订 19 一致；去掉浏览器版反而消掉了"服务绑 127.0.0.1 但仍是开放端口"这一面。§3 目录树、§4 图形界面延后项、§6 GUI 边界、§8.10 打包说明同步更新。exe 重打包（`build_gui.bat` 不受影响）留给用户按需触发。修订 13/14 遗留的真实 MainLoop 集成冒烟测试仍未做 |
| 修订 25 | 清理死文件 + 真正实现 `core/logger.py` + 补 git 版本控制（用户需求：清理死文件、修正文档与代码不一致、核实修订 24 是否彻底）。**先核实修订 24 彻底落地**（`webui/server.py`/`webui.bat`/`build_console.bat` 确已删、`test_webui.py` import 已改自 `webui.gui`、§3/§4/§6/§8.10 描述已同步），文档与代码一致，无"文档说做了代码没做"的情况。改动：(1) **真正建 `core/logger.py`，消除重复日志配置**——背景：修订 11 起每次修订都补记"目录树写了 `core/logger.py` 但实际未独立实现（日志逻辑在 `main.py` 的 `setup_logging`）"，拖了 13 个修订的技术债这次闭环。依据：`main.py` 的 `setup_logging()` 与 `webui/gui.py` 的 `_setup_gui_logging()` 结构**完全同构**——都是建 logs 目录 → 取 root logger + setLevel → RotatingFileHandler(UTF-8, 同一 Formatter) → `sys.stdout is not None` 才加 StreamHandler（修订 19 windowless 保护），只有文件名/级别/大小参数不同。`core/logger.py` 提供 `configure_root_logging(log_path, file_level, console_level, max_bytes, backup_count)` 收敛这套逻辑；`main.py`（voxctl.log/DEBUG/10MB×3）和 `webui/gui.py`（voxctl-gui.log/INFO/5MB×2）改为传参调用，两处原有的 handler/Formatter 构造代码**完全删除**（不留残留，否则改 `core/logger.py` 不生效）。(2) **删死文件**：`main.spec`（早期 `python main.py` 的 onefile spec，已被 `build_voice.bat` 取代，两个 bat 均不引用，`.gitignore` 本就忽略 `*.spec`）；`voxctl-console.exe`（根目录 + `dist/` 各一份，修订 24 删浏览器版源码后的历史遗留产物，无代码依赖）。(3) **清理文档/注释残留**：`webui/__init__.py` docstring 的"仅 127.0.0.1"（浏览器版 HTTP 服务描述，GUI 版走 pywebview js_api 桥无 localhost）改为准确说法；`.gitignore` 删 `*.jit`/`*.ckpt` 及"silero-vad 模型缓存（torch hub 缓存目录）"注释（修订 21 砍 torch、VAD 改 onnx 直跑后的残留）。(4) **项目补 git 版本控制**：此前不是 git 仓库、任何删除不可逆，本次 `git init` 并在清理前打基线快照 commit，敏感文件 `voxctl.local.yaml`（含明文 key）/`user_apps.yaml` 经 `.gitignore` 正确排除不入库。安全不变量不变：仅重构日志配置和删历史产物，不碰白名单/二次确认/key 读取路径（仍 env var）/GUI 非破坏性边界。§3 目录树 `core/logger.py`/`main.py`/`webui/gui.py` 三条同步。修订 13/14 遗留的真实 MainLoop 集成冒烟测试仍未做 |
