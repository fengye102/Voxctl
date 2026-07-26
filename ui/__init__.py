"""ui：Voxctl 的轻量 Windows UI 基础设施。

`ui/hotkey.py` 提供 `HotkeyListener`：Win32 `RegisterHotKey` + daemon 消息循环线程，
按 config.yaml 的 `ui.hotkey` 注册全局热键，触发后调注入的 `on_toggle` 回调
（GUI 注入的是 `api.toggle_voice`），完成"按热键启停语音子进程"。

非 Windows 平台 no-op。热键被占用（RegisterHotKey 失败）回报错误，不静默。

`ui/tray.py` 提供只读状态托盘，显示监听/识别/执行/播报阶段，无操作入口。
"""
