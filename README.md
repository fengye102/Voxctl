# Voxctl — 全双工语音助手

对着麦克风说话就能控制 Windows：打开/关闭应用、查系统状态、开文件、跑白名单命令，全程不碰键盘。播报期间可被人声打断，转去处理新指令（全双工）。

> **本 README 是"文件地图"，逐个说明每个文件的作用。**架构决策、MVP 边界、安全规则、变更历史以 [CLAUDE.md](CLAUDE.md) 为唯一事实来源；两者冲突时以 CLAUDE.md 为准。

## 数据流一览

```
麦克风 → recorder → VAD 切片 → ASR 转写 → 意图解析(LLM) → executor 执行 → TTS 播报
                     ↑                                                          │
                     └────────── 播报期间检测打断，立即停播回到监听 ────────────┘
```

---

## 根目录

| 文件 | 作用 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | **项目唯一事实来源**。架构图、技术栈、MVP 范围、分步开发计划、安全规则、Windows 踩坑；全部变更记录已拆到 [CHANGELOG.md](CHANGELOG.md)（修订 40）。改架构先改这里再改代码。 |
| [README.md](README.md) | 本文件。全项目文件地图。 |
| [config.yaml](config.yaml) | **主配置**（用户可改，含手写注释）。audio / vad / asr / llm / tts / ui / whitelist 全部字段。白名单三类（apps 别名、commands 命令、paths 路径）+ 打断阈值调优指南都在这里。`ui` 块控制全局热键和托盘（`hotkey` / `hotkey_enabled` / `tray_enabled`）。`wake_word` / `dictation` / `context` / `metrics` 四块是修订 56 新增（唤醒词开关+短语 / 听写退出短语 / 多轮上下文开关 / 度量埋点开关）。禁止在代码里硬编码这些值。 |
| [voxctl.local.yaml](voxctl.local.yaml) | GUI 控制台维护的**本地覆盖**（机器写入，gitignore），覆盖 config 的 llm/asr/tts 三块。新写入的 API key 存 OS keyring，不进 yaml；旧版 yaml 明文 key 仅保留迁移读取兼容。 |
| [user_apps.yaml](user_apps.yaml) | GUI 维护的**用户别名表**（应用名→exe/.lnk 路径，机器写入，gitignore）。启动时合并进 `whitelist.apps`，config.yaml 同名优先。独立成文件是为了不破坏 config.yaml 的注释。（当前工作区可能尚未生成。） |
| [requirements.txt](requirements.txt) | 依赖清单（宽松基线版本）。pydantic/anthropic/openai/numpy/sounddevice/onnxruntime/faster-whisper/opencc/psutil/edge-tts/pyttsx3/av(PyAV)/PyYAML/pywebview/keyring。注明修订 21 起用 onnxruntime 跑 VAD、去掉 torch。 |
| [pyproject.toml](pyproject.toml) | pytest 与 Ruff 项目配置：测试收集范围、Python 路径和静态检查规则。 |
| [.github/workflows/ci.yml](.github/workflows/ci.yml) | GitHub Actions CI：安装依赖，执行 Ruff、全量单元测试、离线 TTS 模板和主循环纯状态机验收。 |
| [.gitignore](.gitignore) | 忽略规则。venv、logs、test_audio、打包产物、`voxctl.local.yaml` / `user_apps.yaml`（机器写入的本地状态）。 |

### 入口 / 启动脚本

