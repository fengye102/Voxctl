"""asr.providers：ASR 具体 provider 实现（修订 45）。

当前仅 OpenAI 兼容云端（CLAUDE.md §2.2：Anthropic 无音频端点，云端只做 OpenAI 格式）。
本地 faster-whisper 仍在 asr.whisper_engine，不属于本子包。
"""
