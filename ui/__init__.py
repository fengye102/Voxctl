"""ui：Voxctl 全局热键监听（CLAUDE.md §6 / 修订 33；修订 50 起本目录只剩 hotkey）。

`ui/hotkey.py` 提供 `HotkeyListener`：Win32 `RegisterHotKey` + daemon 消息循环线程，
按 config.yaml 的 `ui.hotkey` 注册全局热键，触发后调注入的 `on_toggle` 回调
（GUI 注入的是 `api.toggle_voice`），完成"按热键启停语音子进程"。

非 Windows 平台 no-op。热键被占用（RegisterHotKey 失败）回报错误，不静默。
"""