| 文件 | 作用 |
|---|---|
| [app.py](app.py) | **统一入口**（修订 19）。默认 `python app.py` 起 PyWebView GUI 控制台；`python app.py --voice` 委托 `main.main()` 跑语音。单 exe 靠 `--voice` 参数复用两种角色。含 frozen 下 stdout 重定向（修订 28：语音角色接回 fd1 让崩溃 traceback 落进 voxctl-voice.out 供 GUI 回读，GUI 角色扔 devnull）、chdir 到 exe 目录。 |
| [main.py](main.py) | **语音主循环入口**。日志配置调 `core/logger.py` 的 `configure_root_logging()`（修订 25：控制台 INFO + 轮转文件 DEBUG，UTF-8，windowed 下跳过控制台 handler）、frozen chdir、`--config` 参数、构造 `MainLoop.from_config_file()` 跑 `asyncio.run`、捕获 Ctrl+C。 |
| [run.bat](run.bat) | 一键启动语音主程序：设 UTF-8 代码页 → 激活 venv → `python main.py`。日常开发用，用户双击即可。 |
| [build_voice.bat](build_voice.bat) | 语音主程序 exe 打包脚本（PyInstaller onedir → `dist/voxctl/`，console 模式）。显式 collect 原生依赖、exclude torch、打包 VAD，并随包拷 `plugins\*.py`。默认约 271MB、首次联网下载 Whisper；修订 60 起可先设置 `VOXCTL_OFFLINE_MODEL_DIR=<CTranslate2 模型目录>`，把模型打到 `_internal/models/whisper`，`small` 离线实测产物约 725MB。 |
| [build_gui.bat](build_gui.bat) | GUI 控制台 exe 打包脚本（onedir `--windowed` 无黑窗）。在 build_voice 基础上加 PyWebView 原生栈 + `--hidden-import main` + index.html；同样支持 `VOXCTL_OFFLINE_MODEL_DIR` 真离线构建。 |
| [tests/acceptance/test_gui_smoke.py](tests/acceptance/test_gui_smoke.py) | 源码模式 GUI 窗口**冒烟测试**（修订 19）。开 pywebview 窗口→探测 js_api 桥→检查 11 个方法存在→自动关闭。非交互。 |

---

## audio/ — 音频采集、播放、VAD、输出模式

| 文件 | 作用 |
|---|---|
| [audio/__init__.py](audio/__init__.py) | 导出 AudioRecorder / VADProcessor / VADMode / AudioPlayer / OutputMode / detect_output_mode。 |
| [audio/recorder.py](audio/recorder.py) | **录音封装**。`AudioRecorder` 用 sounddevice InputStream（16kHz、512 样本/帧），回调线程经 `run_coroutine_threadsafe` 把音频块送进 asyncio.Queue。`list_devices()`/`print_devices()` 引导选设备。异常 `AudioRecorderError`/`DeviceNotFoundError`。 |
| [audio/player.py](audio/player.py) | **播放封装**。`AudioPlayer.play()`（sd.play 非阻塞 + `to_thread(sd.wait)`）、`stop()`（sd.stop 让 wait 返回——打断的物理基础）、`is_playing`。 |
| [audio/vad.py](audio/vad.py) | **VAD 核心**。`VADProcessor` 状态机（SILENCE→确认→SPEAKING→确认静默→emit 片段），用 onnxruntime+numpy 直跑 silero_vad.onnx，逐帧打概率、自复刻 OnnxWrapper 状态。`process_chunk(mode)` 常规监听、`process_chunk_interrupt()` 快速打断判定、`reset()`。含耳机/外放两套打断阈值、预滚防截字头。frozen 感知的模型路径解析。 |
| [audio/silero_vad.onnx](audio/silero_vad.onnx) | **vendor 的 silero-vad 模型**（2.3MB，修订 21 随包走的打包资产）。绕开 silero_vad 官方包，去掉 torch 依赖。 |
| [audio/output_mode.py](audio/output_mode.py) | **耳机/外放检测**。`detect_output_mode(override)` 用设备名关键词匹配返回 `OutputMode.HEADPHONE/SPEAKER`；检测失败默认 SPEAKER（更保守）。用户可在 config.yaml 手动覆盖。 |

---

## asr/ — 语音转文字

| 文件 | 作用 |
|---|---|
| [asr/__init__.py](asr/__init__.py) | 导出 ASR 抽象层、工厂、本地 Whisper 实现和异常。 |
| [asr/asr_engine.py](asr/asr_engine.py) | **ASR 抽象层**。`ASREngine`、`ASRResult`、异常层级、置信度过滤与 OpenCC 共享 helper，以及按 `provider` 构造实现的工厂。 |
| [asr/providers/__init__.py](asr/providers/__init__.py) | 云端 ASR provider 包标记。 |
| [asr/providers/openai_asr.py](asr/providers/openai_asr.py) | **OpenAI 兼容云端 ASR**。内存 WAV 上传 `/audio/transcriptions`，统一结果过滤、繁简转换和远端错误归一化。 |
| [asr/whisper_engine.py](asr/whisper_engine.py) | **faster-whisper 本地实现**。惰性加载模型，`transcribe()` 返回统一 `ASRResult`，按置信度过滤空/噪声结果并做 opencc t2s 转换；冻结产物优先发现 `models/whisper` 随包模型，实现真离线加载。 |

---

## nlu/ — 意图解析（LLM 结构化输出）

