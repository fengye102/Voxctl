"""LLM Provider 实现。

不要在此包顶层 import 具体 provider 模块——避免只用一个 provider 时也把另一个 SDK
加载进内存（也顺便照顾 PyInstaller 打包时的按需裁剪，见 CLAUDE.md 第 8.10 节）。
具体实例化统一走 `nlu.llm_client.build_client()`。
"""
