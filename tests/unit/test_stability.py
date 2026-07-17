"""Step 8 稳定性验收测试。

CLAUDE.md §7 验收标准：
    连续运行 30 分钟不崩溃、不内存泄漏

本测试用 PureStateMachineLoop（mock LLM/TTS）跑 30 轮主循环，
检查：
    1. 不崩溃（所有轮次正常完成）
    2. 内存不泄漏（每轮 RSS 增长 < 阈值）
    3. 状态正确收敛（每轮结束都回到 LISTEN 或退出）
    4. 轮次计数准确

注：真实 30 分钟稳定性测试需要 live 模式 + 麦克风，这里只验证
代码层面没有对象累积、状态机不会无限循环等明显问题。
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sys
import unittest
from pathlib import Path

# 保证从项目根目录导入
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 复用 test_main_loop 里的 PureStateMachineLoop
from tests.acceptance.test_main_loop import PureStateMachineLoop

_LOG = logging.getLogger(__name__)


def _get_rss_kb() -> int:
    """获取当前进程 RSS（KB），跨平台兼容。"""
    try:
        import psutil
        return int(psutil.Process().memory_info().rss / 1024)
    except ImportError:
        # psutil 不可用，用 tracemalloc 的当前内存（不完全等于 RSS，但能反映 Python 对象增长）
        import tracemalloc
        if not tracemalloc.is_tracing():
            tracemalloc.start()
        return int(tracemalloc.get_traced_memory()[0] / 1024)


class TestStability30Rounds(unittest.IsolatedAsyncioTestCase):
    """连续跑 30 轮主循环，验证不崩溃 + 不内存泄漏。"""

    ROUNDS = 30
    # 每轮允许的 RSS 增长上限（KB）。30 轮总增长应 < 5MB
    # 用平均值更稳定（gc 抖动大）
    MAX_AVG_RSS_GROWTH_KB = 200  # 平均每轮增长 < 200KB

    async def test_30_rounds_no_crash_no_leak(self):
        """30 轮 × （打开记事本 + 退出），验证稳定性。"""
        # 每轮：1 条"打开记事本"指令，然后第 30 轮用"退出程序"
        rss_samples: list[int] = []
        rounds_completed = 0
        crashes = 0

        gc.collect()
        rss_before = _get_rss_kb()
        rss_samples.append(rss_before)

        for i in range(self.ROUNDS):
            # 最后一轮用退出指令，前面 29 轮用打开记事本
            if i == self.ROUNDS - 1:
                inputs = ["退出程序"]
                expect_exit = True
            else:
                inputs = ["打开记事本"]
                expect_exit = False

            loop = PureStateMachineLoop(inputs)
            try:
                await loop.run()
            except Exception as exc:  # noqa: BLE001
                crashes += 1
                _LOG.error("round %d crashed: %s", i + 1, exc)
                continue

            # 状态收敛检查
            if expect_exit:
                if not loop._state.exit_requested:
                    self.fail(f"round {i+1}: 期望退出但未退出")
            else:
                if loop._state.exit_requested:
                    self.fail(f"round {i+1}: 不应退出但退出了")
                if loop._state.state.value != "listen":
                    self.fail(f"round {i+1}: 期望回到 LISTEN，实际 {loop._state.state.value}")
                rounds_completed = loop._state.rounds_completed

            # 每 5 轮采样一次内存
            if (i + 1) % 5 == 0:
                gc.collect()
                rss = _get_rss_kb()
                rss_samples.append(rss)
                _LOG.info("round %d/%d done, RSS=%d KB", i + 1, self.ROUNDS, rss)

        # ─── 断言 ─────────────────────────────────────────────
        self.assertEqual(crashes, 0, f"有 {crashes} 轮崩溃")
        self.assertEqual(rounds_completed, 1, "每轮 rounds_completed 应为 1")

        # 内存增长检查
        rss_first = rss_samples[0]
        rss_last = rss_samples[-1]
        total_growth = rss_last - rss_first
        avg_growth = total_growth / self.ROUNDS

        _LOG.info("RSS: first=%d KB, last=%d KB, total_growth=%d KB, avg/round=%.1f KB",
                  rss_first, rss_last, total_growth, avg_growth)
        print("\n[稳定性] 30 轮完成，0 崩溃")
        print(f"  RSS 起始: {rss_first} KB")
        print(f"  RSS 结束: {rss_last} KB")
        print(f"  总增长:   {total_growth} KB")
        print(f"  平均每轮: {avg_growth:.1f} KB")

        # 平均增长不应超过阈值（允许 GC 抖动）
        self.assertLess(
            avg_growth, self.MAX_AVG_RSS_GROWTH_KB,
            f"平均每轮 RSS 增长 {avg_growth:.1f} KB 超过阈值 {self.MAX_AVG_RSS_GROWTH_KB} KB，疑似内存泄漏",
        )


class TestInterruptStability(unittest.IsolatedAsyncioTestCase):
    """连续触发 20 次打断，验证打断逻辑不累积状态。"""

    ROUNDS = 20

    async def test_20_interrupts_no_state_accumulation(self):
        """每轮：触发打断 → 验证状态正确清理。"""
        from tests.unit.test_interrupt import MockVAD, MockPlayer, MockTTS, MainLoopForTest
        from executor import ExecutionResult

        for i in range(self.ROUNDS):
            vad = MockVAD(interrupt_pattern=[True, True])
            player = MockPlayer()
            tts = MockTTS(speak_delay=1.0)

            loop = MainLoopForTest(vad=vad, player=player, tts=tts)
            loop._pending_result = ExecutionResult(
                success=True, action_type="open_app",
                payload={"app_name": "notepad"},
            )
            # 预填 2 个音频块触发打断
            for _ in range(2):
                await loop._audio_queue.put(__import__("numpy").zeros(
                    512, dtype="float32",
                ))

            await asyncio.wait_for(loop._phase_tts(), timeout=3.0)

            # 每轮都必须正确清理
            self.assertTrue(loop._interrupt_coord.interrupted, f"round {i+1}: 未检测到打断")
            self.assertEqual(player.stop_count, 1, f"round {i+1}: player.stop 调用次数错误")
            self.assertTrue(loop._audio_queue.empty(), f"round {i+1}: 队列未清空")
            self.assertEqual(loop._state.state.value, "listen", f"round {i+1}: 状态未切回 LISTEN")

            # 下一轮开始前，InterruptCoordinator.interrupted 应被 reset
            loop._interrupt_coord.reset()
            self.assertFalse(loop._interrupt_coord.interrupted)

        print(f"\n[打断稳定性] {self.ROUNDS} 轮打断全部正确清理")


class TestLongTransitionChain(unittest.IsolatedAsyncioTestCase):
    """验证长状态链不累积：50 轮多指令序列。"""

    async def test_50_rounds_mixed_intents(self):
        # 混合指令：打开 / 查询 / 关闭（含确认）/ 退出
        sequence = [
            "打开记事本",                    # 简单指令
            "查询内存",                      # 简单指令
            "关闭记事本", "是",              # 二次确认
            "查询CPU",                       # 简单指令
            "关闭记事本", "取消",            # 取消确认
        ]
        # 重复 8 次 = 48 条指令，最后加退出
        inputs = sequence * 8 + ["退出程序"]

        loop = PureStateMachineLoop(inputs)
        await asyncio.wait_for(loop.run(), timeout=10.0)

        self.assertTrue(loop._state.exit_requested, "应该退出")
        self.assertGreater(loop._state.rounds_completed, 30, "应该完成 30+ 轮")
        print(f"\n[长链稳定性] {len(inputs)} 条指令，{loop._state.rounds_completed} 轮完成，正确退出")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    unittest.main(verbosity=2)