| 文件 | 作用 |
|---|---|
| [nlu/__init__.py](nlu/__init__.py) | 导出 IntentParser、schema 符号、LLM 异常。 |
| [nlu/schema.py](nlu/schema.py) | **意图数据结构**（pydantic）。OpenApp/CloseApp/QuerySystem/OpenFile/RunCommand/ExitProgram/Confirm/Cancel/Unknown/EnterDictation（修订 56）各意图模型 + `IntentType` 枚举 + `ParsedIntent` 判别联合 + `validate_intent()` + `build_intent_schema()`（生成 tool 规格）+ `safe_model_dump()`（安全转 dict，供 event_loop/executor 审计日志用）。中文字段描述。 |
| [nlu/llm_client.py](nlu/llm_client.py) | **LLM 抽象层**。`LLMClient` 抽象基类（`parse_intent(text, schema, context=None)`——`context` 为上一轮指代摘要，修订 56；+ `_resolve_name()` 处理中转站 tool 名重写的公共逻辑）、异常层级（LLMError/Timeout/Auth/RateLimit/API/Parse）、`build_client()` 工厂（惰性导入按 provider 实例化）。屏蔽厂商差异。 |
| [nlu/intent_parser.py](nlu/intent_parser.py) | **意图解析器**。`from_config()` 从环境变量读 api_key（空则 RuntimeError），`parse(text, context=None)` 把 LLM 错误映射为 UnknownIntent 兜底（Auth 错误重抛）；`context` 透传给 client 供多轮指代消解（修订 56）。`LLMConfig` 数据类。 |
| [nlu/providers/__init__.py](nlu/providers/__init__.py) | 仅文档字符串（无顶层 provider 导入，供惰性加载 / PyInstaller 裁剪）。 |
| [nlu/providers/anthropic_provider.py](nlu/providers/anthropic_provider.py) | **Anthropic 实现**。Messages API + `tool_choice=any`，错误归一化，`_extract_tool_call()`。tool 名解析复用基类 `LLMClient._resolve_name()`；有 `context` 时拼进 system prompt 辅助指代消解（修订 56）。 |
| [nlu/providers/openai_provider.py](nlu/providers/openai_provider.py) | **OpenAI 实现**。Chat Completions + `tool_choice=required`，解析 JSON arguments，tool 名解析同样复用基类；`context` 注入 system prompt 同 Anthropic（修订 56）。 |

---

## executor/ — 执行模块（白名单 + 实际系统操作）

| 文件 | 作用 |
|---|---|
| [executor/__init__.py](executor/__init__.py) | **执行分发器**。`Executor`（按意图类型分发）、`ExecutionResult` 数据类（含 `enter_dictation` 标志，修订 56）、`load_user_apps()`。`from_config()` 合并 user_apps.yaml（config 优先）、按 allow_open_any_app 建 AppIndex。`execute()` 无论成败都写审计日志（JSON lines）；`EnterDictationIntent` 走轻量分支置 `enter_dictation=True`，交主循环切听写模式（修订 56）。 |
| [executor/whitelist.py](executor/whitelist.py) | **白名单校验**。`Whitelist` 校验 apps/commands/paths。`get_app_path()`、`get_command()`、`is_path_allowed()`（resolve + is_relative_to，空=拒绝）。`CommandEntry`（列表形式命令 + dangerous 标记）。 |
| [executor/app_index.py](executor/app_index.py) | **已安装应用索引**（修订 15）。`AppIndex.build()` 扫开始菜单 .lnk + 注册表 App Paths，排除 uninstall/卸载。`resolve()` 确定性匹配（精确→唯一子串→歧义/未找到），不做相似度猜测。 |
| [executor/app_control.py](executor/app_control.py) | **开关应用**。`open_app()`：白名单别名→索引→拒绝（os.startfile）；`close_app()`：仅认白名单、需二次确认、按 exe 名 psutil 终止进程。 |
| [executor/system_query.py](executor/system_query.py) | **系统状态查询**。`query()` 用 psutil 查内存/磁盘/CPU；磁盘支持指定盘符或全部可读分区；CPU 用 interval=1。 |
| [executor/file_ops.py](executor/file_ops.py) | **文件操作**。`open_file()`：`_resolve_path()` 三策略（完整路径 / 位置关键词+文件名 / 裸文件名搜索），白名单校验后 os.startfile。位置关键词：桌面/文档/下载。 |
| [executor/shell_runner.py](executor/shell_runner.py) | **白名单命令执行**。`run()`：查白名单命令、dangerous→需确认、subprocess `shell=False`（禁任意拼接）、30s 超时、输出截断 500 字。 |
| [executor/text_input.py](executor/text_input.py) | **听写文本注入**（修订 56）。`inject_text(text) -> int` 用 ctypes `SendInput` + `KEYEVENTF_UNICODE` 逐字符打字、返回注入字符数；**只发字面字符**，跳过控制字符（<0x20，含回车/Tab）与 BMP 外字符（§6 安全边界，无法凭自身提交/执行）；非 Windows 抛 `TextInputError` 优雅降级。懒加载 `_get_send_input()` 便于测试注入。 |

