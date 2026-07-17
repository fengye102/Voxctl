"""ui：Voxctl 屏幕状态浮窗（光圈，CLAUDE.md §8.11 / 修订 26）。

给用户一个屏幕级实时状态反馈——右下角一枚 80×80 小圆点，跟随语音状态机
（IDLE/LISTEN/ASR/EXECUTE/TTS/AWAIT_CONFIRM/错误）变色变动画。

单向数据流（语音子进程 → GUI）：
    语音进程 event_loop 各 _phase_* 入口
        → ui.state_emitter.emit_state()  print 一行 JSON 到 stdout
        → （被 webui.gui.voice_start 重定向到 logs/voxctl-voice.out，修订 20 已有通道）
        → ui.state_tailer 后台线程 tail-follow 该文件 → parse_line
        → webview evaluate_js 推给 ui/static/overlay.html

架构约束（§8.11）：不改 core/state.py、不改 main.py 主循环、不塞 transition()，
浮窗是纯展示的可选增强，config.yaml 的 ui.status_overlay 关掉后语音功能完全不受影响。
"""
