# tests/ 目录结构

## unit/ — 单元测试（可进 CI）

纯函数 / mock，无需真实设备、网络或 API key。

```powershell
pytest tests/unit/
```

**跨层依赖备注**：`test_stability.py`（unit）依赖 `test_main_loop.py`（acceptance）的 `PureStateMachineLoop` 和 `test_interrupt.py`（unit）的 `MockVAD`/`MockPlayer`/`MockTTS`/`MainLoopForTest`。这是有意为之的复用，非误分类——`PureStateMachineLoop` 是纯 mock 状态机（无外部依赖），定义在 acceptance 文件里只是因为该文件同时承载 `--live` 端到端模式。

## acceptance/ — 验收脚本（手动运行）

对应 CLAUDE.md §5 各 Step 的验收标准，需要真实麦克风、LLM API、音频设备或网络。
用 `python -m tests.acceptance.<脚本名>` 单独运行，不应被 CI 默认收集。

```powershell
python -m tests.acceptance.test_recorder_vad --save-dir ./test_audio   # Step 1
python -m tests.acceptance.test_asr --live                              # Step 2
python -m tests.acceptance.test_intent_parser --provider anthropic      # Step 3
python -m tests.acceptance.test_executor                                # Step 4
python -m tests.acceptance.test_tts                                     # Step 5
python -m tests.acceptance.test_main_loop                               # Step 6（默认 --mock 无需设备）
python -m tests.acceptance.test_gui_smoke                               # GUI 冒烟（需 pywebview 窗口）
```

**包含可独立提取到 CI 的纯函数/mock 部分**：
- `test_main_loop.py` 的 `PureStateMachineLoop` 默认 `--mock` 模式无需外部依赖
- `test_tts.py` 的 `--templates-only` 模式是纯函数测试，不需要网络

## tools/ — 诊断/探针脚本

不是测试，是换配置前手动跑的诊断工具。

```powershell
python -m tests.tools.probe_relay --provider anthropic --base-url https://xxx --model claude-xxx
```