---

## plugins/ — 插件机制（扩展意图，不扩展权限，修订 42）

| 文件 | 作用 |
|---|---|
| [plugins/__init__.py](plugins/__init__.py) | **PluginRegistry**：启动时 `load_from_directory()` 扫 `plugins/*.py`（跳过 `_` 开头与 `base.py`），加载前 AST 安全扫描拦截 subprocess/os.system 等危险调用（修订 47），校验 `PLUGIN_SPEC` 后注册；`tool_specs()` 合并进 LLM 意图工具集、`dispatch()` 分发到插件 handler（异常兜底不崩主循环）。 |
| [plugins/base.py](plugins/base.py) | `PluginSpec` 数据类 + `PluginError`/`PluginLoadError` 异常 + 插件开发约束说明。 |
| [plugins/volume_control.py](plugins/volume_control.py) | **音量/静音插件**（修订 51）。intent `volume_control`，ctypes `keybd_event` 发虚拟音量键，无 pycaw/subprocess，非 Windows 优雅降级。 |
| [plugins/clipboard_read.py](plugins/clipboard_read.py) | **剪贴板读取插件**（修订 53）。intent `clipboard_read`，ctypes 读 CF_UNICODETEXT 供 TTS 朗读；**只读**，绝不执行/打开内容；空/超长/非 Windows 均有兜底。 |
| [plugins/media_control.py](plugins/media_control.py) | **媒体控制插件**（修订 55）。intent `media_control`，ctypes 发媒体键（播放暂停/停止/上一首/下一首），单键 tap，非 Windows 降级。 |
| [plugins/window_control.py](plugins/window_control.py) | **窗口/桌面插件**（修订 55）。intent `window_control`，ctypes 组合键（显示桌面 Win+D/最小化 Win+M/恢复 Win+Shift+M/切换窗口 Alt+Tab），down 顺序 + up 逆序。 |
| [plugins/system_shortcuts.py](plugins/system_shortcuts.py) | **系统快捷插件**（修订 55）。intent `system_shortcuts`，锁屏走 `user32.LockWorkStation()`、截图走 Win+Shift+S，均非破坏性（`dangerous=False`）。 |
| [plugins/datetime_query.py](plugins/datetime_query.py) | **时间日期查询插件**（修订 55）。intent `datetime_query`，`datetime` 纯只读，12 小时制报时 + 中文星期报日期，TTS 模板，跨平台无按键。 |
| [plugins/power_status.py](plugins/power_status.py) | **电池状态插件**（修订 55）。intent `power_status`，`psutil.sensors_battery()` 查电量/充电状态，无电池台式机友好降级。 |
| [plugins/reminder.py](plugins/reminder.py) | **一次性提醒插件**（修订 58）。intent `set_reminder`，校验 1～86400 秒延迟和最多 200 字提醒文本；定时与播报由主循环统一管理，进程退出即取消。 |
| [plugins/weather_query.py](plugins/weather_query.py) | **天气查询插件**（修订 61）。intent `weather_query`，要求明确城市，固定 HTTPS GET Open-Meteo，支持当前/今天/明天；不定位、不猜城市、无 API key。 |
| [plugins/known_folder.py](plugins/known_folder.py) | **固定文件夹插件**（修订 62）。intent `open_known_folder`，仅允许桌面/文档/下载，使用 Windows Known Folder API 解析真实路径。 |
| [plugins/brightness_control.py](plugins/brightness_control.py) | **屏幕亮度插件**（修订 63～65）。intent `brightness_control`，支持查询/设置/增减内置屏亮度，通过显式 WMI `ExecMethod_` 参数对象写入。 |

---

## tts/ — 文字转语音

