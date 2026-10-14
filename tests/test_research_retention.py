"""Behavioral regressions for per-conversation research retention."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent
from memory import Memory


def _response(*uses):
    return {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": f"t{i}", "name": name, "input": args}
        for i, (name, args) in enumerate(uses, 1)]}


class _Browser:
    def __init__(self):
        self.url = "https://old.example/page"
        self.detached = False
        self.pinned = False
        self.pin_lost = False
        self.actions = []

    def screenshot(self):
        return b"jpeg"

    def observe(self):
        return f"URL: {self.url}\nTITLE: test\n[0] <button> \"발송\""

    def current_url(self):
        return self.url

    def act(self, name, args):
        self.actions.append((name, dict(args)))
        if name == "navigate":
            self.url = args["url"]
            return "navigated"
        if name == "extract":
            return args.get("result", "페이지 내용")
        if name == "click":
            return "sent"
        if name == "run_code":
            return args.get("result", "ok")
        return "ok"

    def focus_user_tab(self, *_):
        pass

    def pin_user_tab(self):
        self.pinned = True


class _IO:
    def __init__(self, secrets=()):
        self.secrets = set(secrets)
        self.stop_requested = None
        self.confirm_calls = 0

    def say(self, *_):
        pass

    def ask(self, *_):
        return "n"

    def confirm(self, *_):
        self.confirm_calls += 1
        return "y"


class ResearchRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = Memory(Path(self.tmp.name) / "sites.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, task, browser, io, messages, chat):
        with patch.object(agent.llm, "chat", side_effect=chat):
            return agent.run(task, browser, io, messages, mem=self.mem)

    def test_default_messages_are_history_inside_run(self):
        seen = []

        def chat(messages, _tools):
            seen.append(messages)
            return _response(("done", {"summary": "done"}),)

        result = self._run("기록 확인", _Browser(), _IO(), None, chat)
        self.assertEqual(result, "done")
        self.assertIsInstance(seen[0], agent.History)

    def test_tail_survives_compaction_for_followup_recall_in_same_history(self):
        browser, io = _Browser(), _IO()
        first_calls = [0]
        tail = "TOTAL=918273645"

        def first_chat(messages, _tools):
            first_calls[0] += 1
            if first_calls[0] == 1:
                return _response(("extract", {"result": "A" * 1900 + tail}),
                                 ("extract", {"result": "B" * 1900 + "END=771234"}))
            if first_calls[0] == 2:
                tools = [b for m in messages for b in (m.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "tool_result"]
                self.assertFalse(any(agent.TOOL_RESULT_STUB in b["content"] for b in tools))
                return _response(("find", {"text": "tail"}),)
            tools = [b for m in messages for b in (m.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "tool_result"]
            retained = [b["content"] for b in tools if "조사 ID: R" in b["content"]]
            self.assertTrue(all(agent.TOOL_RESULT_STUB in result for result in retained))
            self.assertEqual(re.findall(r"조사 ID: (R\d{4})", "\n".join(retained)), ["R0001", "R0002"])
            return _response(("done", {"summary": "자료 수집 완료"}),)

        history = agent.History()
        result = self._run("수치 수집", browser, io, history, first_chat)
        self.assertEqual(result, "자료 수집 완료")

        recall_calls = []

        def followup_chat(messages, _tools):
            recall_calls.append(messages)
            if len(recall_calls) == 1:
                tools = [b for m in messages for b in (m.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "tool_result"]
                self.assertTrue(any(agent.TOOL_RESULT_STUB in b["content"] for b in tools))
                ids = re.findall(r"조사 ID: (R\d{4})", "\n".join(b["content"] for b in tools))
                self.assertEqual(ids, ["R0001", "R0002"])
                return _response(("recall", {"source": ids[0]}),)
            self.assertIn(tail, repr(messages[-1]))
            self.assertTrue(all(set(m) == {"role", "content"} for m in messages))
            return _response(("done", {"summary": "초안 작성에 필요한 수치를 확인함"}),)

        result = self._run("수집한 수치로 초안 작성", browser, io, history, followup_chat)
        self.assertIn("수치를 확인", result)

    def test_screenshot_fallback_keeps_destination_url_and_recall_id_after_shrink(self):
        history, browser, io = agent.History(), _Browser(), _IO()
        destination = "https://new.example/chart"
        calls = [0]

        def chat(messages, _tools):
            calls[0] += 1
            if calls[0] == 1:
                return _response(("navigate", {"url": destination}), ("screenshot", {}))
            if calls[0] == 2:
                return _response(("done", {"summary": "자료 수집 완료"}),)
            tool_results = [b["content"] for m in messages for b in (m.get("content") or [])
                            if isinstance(b, dict) and b.get("type") == "tool_result"]
            self.assertTrue(any(agent.TOOL_RESULT_STUB in result for result in tool_results))
            self.assertTrue(any("조사 ID: R0001" in result for result in tool_results))
            if calls[0] == 3:
                return _response(("recall", {"source": "R0001"}),)
            return _response(("done", {"summary": "재확인 완료"}),)

        # Make the fallback extract large enough to exercise compaction.
        original_act = browser.act
        def act(name, args):
            if name == "extract":
                return "CHART=" + "x" * 1900
            return original_act(name, args)
        browser.act = act
        with patch.object(agent.llm, "vision_model", return_value=False), \
                patch.object(agent.llm, "chat", side_effect=chat):
            first = agent.run("차트 확인", browser, io, history, mem=self.mem)
            second = agent.run("차트 다시 확인", browser, io, history, mem=self.mem)
        self.assertEqual(first, "자료 수집 완료")
        self.assertEqual(second, "재확인 완료")
        self.assertEqual(history.archive["R0001"]["url"], destination)
        self.assertIn("CHART=", history.recall({"source": "R0001"}, set()))

    def test_vision_result_is_capped_in_messages_but_full_text_is_recallable(self):
        history, browser, io = agent.History(), _Browser(), _IO()
        full = "VISION=" + "v" * (agent.VISION_CAP + 300) + "VISION_TAIL=5629"
        calls = []

        def chat(messages, _tools):
            calls.append(messages)
            if len(calls) == 1:
                return _response(("extract", {}),)
            if len(calls) == 2:
                return _response(("screenshot", {}),)
            if len(calls) == 3:
                result = next(b["content"] for m in messages for b in (m.get("content") or [])
                              if isinstance(b, dict) and b.get("type") == "tool_result"
                              and "비전 모델의 화면 판독 결과" in b.get("content", ""))
                self.assertIn("조사 ID: R0002", result)
                self.assertEqual(len(result.split("\n", 1)[1].split("\n조사 ID:", 1)[0]), agent.VISION_CAP)
                return _response(("recall", {"source": "R0002", "offset": agent.VISION_CAP}),)
            self.assertIn("VISION_TAIL=5629", repr(messages[-1]))
            return _response(("done", {"summary": "비전 판독 확인"}),)

        with patch.object(agent.llm, "vision_model", return_value=True), \
                patch.object(agent.llm, "vision", return_value=full), \
                patch.object(agent.llm, "chat", side_effect=chat):
            result = agent.run("화면 확인", browser, io, history, mem=self.mem)
        self.assertIn("비전 판독", result)
        self.assertEqual(len(history.archive["R0002"]["text"]), len(full))

    def test_ask_user_secret_is_not_archived(self):
        secret = "secret-value-8921"
        io = _IO(secrets={secret})
        io.ask_fields = lambda *_: f"token: {secret}"
        history = agent.History()
        responses = iter([
            _response(("ask_user", {"question": "토큰 입력", "fields": [{"name": "token", "secret": True}]}),),
            _response(("done", {"summary": "입력 완료"}),),
        ])
        self._run("토큰 입력", _Browser(), io, history, lambda *_: next(responses))
        self.assertEqual(history.archive, {})
        self.assertNotIn(secret, repr(history.archive))

    def test_same_history_can_recall_and_separate_history_cannot_leak(self):
        h1, h2 = agent.History(), agent.History()
        h1.retain("extract", "https://one.example", "private finding 123", set())
        self.assertIn("private finding 123", h1.recall({"source": "R0001"}, set()))
        self.assertIn("error:", h2.recall({"source": "R0001"}, set()))
        h1.clear()
        self.assertEqual(h1, [])
        self.assertEqual(h1.archive, {})
        self.assertEqual(h1.archive_chars, 0)

    def test_catalog_and_source_pagination_validate_offsets_and_sources(self):
        h = agent.History()
        h.retain("extract", "https://example.test", "x" * (agent.RECALL_CHUNK_SIZE + 10), set())
        catalog = h.recall({}, set())
        self.assertIn("R0001", catalog)
        first = h.recall({"source": "R0001"}, set())
        self.assertIn("다음 offset: 6000", first)
        second = h.recall({"source": "R0001", "offset": 6000}, set())
        self.assertIn("x" * 10, second)
        for bad in (-1, 1.2, "1", True):
            self.assertTrue(h.recall({"source": "R0001", "offset": bad}, set()).startswith("error:"))
        for bad_source in (None, [], {}):
            self.assertTrue(h.recall({"source": bad_source}, set()).startswith("error:"))
        self.assertTrue(h.recall({"source": "R9999"}, set()).startswith("error:"))

    def test_current_secrets_are_masked_without_changing_offsets_or_leaking_at_chunk_boundary(self):
        h = agent.History()
        source = "a" * (agent.RECALL_CHUNK_SIZE - 3) + "LONGSECRET" + " tail"
        h.retain("extract", "https://example.test/LONGSECRET", source, set())
        current_secrets = {"SECRET", "LONGSECRET"}
        page0 = h.recall({"source": "R0001"}, current_secrets)
        page1 = h.recall({"source": "R0001", "offset": agent.RECALL_CHUNK_SIZE - 3}, current_secrets)
        self.assertNotIn("LONGSECRET", page0 + page1)
        self.assertNotIn("SECRET", page0 + page1)
        self.assertIn("**********", page0 + page1)
        self.assertIn("LONGSECRET", h.archive["R0001"]["text"])  # masking is applied on recall

    def test_navigate_then_extract_archives_actual_destination_url(self):
        h, browser, io = agent.History(), _Browser(), _IO()
        def chat(messages, _tools):
            if not any(m["role"] == "assistant" for m in messages):
                return _response(("navigate", {"url": "https://new.example/report"}),
                                 ("extract", {"result": "report total 2468"}))
            return _response(("done", {"summary": "done"}),)
        self._run("보고서 확인", browser, io, h, chat)
        self.assertEqual(h.archive["R0001"]["url"], "https://new.example/report")

    def test_failed_extract_and_run_code_are_not_archived(self):
        h, browser, io = agent.History(), _Browser(), _IO()
        responses = iter([
            _response(("extract", {"result": "error: denied"}),
                      ("run_code", {"result": "error: skipped", "code": "print(0)"})),
            _response(("run_code", {"result": "error: failed", "code": "print(1)"}),),
            _response(("done", {"summary": "no result"}),),
        ])
        self._run("실패 확인", browser, io, h, lambda *_: next(responses))
        self.assertEqual([n for n, _ in browser.actions], ["extract", "run_code"])
        self.assertEqual(h.archive, {})

    def test_recall_does_not_clear_dangerous_action_verification_gate(self):
        h, browser, io = agent.History(), _Browser(), _IO()
        h.retain("extract", browser.url, "prior", set())
        responses = iter([
            _response(("click", {"index": 0}),),
            _response(("recall", {"source": "R0001"}), ("done", {"summary": "premature"})),
            _response(("extract", {}),),
            _response(("done", {"summary": "verified"}),),
        ])
        self._run("발송", browser, io, h, lambda *_: next(responses))
        self.assertEqual(io.confirm_calls, 1)
        self.assertEqual([n for n, _ in browser.actions], ["click", "extract"])

    def test_archive_capacity_warning_survives_compaction_and_blocks_later_entries(self):
        h = agent.History()
        h.retain("extract", "https://example.test", "x" * agent.ARCHIVE_MAX_CHARS, set())
        log = agent._Log(_IO())
        warning = agent._retain_research(h, "extract", "https://example.test", "not retained", set(), log)
        self.assertIsNotNone(warning)
        self.assertIn("보관되지 않았습니다", warning)
        tool_result = {"type": "tool_result", "tool_use_id": "t1",
                       "content": "x" * 1900 + "\n" + warning}
        messages = [{"role": "user", "content": [tool_result]}]
        agent._stub_observations(messages)
        self.assertIn(agent.TOOL_RESULT_STUB, tool_result["content"])
        self.assertIn("이후 자료도 보관되지 않습니다", h.recall({}, set()))
        self.assertTrue(h.archive_full)
        self.assertFalse(h.retain("extract", "https://example.test", "later", set()))
        self.assertNotIn("R0002", h.archive)

    def test_failed_extract_result_does_not_appear_in_catalog(self):
        h = agent.History()
        self.assertIsNone(agent._retain_research(h, "extract", "https://example.test", "error: no", set(),
                                                 agent._Log(_IO())))
        self.assertIn("보관된 조사 자료가 없습니다", h.recall({}, set()))


if __name__ == "__main__":
    unittest.main()
