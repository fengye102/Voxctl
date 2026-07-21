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
| TTS | edge-tts + pyttsx3 本地后备 | edge-tts 免费中文自然需联网；pyttsx3（SAPI5）作断网时本地后备，无需联网（修订 34）。三级降级：流式播放 → 一次性合成 → pyttsx3 |
| API Key 存储 | keyring（OS 密钥管理器） | WinCred on Windows；替代 yaml 明文存 api_key，启动时从 keyring 读出注入环境变量（修订 34）。keyring 不可用时 fallback 到 yaml 旧值（迁移兼容） |
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
Voxctl/
├── CLAUDE.md                  # 本文件
├── CHANGELOG.md               # 变更记录（原 §9，修订 40 拆出；每次修订在此追加）
├── README.md                  # 项目文件地图
├── requirements.txt           # 依赖清单（宽松基线版本）
├── config.yaml                # 主配置（用户可改，含手写注释）：audio/vad/asr/llm/tts/ui/whitelist
├── .gitignore                 # 忽略规则（venv/logs/打包产物/voxctl.local.yaml/user_apps.yaml）
├── run.bat                    # 一键启动语音：设 UTF-8 代码页 → 激活 venv → python main.py
├── main.py                    # 语音主循环入口；日志配置调 core/logger.py 的 configure_root_logging()（修订 25）
├── app.py                     # 统一入口（修订 19）：默认起 GUI 控制台，`--voice` 跑语音；含 frozen stdout 重定向（修订 28）
│
├── audio/
│   ├── __init__.py
│   ├── recorder.py            # sounddevice 录音封装
│   ├── player.py              # sounddevice 播放封装：play() 一次性 + play_stream() 流式 + stop_callback 机制（修订 34）
│   ├── vad.py                 # VAD 封装，判断人声起止（含打断判定逻辑）；onnxruntime 直跑 onnx（修订 21）
│   ├── silero_vad.onnx        # vendor 的 silero-vad 模型（2.3MB，打包资产，修订 21）
│   └── output_mode.py         # 耳机/外放自动检测 + 手动覆盖
│
├── asr/
│   ├── __init__.py
│   └── whisper_engine.py      # faster-whisper 封装
│
├── nlu/
│   ├── __init__.py
│   ├── llm_client.py          # 抽象基类 LLMClient：统一接口 parse_intent() + _resolve_name() 公共逻辑 + 异常层级，见第 2.1 节
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── anthropic_provider.py  # AnthropicClient(LLMClient)，anthropic SDK + tool use，复用基类 _resolve_name()
│   │   └── openai_provider.py     # OpenAIClient(LLMClient)，openai SDK + function calling，复用基类 _resolve_name()
│   ├── intent_parser.py       # 按 config 的 provider 字段实例化对应 LLMClient，只依赖抽象接口
│   └── schema.py              # 意图数据结构（pydantic）+ safe_model_dump() 公共函数
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
├── plugins/                    # 插件机制（修订 42）：扩展意图类型，不扩展权限
│   ├── __init__.py             # PluginRegistry：扫描/加载/校验插件，提供 dispatch + tool specs 合并
│   └── base.py                 # PluginSpec 数据类 + PluginError 异常 + 插件开发约束说明
│
├── tts/
│   ├── __init__.py
│   └── edge_tts_engine.py     # Edge TTS 封装 + 流式播放管线 + pyttsx3 本地后备（修订 34）
│
├── ui/                         # 全局热键监听（修订 33/50）：纯 ctypes，非 Windows no-op
│   ├── __init__.py
│   └── hotkey.py               # Win32 RegisterHotKey + daemon 消息循环线程，toggle 语音（修订 33，修订 50 起本目录只剩此文件）
│
├── core/
│   ├── __init__.py
│   ├── event_loop.py           # 主循环：状态机分发 + 各 _phase_* 阶段处理 + 打断处理（不改 state.py）
│   ├── factory.py              # MainLoop 构造工厂：create_from_config_file / create_from_config，从 config 实例化所有子模块（修订 35 从 event_loop.py 拆出）
│   ├── interrupt_coordinator.py # 状态协调器：打断信号判定与分发
│   ├── state.py                 # 全局状态机（空闲/监听/识别/执行/播报/等待确认）
│   ├── logger.py               # 共用日志配置 configure_root_logging()（修订 25）：建 logs 目录 + RotatingFileHandler(UTF-8) + windowless-safe StreamHandler
│   ├── local_settings.py       # voxctl.local.yaml 读写 + LLM/ASR/TTS 覆盖注入 + API key keyring 存储（修订 19/23/34）
│   ├── audit.py                # 审计日志写入：执行动作 + 打断事件 JSON lines（§6 强制，executor/event_loop 共用）
│   └── version.py              # 版本信息：__version__ + get_version_info()（版本号+git commit hash，修订 39）
│
├── webui/                      # 本地管理控制台（修订 24：只剩 PyWebView GUI 版，浏览器版已删）
│   ├── __init__.py
│   ├── gui.py                  # PyWebView 原生窗口 + js_api；含 ConsoleState / 别名读写 / validate_alias；run_gui() 起热键监听（修订 26/33/50）
│   └── static/
│       └── index.html          # 控制台前端（单文件，无构建步骤，离线自足）
│
├── build_voice.bat             # 语音主程序 exe 打包脚本（PyInstaller onedir → dist/voxctl/，修订 18）
├── build_gui.bat               # GUI 控制台 exe 打包脚本（onedir --windowed，修订 19）
│
├── user_apps.yaml              # webui 维护的用户别名表（机器写入，gitignore；启动时合并进 whitelist.apps，config.yaml 优先）
├── voxctl.local.yaml           # GUI 维护的本地覆盖：llm/asr/tts（机器写入，gitignore，含 api_key 明文，勿分享，修订 19/23）
│
├── brand/                      # 磁盘存在，用途未在本文档正式声明
│   └── logo/
│
├── test_audio/                 # 磁盘存在，用途未在本文档正式声明（不进 git）
│   └── segment_001.wav
│
├── tests/
│   ├── __init__.py
│   ├── README.md               # 三层目录说明（运行方式、CI 收集规则、跨层依赖备注）
│   ├── unit/                   # 单元测试：纯函数/mock，无需真实设备或网络，能进 CI
│   │   ├── __init__.py
│   │   ├── test_app_index.py       # AppIndex 匹配规则（临时目录伪造数据）
│   │   ├── test_interrupt.py       # 打断功能（mock VAD/Player，12 用例）
│   │   ├── test_stability.py       # 稳定性（复用 PureStateMachineLoop，30 轮 0 崩溃 + RSS 检查）
│   │   ├── test_webui.py           # webui 纯函数（validate_alias / user_apps 合并优先级）
│   │   ├── test_hotkey.py          # 热键字符串解析纯函数
│   │   ├── test_local_settings.py  # local_settings 读写/覆盖/keyring（37 例）
│   │   ├── test_event_loop.py      # 真实 MainLoop 单元测试（41 例，含投机 ASR）
│   │   ├── test_vad_adaptive.py    # VAD 自适应阈值单元测试（15 例）
│   │   ├── test_plugins.py         # 插件机制单元测试（24 例，修订 42）
│   │   └── test_mainloop_integration.py  # MainLoop 集成冒烟测试（5 例，驱动真实 _main_loop while 循环，修订 39）
│   ├── acceptance/             # 验收脚本：需真实设备/网络/LLM，手动运行
│   │   ├── __init__.py
│   │   ├── test_recorder_vad.py    # Step 1：录音闭环 + VAD（需麦克风，交互式）
│   │   ├── test_asr.py             # Step 2：ASR 准确率（需麦克风或 wav 文件）
│   │   ├── test_intent_parser.py   # Step 3：意图解析（打真实 LLM，需网络 + API key）
│   │   ├── test_executor.py        # Step 4：执行模块端到端（真实开关 notepad 等）
│   │   ├── test_tts.py             # Step 5：TTS 播报（默认需网络 + 音频设备）
│   │   ├── test_main_loop.py       # Step 6：主循环（默认 --mock 无需设备；--live 需全套）
│   │   └── test_gui_smoke.py       # GUI 窗口冒烟（需 pywebview 真实窗口）
│   └── tools/                  # 诊断/探针脚本，不是测试
│       └── probe_relay.py          # 中转站兼容性探针（换中转前手动跑，§8.9）
│
└── logs/                       # 运行时日志（不进 git）
    ├── actions.log              # 审计日志：执行动作 + 打断事件的 JSON lines（§6 强制）
    ├── voxctl.log               # 语音主程序运行日志（轮转，DEBUG）
    ├── voxctl-gui.log           # GUI 进程日志（windowed 无控制台，日志只进文件）
    └── voxctl-voice.out         # 语音子进程 stdout/stderr 重定向（修订 50 起仅供 GUI _read_voice_failure_reason 回读崩溃原因）