| 文件 | 作用 |
|---|---|
| [tts/__init__.py](tts/__init__.py) | 导出 TTS 抽象层、工厂、本地 Edge 实现和异常。 |
| [tts/tts_base.py](tts/tts_base.py) | **TTS 抽象层**。`BaseTTSEngine`、异常层级、按 `provider` 构造实现的工厂，以及 edge/OpenAI 共用的 pyttsx3 本地后备。 |
| [tts/_audio.py](tts/_audio.py) | Edge 与 OpenAI TTS 共用的 PyAV MP3→float32 mono PCM 解码工具。 |
| [tts/providers/__init__.py](tts/providers/__init__.py) | 云端 TTS provider 包标记。 |
| [tts/providers/openai_tts.py](tts/providers/openai_tts.py) | **OpenAI 兼容云端 TTS**。调用 `/audio/speech` 生成 MP3，经共享解码器播放，远端失败走本地后备。 |
| [tts/edge_tts_engine.py](tts/edge_tts_engine.py) | **Edge TTS 封装**。`synthesize_pcm()`（edge-tts 出 MP3 → PyAV 解码为 PCM float32）、`speak()`、`speak_result()`。`format_result(ExecutionResult)` 是**纯函数模板拼接**（按 action_type 套模板，退出→"再见"），**不让 LLM 生成播报文本**，保证可控可测。异常 TTSError/Synthesize/Decode。 |

---

## core/ — 主循环、状态机、打断协调

| 文件 | 作用 |
|---|---|
| [core/__init__.py](core/__init__.py) | 导出 MainLoop / MainLoopError / InterruptCoordinator / LoopState / PendingConfirmation / State。 |
| [core/version.py](core/version.py) | 版本号与构建信息。源码模式读取 git 短 hash，冻结产物可读取静态 `_build_hash`，不可用时返回 `unknown`。 |
| [core/state.py](core/state.py) | **状态机定义**。`State` 枚举（IDLE/LISTEN/ASR/EXECUTE/TTS/AWAIT_CONFIRM）、`LoopState` 容器（当前状态、pending、rounds、exit_requested）、`PendingConfirmation`（二次确认上下文）。 |
| [core/event_loop.py](core/event_loop.py) | **主循环核心**。`MainLoop` 把五个阶段串成状态机：`_phase_listen`/`_phase_asr`/`_phase_execute`/`_phase_tts`/`_phase_await_confirm`。同步调用用 `to_thread` 包装，TTS 阶段并发跑打断监听。负责二次确认、退出时序、唤醒词、听写、多轮上下文、度量埋点；修订 58 增加进程内提醒任务与到期播报队列，修订 59 同步托盘阶段状态并管理其生命周期。 |
| [core/interrupt_coordinator.py](core/interrupt_coordinator.py) | **打断协调器**（Step 7）。TTS 期间 `monitor()` 并发监听音频队列，`process_chunk_interrupt` 判定打断→立即 `player.stop()`。`drain_queue()` 清空带 TTS 泄漏的残留音频块，避免回 LISTEN 后误触发。 |
| [core/local_settings.py](core/local_settings.py) | **本地覆盖读写**（修订 19/23/46）。读写 voxctl.local.yaml 并把 llm/asr/tts 叠进 config；API key 新写入走 OS keyring，启动时注入对应环境变量，旧 yaml key 仅作迁移 fallback。`effective_*_settings()` 给 GUI 展示生效值但不回传明文 key。 |
| [core/logger.py](core/logger.py) | **共用日志配置**（修订 25 真正实现）。`configure_root_logging(log_path, file_level, console_level, max_bytes, backup_count)`：建 logs 目录 + RotatingFileHandler(UTF-8) + windowless-safe StreamHandler（`sys.stdout` 为 None 时跳过，修订 19）。main.py（voxctl.log/DEBUG）与 webui/gui.py（voxctl-gui.log/INFO）各传参调用，消除此前两处重复逻辑。审计日志 actions.log 是另一条独立路径，不走这里。 |
| [core/factory.py](core/factory.py) | **MainLoop 构造工厂**（修订 35 从 event_loop 拆出）。`create_from_config_file()` / `create_from_config()` 从 config 实例化 recorder/vad/asr/parser/executor/tts/player、插件注册器及唤醒词/听写/多轮/度量配置；按 `ui.tray_enabled` 创建托盘并注入 MainLoop。 |
| [core/phrase_match.py](core/phrase_match.py) | **文本匹配纯函数**（修订 56）。`normalize()`（NFKC + 去标点空白 + 小写）、`match_wake_prefix(text, phrases)`（唤醒词前缀匹配，把归一化切点映射回原文剥离）、`match_exit_phrase(text, phrases)`（听写退出短语等值或包含匹配）。纯函数无副作用，供 event_loop 唤醒/听写过滤调用。 |
| [core/audit.py](core/audit.py) | **审计日志写入器**（§6 强制，executor 与 event_loop 共用）。`log_action()` / `log_interrupt_event()`（修订 56 加 `interrupt_latency_ms`）/ `log_metrics()`（修订 56：每轮各阶段延迟写 `event="metrics"` 行）。UTF-8 JSON lines 追加写 logs/actions.log。 |

