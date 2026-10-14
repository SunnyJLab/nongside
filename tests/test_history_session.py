"""History(조사 자료 보관)의 대화 수명주기와 LLM 요청 직렬화 회귀 테스트."""

import copy
import json
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent
import llm
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


class _HookQueue(queue.Queue):
    """get() 호출 번호마다 꺼내기 직전에 훅을 실행한다. 작업 루프의 경합 지점을 재현한다."""

    def __init__(self, hooks=None):
        super().__init__()
        self.hooks = hooks or {}
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        hook = self.hooks.get(self.calls)
        if hook:
            hook()
        return super().get(*args, **kwargs)


def _browser():
    return SimpleNamespace(tab_info=None, prepare_background=None, close_background=Mock())


def _run_loop(io, br, run_task, ticks_alive):
    ticks = [0]

    def alive():
        ticks[0] += 1
        return ticks[0] <= ticks_alive

    with patch.object(main, "run_task", side_effect=run_task):
        main._task_loop(br, io, alive, lambda: None)


def _recording(snaps, fn):
    """작업이 끝난 뒤의 대화 상태를 깊은 복사로 기록한다. 원본 History 참조는 나중에 바뀔 수 있다."""
    def wrapper(task, browser, io_, messages, **kwargs):
        try:
            return fn(task, browser, io_, messages, **kwargs)
        finally:
            snaps.append((task, {"messages": copy.deepcopy(list(messages)),
                                 "archive": copy.deepcopy(messages.archive)}))
    return wrapper


class TaskLoopHistoryTests(unittest.TestCase):
    def test_research_survives_next_task_in_same_conversation(self):
        io, br = _FakeIO(), _browser()
        seen = []
        io.tasks.put("첫 작업")
        io.tasks.put("둘째 작업")

        def run_task(task, browser, io_, messages, **_k):
            seen.append((task, messages))
            if task == "첫 작업":
                messages.retain("extract", "https://example.com/a", "조사 본문 가나다", set())

        _run_loop(io, br, run_task, 3)
        self.assertEqual([t for t, _ in seen], ["첫 작업", "둘째 작업"])
        first, second = seen[0][1], seen[1][1]
        self.assertIsInstance(first, agent.History)
        self.assertIs(first, second)
        self.assertIn("R0001", second.archive)
        self.assertEqual(second.archive["R0001"]["text"], "조사 본문 가나다")

    def test_new_session_set_before_next_task_clears_history(self):
        io, br = _FakeIO(), _browser()
        snaps = []
        io.tasks.put("첫 작업")

        def run_task(task, browser, io_, messages, **_k):
            if task == "첫 작업":
                messages.retain("extract", "https://example.com/a", "이전 대화 자료", set())
                io_.new_session.set()
                io_.tasks.put("새 대화 작업")

        _run_loop(io, br, _recording(snaps, run_task), 4)
        self.assertEqual([t for t, _ in snaps], ["첫 작업", "새 대화 작업"])
        self.assertIn("R0001", snaps[0][1]["archive"])
        self.assertEqual(snaps[1][1]["messages"], [])
        self.assertEqual(snaps[1][1]["archive"], {})
        self.assertFalse(io.new_session.is_set())

    def test_new_session_set_while_fetching_next_task_clears_history(self):
        # 두 번째 get() 직전에 new_session이 켜진다. 큐에서 꺼낸 뒤 비우는 경로를 검증한다.
        io, br = _FakeIO(), _browser()
        io.tasks = _HookQueue({2: lambda: io.new_session.set()})
        snaps = []
        io.tasks.put("첫 작업")
        io.tasks.put("둘째 작업")

        def run_task(task, browser, io_, messages, **_k):
            if task == "첫 작업":
                messages.retain("extract", "https://example.com/a", "경합 전 자료", set())

        _run_loop(io, br, _recording(snaps, run_task), 3)
        self.assertEqual([t for t, _ in snaps], ["첫 작업", "둘째 작업"])
        self.assertIn("R0001", snaps[0][1]["archive"])
        self.assertEqual(snaps[1][1]["messages"], [])
        self.assertEqual(snaps[1][1]["archive"], {})
        self.assertFalse(io.new_session.is_set())

    def test_stale_epoch_task_is_skipped_and_new_epoch_task_starts_clean(self):
        io, br = _FakeIO(), _browser()
        snaps = []

        def new_chat():
            io.epoch = 1
            io.new_session.set()

        io.tasks = _HookQueue({2: new_chat})
        io.tasks.put(("첫 작업", True, None, 0))
        io.tasks.put(("이전 대화 작업", True, None, 0))
        io.tasks.put(("새 대화 작업", True, None, 1))

        def run_task(task, browser, io_, messages, **_k):
            if task == "첫 작업":
                messages.retain("extract", "https://example.com/a", "이전 에포크 자료", set())

        _run_loop(io, br, _recording(snaps, run_task), 4)
        self.assertEqual([t for t, _ in snaps], ["첫 작업", "새 대화 작업"])
        self.assertIn("R0001", snaps[0][1]["archive"])
        self.assertEqual(snaps[1][1]["messages"], [])
        self.assertEqual(snaps[1][1]["archive"], {})

    def test_idle_close_keeps_history(self):
        io, br = _FakeIO(), _browser()
        seen = []
        clock = [0.0]
        io.tasks.put("첫 작업")
        ticks = [0]

        def alive():
            ticks[0] += 1
            return ticks[0] <= 5

        def run_task(task, browser, io_, messages, **_k):
            seen.append(messages)
            if task == "첫 작업":
                messages.retain("extract", "https://example.com/a", "유휴 종료 뒤에도 남을 자료", set())

        def idle():
            # 유휴 종료 조건을 넘긴 뒤 다음 작업이 들어온다고 가정한다
            clock[0] += main.BG_IDLE_SEC + 1
            if ticks[0] == 2:
                io.tasks.put("다음 작업")

        with patch.object(main.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(main, "run_task", side_effect=run_task):
            main._task_loop(br, io, alive, idle)
        br.close_background.assert_called()
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], seen[1])
        self.assertIn("R0001", seen[1].archive)


