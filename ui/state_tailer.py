"""GUI 端状态读取线程（CLAUDE.md §8.11 / 修订 26）。

后台线程 tail-follow 语音子进程的输出文件 logs/voxctl-voice.out（webui.gui.voice_start
把语音子进程 stdout 重定向到此），逐行调 ui.state_protocol.parse_line 解析，
解析成功就经 pywebview 的 window.evaluate_js 推给浮窗（window.setState）。

容错（§8.11）：
    - 文件还不存在（语音没启动）→ 等待，周期性重试打开，不报错。
    - 文件被 voice_start 重开截断（每次启动 'w' 模式重写）→ 检测到 inode/大小回退
      则重新从头读（rotate/truncate 重打开）。
    - 单行解析失败 / evaluate_js 抛错 → 吞掉继续下一行，绝不让线程崩。

生命周期：GUI run_gui() 起线程（start），窗口关闭时 stop()。线程是 daemon，
GUI 进程退出时不阻塞。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from ui.state_protocol import parse_line

_LOG = logging.getLogger(__name__)

# 轮询间隔：文件有新行时快读，无变化时慢一点省 CPU
_IDLE_POLL_S = 0.15
_WAIT_FILE_S = 0.5   # 文件不存在时的重试间隔


class StateTailer:
    """tail-follow voxctl-voice.out → 推浮窗。线程安全启停。"""

    def __init__(self, out_path: Path, overlay_window) -> None:
        """
        Args:
            out_path: logs/voxctl-voice.out 的路径
            overlay_window: pywebview window（有 evaluate_js 方法）
        """
        self._path = Path(out_path)
        self._window = overlay_window
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="state-tailer", daemon=True)
        self._thread.start()
        _LOG.info("state tailer started on %s", self._path)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    # ------------------------------------------------------------------
    def _push(self, obj: dict) -> None:
        """把解析出的协议对象推给浮窗 JS。evaluate_js 抛错吞掉。"""
        try:
            import json
            self._window.evaluate_js(
                "window.setState(%s)" % json.dumps(json.dumps(obj, ensure_ascii=False))
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("evaluate_js push failed (window closing?): %s", exc)

    def _run(self) -> None:
        f = None
        inode = None
        # 仅当文件在 tailer 启动时就已存在（可能是上一次 session 的陈旧内容）才跳过历史；
        # 若文件在启动后才出现（voice_start 新建），说明是全新 session，从头读。
        skip_history = self._path.is_file()
        try:
            while not self._stop.is_set():
                # 打开文件（不存在则等待）
                if f is None:
                    try:
                        if not self._path.is_file():
                            self._stop.wait(_WAIT_FILE_S)
                            continue
                        f = open(self._path, "r", encoding="utf-8", errors="replace")
                        inode = self._path.stat().st_ino
                        if skip_history:
                            f.seek(0, 2)
                            skip_history = False   # 之后的 rotate/truncate 重开都从头读
                    except OSError:
                        self._stop.wait(_WAIT_FILE_S)
                        continue

                # 检测 truncate / rotate：文件被重开（voice_start 'w' 截断）或换了 inode
                try:
                    st = self._path.stat()
                    if st.st_ino != inode or st.st_size < f.tell():
                        _LOG.info("voice.out rotated/truncated, reopening")
                        f.close()
                        f = None
                        continue
                except OSError:
                    f.close()
                    f = None
                    continue

                line = f.readline()
                if not line:
                    self._stop.wait(_IDLE_POLL_S)
                    continue

                obj = parse_line(line)
                if obj is not None:
                    self._push(obj)
        except Exception:  # noqa: BLE001 —— 线程绝不能把异常抛出去
            _LOG.exception("state tailer thread crashed")
        finally:
            if f is not None:
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    pass


__all__ = ["StateTailer"]