---

## ui/ — 全局热键与系统托盘

全局键盘热键 toggle 语音子进程启停（非破坏性生命周期操作，免二次确认）；系统托盘只读展示语音主循环状态。修订 50 起浮窗已删。

| 文件 | 作用 |
|---|---|
| [ui/__init__.py](ui/__init__.py) | 包标记。 |
| [ui/hotkey.py](ui/hotkey.py) | **全局键盘热键**（修订 33）。Win32 `RegisterHotKey` + daemon 消息循环线程，toggle 语音启停。纯 ctypes，不引新依赖。热键被占用回报错误不静默。非 Windows no-op。 |
| [ui/tray.py](ui/tray.py) | **只读状态托盘**（修订 59）。纯 ctypes 调 `Shell_NotifyIconW`，独立消息线程显示监听/识别/执行/播报/确认状态；无菜单和点击操作，非 Windows 安全降级。 |

---

## webui/ — 本地管理控制台（PyWebView GUI）

| 文件 | 作用 |
|---|---|
| [webui/__init__.py](webui/__init__.py) | 包标记（一行 docstring）。 |
| [webui/gui.py](webui/gui.py) | **GUI 控制台主体**（修订 19/24/33/52）。PyWebView 原生窗口 + `Api` js_api 桥（11+ 个方法）：别名增删/解析/打开、审计日志、LLM/ASR/TTS 设置读写、语音子进程启停、热键设置、`get_audio_mode()`（外放/耳机 + 打断可用性诚实提示，修订 52）。修订 24 把已删的 server.py 共享底层（`ConsoleState`/别名读写/`validate_alias`/frozen 路径解析）内联进来。`run_gui()` 起全局热键监听（修订 33，修订 50 起浮窗已删）。**只暴露非破坏性操作**；语音启停含四道失败反馈防线（修订 20：预检 key、重定向 stderr、探活、崩溃回读）。 |
| [webui/static/index.html](webui/static/index.html) | **控制台前端**（单文件，无构建步骤）。修订 22 亮色毛玻璃视觉，**离线自足**（无 CDN/webfont/图片，纯 CSS 渐变）。左侧导航（语音控制/大模型配置/应用别名/审计日志）+ 右侧卡片。经 `window.pywebview.api.*` 调后端。 |

---

## tests/ — 测试与诊断脚本

分三层组织。默认 `pytest` 只收集 `tests/unit/`（`pyproject.toml` 配置）；验收脚本默认手动运行，其中 TTS `--templates-only` 与主循环默认纯状态机模式由 CI 显式执行。诊断工具仍需手动运行。详见 [tests/README.md](tests/README.md)。

[tests/__init__.py](tests/__init__.py) 是测试包标记。

### unit/ — 单元测试（可进 CI，无需设备/网络）