class LlmBodySerializationTests(unittest.TestCase):
    SECRET = "sk-test-secret-123"

    def test_retain_masks_secrets_before_archiving_text_and_url(self):
        hist = agent.History()
        secret = self.SECRET
        hist.retain("extract", f"https://example.com/{secret}", f"본문 {secret} 끝", {secret})
        archived = hist.archive["R0001"]
        self.assertNotIn(secret, archived["text"])
        self.assertNotIn(secret, archived["url"])

    def test_recall_catalog_paginates_and_reconstructs_full_listing(self):
        hist = agent.History()
        for i in range(3):
            hist.retain("extract", f"https://example.com/{i}/{'x' * 3000}", "본문", set())
        listing = "\n".join(
            f"{entry['id']} | {entry['tool']} | {entry['url']} | {len(entry['text'])}자"
            for entry in hist.archive.values()
        )
        first = hist.recall({}, set())
        marker = "\n다음 offset: "
        self.assertIn(marker, first)
        first_chunk, offset_text = first.rsplit(marker, 1)
        offset = int(offset_text)
        second = hist.recall({"offset": offset}, set())
        self.assertEqual(first_chunk + second, listing)

    def test_system_prompt_keeps_recall_and_untrusted_archive_rule(self):
        self.assertIn("recall", llm.SYSTEM)
        self.assertIn("신뢰할 수 없는 자료", llm.SYSTEM)

    def _serialize(self, messages):
        captured = {}

        def fake_post(body, cenv, cancel=None):
            captured["body"] = body
            return {"content": []}

        with patch.object(llm, "_post", side_effect=fake_post), \
                patch.object(llm, "_claude_env", return_value={}), \
                patch.object(llm, "_system_blocks", return_value=[{"type": "text", "text": "sys"}]):
            llm.chat(messages, [{"name": "done", "input_schema": {}}])
        return json.loads(json.dumps(captured["body"]))

    def test_post_body_has_only_standard_fields_and_paired_tool_results(self):
        hist = agent.History()
        hist.append({"role": "user", "content": "작업"})
        hist.retain("extract", "https://example.com/a", "보관된 긴 자료 " * 500, set())
        hist.append({"role": "assistant", "content": [
            {"type": "text", "text": "조사 중"},
            {"type": "tool_use", "id": "t1", "name": "extract", "input": {}}]})
        hist.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "관찰 " * 50}]})
        agent._stub_observations(hist)
        body = self._serialize(hist)
        self.assertEqual(set(body), {"model", "max_tokens", "system", "messages", "tools"})
        for m in body["messages"]:
            self.assertEqual(set(m), {"role", "content"})
        uses = [b["id"] for m in body["messages"] if m["role"] == "assistant"
                for b in m["content"] if b.get("type") == "tool_use"]
        results = [b["tool_use_id"] for m in body["messages"] if m["role"] == "user"
                   and isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        self.assertEqual(uses, ["t1"])
        self.assertEqual(results, ["t1"])
        text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("보관된 긴 자료", text)
        self.assertNotIn("archive", text)

    def test_recalled_secret_is_masked_in_request_body(self):
        hist = agent.History()
        hist.append({"role": "user", "content": "작업"})
        hist.retain("extract", "https://example.com/a", f"키는 {self.SECRET} 입니다", {self.SECRET})
        out = hist.recall({"source": "R0001", "offset": 0}, {self.SECRET})
        self.assertIn("입니다", out)
        self.assertNotIn(self.SECRET, out)
        hist.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "recall", "input": {"source": "R0001", "offset": 0}}]})
        hist.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": out}]})
        body = self._serialize(hist)
        self.assertNotIn(self.SECRET, json.dumps(body, ensure_ascii=False))


class ChatLoopHistoryTests(unittest.TestCase):
    def test_chat_loop_reuses_one_history_across_inputs(self):
        io = Mock()
        seen = []

        def run_task(line, browser, io_, messages, **_k):
            seen.append(messages)
            if line == "첫 입력":
                messages.retain("extract", "https://example.com/a", "대화 루프 자료", set())

        inputs = iter(["첫 입력", "둘째 입력", "exit"])
        with patch.object(main, "run_task", side_effect=run_task), \
                patch("builtins.input", side_effect=lambda *_a: next(inputs)):
            main.chat_loop(_browser(), io)
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], seen[1])
        self.assertIn("R0001", seen[1].archive)


if __name__ == "__main__":
    unittest.main()