```

### 命名与存放约定

- 配置一律进 `config.yaml`，禁止在代码里硬编码路径、模型名、白名单。
- 每个模块自己的异常，用自定义 Exception 类，不要裸用 `Exception`。
- 日志文件、模型缓存文件不进 git，写入 `.gitignore`。
- 新增执行能力（比如以后加"发邮件"）优先通过插件机制（`plugins/` 下新建 `.py` 文件，暴露 `PLUGIN_SPEC`），无需改动核心代码；仅当能力需要深度集成（新的 executor 子模块 + 白名单类型）时才在 `executor/` 下新建文件并注册进白名单。
- **生产界面（`webui/static/`）必须离线自足**（frozen exe 无网也要能渲染，不引任何 CDN 资源）。

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
  language: zh                   # ASR 识别语言（传给 faster-whisper），zh/en/ja 等；只改此项不影响 TTS/LLM 链路

llm:
  provider: anthropic             # anthropic / openai，决定用哪个 provider 实现，见第 2.1 节
  model: claude-haiku-4-5-20251001  # provider=openai 时填对应模型标识（如 gpt-4o 或中转站别名）
  api_key_env: ANTHROPIC_API_KEY  # 从环境变量读取，不写明文 key；换 provider 时同步换变量名
  base_url: ""                    # 留空 = 走对应 provider 官方端点；非空指向中转站，见第 2.1 节

tts:
  voice: zh-CN-XiaoxiaoNeural     # Edge TTS 中文音色，可替换
  local_fallback: true             # pyttsx3 本地后备：edge-tts 网络异常时自动降级到 SAPI5（修订 34）

ui:
  hotkey: "ctrl+alt+v"            # 全局热键 toggle 语音启停（修订 33）；GUI 可改，覆盖写 voxctl.local.yaml
  hotkey_enabled: true            # 是否注册全局热键（修订 50 起浮窗已删，热键是唯一 toggle 入口之一，另入口是 GUI 控制台按钮）

plugins:
  enabled: true                   # 插件机制总开关（修订 42）；false = 不扫描 plugins/，LLM schema 不含插件意图

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
- **全局热键 = 启停的等价触发（修订 33，修订 50 起浮窗已删）**：按全局键盘热键只是 GUI 启停语音子进程的**新触发方式**，走同一套 `toggle_voice`（内部复用修订 20 的启停逻辑与四道防线），不新增任何能力面——不暴露关闭应用 / 删文件 / 跑命令。热键**启动失败**只写日志，详细原因仍需开 GUI 看（GUI 按钮路径的失败反馈不变）。热键用纯 ctypes `RegisterHotKey`，热键被占用要回报错误不静默。
- **插件安全边界（修订 42，强制）**：插件可以注册新意图类型和 handler，但**不能扩展权限**：
  - 插件 handler 返回的 `ExecutionResult` 走与内置意图相同的 TTS 模板播报链路
  - 插件 handler **禁止直接调用 `subprocess`、`os.system`、`os.popen`**——这不是技术上做不到，而是规范约束；如果插件需要执行系统命令，必须通过 executor 暴露的受控接口（如调已注册的白名单命令），这些接口自身有白名单校验
  - 插件 handler 的异常不能导致主循环崩溃——`PluginRegistry.dispatch()` 内部 catch 插件异常，转为失败的 `ExecutionResult`
  - `plugins/` 目录下的 `.py` 文件在启动时静态加载（`import` 模块 → 读 `PLUGIN_SPEC` 变量），加载失败的插件跳过并记日志，不阻断启动
  - 审计日志记录插件意图的执行（`action_type` = 插件意图名），与内置意图一致

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

全部修订记录（初版至今）已移至 [CHANGELOG.md](CHANGELOG.md)（修订 40 拆分——§9 曾占本文件近半篇幅，历史记录与"当前有效规范"混在一起，拆出后 CLAUDE.md 只保留现行规范）。

**约定不变**：每次修订仍须在 CHANGELOG.md 追加记录（格式沿用原表格）；架构/目录/规范调整仍遵循"先改本文件，再改代码"。