| 文件 | 作用 |
|---|---|
| [tests/unit/__init__.py](tests/unit/__init__.py) | unit 测试包标记。 |
| [test_app_index.py](tests/unit/test_app_index.py) | AppIndex 匹配规则（临时目录伪造开始菜单，覆盖精确/子串/歧义/零命中/卸载排除）。 |
| [test_asr_provider.py](tests/unit/test_asr_provider.py) | ASR 抽象工厂、云端 OpenAI provider、WAV 编码、结果过滤与远端错误映射。 |
| [test_event_loop.py](tests/unit/test_event_loop.py) | 主循环各阶段、确认/退出/打断、提醒、托盘、推测 ASR、审计与运行循环。 |
| [test_interrupt.py](tests/unit/test_interrupt.py) | 打断功能（mock VAD/Player，12 用例含 monitor 检测/打断取消 pending/exit 时序）。 |
| [test_stability.py](tests/unit/test_stability.py) | 稳定性（复用 PureStateMachineLoop，30 轮 0 崩溃 + RSS 增长检查 + 状态收敛）。 |
| [test_webui.py](tests/unit/test_webui.py) | webui 纯函数（validate_alias / user_apps 合并优先级，修订 24 import 改自 webui.gui）。 |
| [test_hotkey.py](tests/unit/test_hotkey.py) | 热键字符串解析纯函数（修订 33）。 |
| [test_local_settings.py](tests/unit/test_local_settings.py) | 本地覆盖读写、配置合并、keyring 存取与旧 yaml key 迁移兼容。 |
| [test_mainloop_integration.py](tests/unit/test_mainloop_integration.py) | mock 组件下的正常流程、退出时序、打断取消退出、确认与多轮稳定性。 |
| [test_plugins.py](tests/unit/test_plugins.py) | 插件规格、注册/调度、schema 合并、目录加载与 AST 安全扫描。 |
| [test_security_review.py](tests/unit/test_security_review.py) | 白名单、命令参数、文件扩展名与关闭应用路径匹配的安全回归。 |
| [test_tts_provider.py](tests/unit/test_tts_provider.py) | TTS 抽象工厂、OpenAI provider、响应取字节、播放和 pyttsx3 降级。 |
| [test_vad_adaptive.py](tests/unit/test_vad_adaptive.py) | VAD 自适应阈值的噪声基线、夹紧与状态更新规则。 |
| [test_volume_plugin.py](tests/unit/test_volume_plugin.py) | 音量插件（修订 51）：steps 夹紧、mute/up/down、非 Windows 降级、经真实 PluginRegistry 加载。 |
| [test_clipboard_plugin.py](tests/unit/test_clipboard_plugin.py) | 剪贴板插件（16 例，修订 53）：读取/空/超长截断/忽略参数/非 Windows 降级/异常兜底 + 真实加载与调度。 |
| [test_media_control.py](tests/unit/test_media_control.py) | 媒体控制插件（10 例，修订 55）：每个动作按键序列、非法/缺失 action、非 Windows 降级、异常兜底 + 真实加载。 |
| [test_window_control.py](tests/unit/test_window_control.py) | 窗口/桌面插件（11 例，修订 55）：组合键 down 顺序/up 逆序校验、非法/缺失、非 Windows 降级 + 真实加载。 |
| [test_system_shortcuts.py](tests/unit/test_system_shortcuts.py) | 系统快捷插件（10 例，修订 55）：LockWorkStation 调用、截图组合键逆序、非法/缺失、降级、异常兜底 + 真实加载。 |
| [test_datetime_query.py](tests/unit/test_datetime_query.py) | 时间日期插件（23 例，修订 55）：time/date/both、边界时刻（整点/凌晨/中午/周日）、无效 field、异常兜底 + 真实加载。 |
| [test_power_status.py](tests/unit/test_power_status.py) | 电池状态插件（9 例，修订 55）：充电/未充电/百分比取整/无电池/异常兜底 + 真实加载。 |
| [test_phrase_match.py](tests/unit/test_phrase_match.py) | 唤醒词/听写退出文本匹配纯函数（27 例，修订 56）：normalize、唤醒前缀命中/剥离/未命中、退出短语等值与包含匹配、切点映射回原文。 |
| [test_text_input.py](tests/unit/test_text_input.py) | 听写文本注入 SendInput（11 例，修订 56）：逐字符注入计数、控制字符/BMP 外跳过、非 Windows 降级抛 TextInputError。 |
| [test_rev56_features.py](tests/unit/test_rev56_features.py) | 修订 56 接线集成（22 例）：enter_dictation 意图/schema/executor/TTS 模板、唤醒词过滤、听写注入与隐私脱敏、多轮上下文透传/摘要更新、度量埋点写审计行。 |
| [test_reminder_plugin.py](tests/unit/test_reminder_plugin.py) | 一次性提醒插件参数边界、返回契约与注册器加载。 |
| [test_tray.py](tests/unit/test_tray.py) | 托盘非 Windows 降级与未知状态归一化。 |
| [test_whisper_offline.py](tests/unit/test_whisper_offline.py) | 随包 Whisper 模型发现、残缺模型拒绝、本地路径传递与两个构建脚本接线。 |
| [test_weather_plugin.py](tests/unit/test_weather_plugin.py) | 天气三个时段、固定端点、WMO 映射、网络/数据失败与注册器加载。 |
| [test_known_folder_plugin.py](tests/unit/test_known_folder_plugin.py) | 固定文件夹白名单、Known Folder 解析/打开失败、平台降级与注册器加载。 |
| [test_brightness_plugin.py](tests/unit/test_brightness_plugin.py) | 亮度查询/设置/增减、参数边界、WMI 原生调用链、平台/硬件降级与注册器加载。 |

