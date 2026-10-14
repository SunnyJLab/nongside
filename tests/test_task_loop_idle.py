"""Regression tests for _task_loop's idle shutdown of the background browser."""

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main


class _FakeIO:
    def __init__(self):
        self.new_session = threading.Event()
        self.stop_requested = threading.Event()
        self.tasks = queue.Queue()
        self.busy = False
        self.task = None
        self.epoch = 0
        self.tab = None
        self._drop = False


class TaskLoopIdleCloseTests(unittest.TestCase):
    def _browser(self):
        return SimpleNamespace(tab_info=None, prepare_background=None,
                               close_background=Mock())

    def test_no_idle_close_before_threshold(self):
        io, br = _FakeIO(), self._browser()
        clock = [0.0]
        ticks = [0]
        def alive():
            ticks[0] += 1
            return ticks[0] <= 3
        with patch.object(main.time, "monotonic", side_effect=lambda: clock[0]):
            main._task_loop(br, io, alive, lambda: None)
        br.close_background.assert_not_called()

    def test_idle_close_called_once_after_threshold(self):
        io, br = _FakeIO(), self._browser()
        clock = [0.0]
        ticks = [0]
        def alive():
            ticks[0] += 1
            return ticks[0] <= 3
        def idle():
            if ticks[0] == 1:
                clock[0] += main.BG_IDLE_SEC + 1
        with patch.object(main.time, "monotonic", side_effect=lambda: clock[0]):
            main._task_loop(br, io, alive, idle)
        br.close_background.assert_called_once_with()

    def test_finished_task_resets_idle_clock(self):
        io, br = _FakeIO(), self._browser()
        io.tasks.put("작업")
        clock = [0.0]
        ticks = [0]
        def alive():
            ticks[0] += 1
            return ticks[0] <= 2
        def run_task(*_a, **_k):
            clock[0] += main.BG_IDLE_SEC + 1
        with patch.object(main.time, "monotonic", side_effect=lambda: clock[0]),                 patch.object(main, "run_task", side_effect=run_task):
            main._task_loop(br, io, alive, lambda: None)
        br.close_background.assert_not_called()


if __name__ == "__main__":
    unittest.main()