### acceptance/ — 验收脚本（默认需真实设备/网络，手动运行）

| 文件 | 作用 |
|---|---|
| [tests/acceptance/__init__.py](tests/acceptance/__init__.py) | acceptance 测试包标记。 |
| [test_recorder_vad.py](tests/acceptance/test_recorder_vad.py) | Step 1：录音闭环 + VAD 人声检测（交互式，需真实麦克风，可存 wav）。 |
| [test_asr.py](tests/acceptance/test_asr.py) | Step 2：ASR 准确率。`--live` 实时录音转写，或 `--wav-dir` 离线核对。 |
| [test_intent_parser.py](tests/acceptance/test_intent_parser.py) | Step 3：意图解析准确率（打真实 LLM，两个 provider 各跑一遍，无 mock）。 |
| [test_executor.py](tests/acceptance/test_executor.py) | Step 4：执行模块端到端（真实开关 notepad、查状态、开文件、跑命令、白名单拦截）。 |
| [test_tts.py](tests/acceptance/test_tts.py) | Step 5：TTS 播报（默认需网络 + 音频设备；`--templates-only` 是纯函数可单独跑）。 |
| [test_main_loop.py](tests/acceptance/test_main_loop.py) | Step 6：默认纯状态机且无外部副作用；`--components` 显式启用真实 LLM/执行/TTS，`--live` 再启用真实麦克风。 |
| [test_gui_smoke.py](tests/acceptance/test_gui_smoke.py) | GUI 窗口冒烟（需 pywebview 真实窗口，非交互，自动关闭）。 |

### tools/ — 诊断脚本（非测试）

| 文件 | 作用 |
|---|---|
| [probe_relay.py](tests/tools/probe_relay.py) | 中转站兼容性探针（§8.9）。跳过 SDK 直接打 HTTP，验证目标端点是否真支持 tool use / function calling。换中转前先跑。 |

---

## logs/ — 运行时日志（不进 git）

| 文件 | 作用 |
|---|---|
| [logs/actions.log](logs/actions.log) | **审计日志**（§6 强制）。每次执行动作 + 打断事件的 JSON lines 记录（时间戳、原始文本、意图、操作、结果、打断详情）。排查误触发的唯一依据。 |
| [logs/voxctl.log](logs/voxctl.log) | 语音主程序运行日志（轮转，DEBUG）。 |
| [logs/voxctl-gui.log](logs/voxctl-gui.log) | GUI 进程日志（windowed 无控制台，日志只能进文件）。 |
| [logs/voxctl-voice.out](logs/voxctl-voice.out) | GUI 拉起的语音子进程 stdout/stderr 重定向（修订 20：兜早期崩溃）。修订 50 起浮窗已删，此文件仅供 GUI `_read_voice_failure_reason` 回读子进程崩溃时的早期 traceback。 |

---

## 其他资产

| 文件 | 作用 |
|---|---|
| [test_audio/segment_001.wav](test_audio/segment_001.wav) | VAD 截取出的人声片段样例（测试录音输出，不进 git）。 |

---

## 快速上手

```powershell
# 1. 建虚拟环境 + 装依赖
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt

# 2. 设 API Key（不写进 config.yaml）
setx ANTHROPIC_API_KEY <你的key>

# 3. 起 GUI 控制台（配 LLM / 管别名 / 启停语音）
python app.py

# 或直接跑语音主程序
run.bat            # 等价于 python main.py

# 退出：Ctrl+C，或语音说"退出程序"
```

## 安全红线（详见 CLAUDE.md §6）

- **关闭应用 / 命令 / 文件路径 100% 白名单强制**，无绕过；打开应用放开为全机已安装应用（非破坏性）。
- 危险操作（关应用、删文件、dangerous 命令、关机）需**语音二次确认**。
- **API Key 绝不进 config.yaml / 新写入的 voxctl.local.yaml 明文**——GUI 保存到 OS keyring，启动时注入环境变量；旧 yaml 明文只作迁移读取兼容。
- 打断只能取消"播报"和"等待确认"，**不能撤回已发出的系统调用**。
- GUI 只暴露非破坏性操作；控制电脑仍只走语音 + 二次确认。
