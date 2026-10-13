"""Regression tests for the app-owned background browser launch."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import browser


class _FakePage:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed


class _FakeContext:
    def __init__(self):
        self.pages = []
        self.on_calls = []
        self.init_scripts = []
        self.cookies = []
        self.new_page_result = _FakePage()
        self.close = Mock()

    def on(self, event, handler):
        self.on_calls.append((event, handler))

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    def new_page(self):
        self.pages.append(self.new_page_result)
        return self.new_page_result


class _FakeConnectedBrowser:
    def __init__(self):
        self.context = _FakeContext()
        self.context_kwargs = None
        def send(command):
            if command == "SystemInfo.getProcessInfo":
                return {"processInfo": [{"type": "browser", "id": 4321}]}
            return {}

        self.browser_cdp_session = SimpleNamespace(send=Mock(side_effect=send), detach=Mock())
        self.close = Mock()

    def is_connected(self):
        return True

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self.context

    def new_browser_cdp_session(self):
        return self.browser_cdp_session


class _FakeProcess:
    def __init__(self, pid=4321):
        self.pid = pid
        self.returncode = None
        self.wait = Mock()

    def poll(self):
        return self.returncode


class OwnedBackgroundLaunchTests(unittest.TestCase):
    def _browser(self):
        agent_browser = browser.Browser()
        connected = _FakeConnectedBrowser()
        agent_browser._pw = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=Mock(return_value=connected))
        )
        agent_browser.user_page = None
        agent_browser.ctx = Mock()
        agent_browser.ctx.cookies.return_value = []
        return agent_browser, connected

    def _launch(self, profile, proc, *, endpoint_text=None, connect_error=None):
        profile.mkdir(parents=True, exist_ok=True)
        if endpoint_text is not None:
            (profile / "DevToolsActivePort").write_text(endpoint_text, encoding="utf-8")
        agent_browser, _ = self._browser()
        if connect_error:
            agent_browser._pw.chromium.connect_over_cdp.side_effect = connect_error
        with patch.object(browser.Browser, "_browser_executable", return_value="C:\\Edge\\msedge.exe"), patch.object(
            browser.tempfile, "mkdtemp", return_value=str(profile)
        ), patch.object(browser.subprocess, "Popen", return_value=proc), patch.object(
            browser.time, "monotonic", side_effect=[0, 0, 0, 0, 11, 11]
        ), patch.object(browser.time, "sleep"), patch.object(
            browser.Browser, "_profile_process_ids", return_value=[]
        ):
            result = None
            try:
                result = agent_browser._launch_background("msedge")
            except Exception as e:
                result = e
        return agent_browser, result

    def test_background_launch_uses_owned_headless_process(self):
        agent_browser, connected = self._browser()
        proc = _FakeProcess()

        with tempfile.TemporaryDirectory() as td:
            profile = Path(td) / "profile"
            profile.mkdir()
            (profile / "DevToolsActivePort").write_text(
                "45678\n/devtools/browser/12345678-1234-1234-1234-123456789abc",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"AGENT_CHANNEL": "msedge"}), patch.object(
                browser.Browser, "_browser_executable", return_value="C:\\Edge\\msedge.exe"
            ), patch.object(browser.tempfile, "mkdtemp", return_value=str(profile)), patch.object(
                browser.subprocess, "Popen", return_value=proc
            ) as popen, patch.object(browser.Browser, "_profile_process_ids", return_value=[]):
                page = agent_browser._bg_page(fresh=True)

        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "C:\\Edge\\msedge.exe")
        self.assertIn("--headless=new", command)
        self.assertIn("--remote-debugging-port=0", command)
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertFalse(any("remote-debugging-pipe" in arg for arg in command))

        endpoint = "ws://127.0.0.1:45678/devtools/browser/12345678-1234-1234-1234-123456789abc"
        agent_browser._pw.chromium.connect_over_cdp.assert_called_once()
        self.assertEqual(agent_browser._pw.chromium.connect_over_cdp.call_args.args[0], endpoint)

        self.assertIs(page, connected.context.new_page_result)
        self.assertEqual(connected.context_kwargs["color_scheme"], "null")
        self.assertIn(browser._TAB_POPUP_JS, connected.context.init_scripts)
        self.assertIn("page", [call[0] for call in connected.context.on_calls])
        self.assertEqual(agent_browser.bg_channel, "msedge")
        self.assertEqual(agent_browser._bg_pid, 4321)
        connected.browser_cdp_session.send.assert_any_call("SystemInfo.getProcessInfo")
        self.assertIs(agent_browser._bg_process, proc)
        self.assertEqual(connected.context_kwargs["viewport"], browser.BG_SIZE)
        self.assertEqual(agent_browser._bg_profile, profile)

    def test_background_launch_falls_back_to_next_channel(self):
        agent_browser, connected = self._browser()
        with patch.dict(os.environ, {"AGENT_CHANNEL": "msedge"}), patch.object(
            browser.Browser,
            "_launch_background",
            side_effect=[RuntimeError("launch failed"), connected],
        ) as launch:
            page = agent_browser._bg_page(fresh=True)

        self.assertIs(page, connected.context.new_page_result)
        self.assertEqual(
            [call.args[0] for call in launch.call_args_list],
            ["msedge", "chrome"],
        )
        self.assertEqual(agent_browser.bg_channel, "chrome")

    def test_zero_exit_launcher_can_handoff_to_browser_process(self):
        profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
        proc = _FakeProcess()
        proc.returncode = 0
        endpoint = "12345\n/devtools/browser/12345678-1234-1234-1234-123456789abc"
        try:
            agent_browser, result = self._launch(profile, proc, endpoint_text=endpoint)
            self.assertNotIsInstance(result, Exception)
            self.assertEqual(agent_browser._bg_pid, 4321)
            self.assertIs(agent_browser._bg_process, proc)
        finally:
            with patch.object(browser.Browser, "_profile_process_ids", return_value=[]):
                agent_browser._close_bg()
            if profile.exists():
                import shutil
                shutil.rmtree(profile)

    def test_malformed_devtools_endpoint_is_rejected_and_cleaned(self):
        profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
        proc = _FakeProcess()
        try:
            agent_browser, result = self._launch(
                profile, proc, endpoint_text="99999\n/devtools/browser/not-a-uuid"
            )
            self.assertIsInstance(result, TimeoutError)
            self.assertFalse(profile.exists())
            self.assertIsNone(agent_browser._bg_profile)
        finally:
            if profile.exists():
                import shutil
                shutil.rmtree(profile)

    def test_empty_browser_process_info_is_rejected_and_cleaned(self):
        profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
        proc = _FakeProcess()
        agent_browser, connected = self._browser()
        connected.browser_cdp_session.send.side_effect = lambda command: {"processInfo": []} if command == "SystemInfo.getProcessInfo" else {}
        profile.mkdir(exist_ok=True)
        (profile / "DevToolsActivePort").write_text(
            "12345\n/devtools/browser/12345678-1234-1234-1234-123456789abc", encoding="utf-8"
        )
        try:
            with patch.object(browser.Browser, "_browser_executable", return_value="C:\\Edge\\msedge.exe"), patch.object(
                browser.tempfile, "mkdtemp", return_value=str(profile)
            ), patch.object(browser.subprocess, "Popen", return_value=proc), patch.object(
                browser.Browser, "_profile_process_ids", return_value=[]
            ):
                with self.assertRaisesRegex(RuntimeError, "소유권"):
                    agent_browser._launch_background("msedge")
            self.assertFalse(profile.exists())
            self.assertIsNone(agent_browser._bg_pid)
        finally:
            if profile.exists():
                import shutil
                shutil.rmtree(profile)

    def test_user_agent_locale_cookies_and_viewport_are_applied(self):
        agent_browser, connected = self._browser()
        page = Mock()
        page.is_closed.return_value = False
        page.evaluate.return_value = ["Mozilla/5.0 HeadlessChrome/123", "ko-KR"]
        agent_browser.user_page = page
        agent_browser.ctx.cookies.return_value = [{"name": "sid", "value": "test"}]
        with patch.object(browser.Browser, "_launch_background", return_value=connected):
            agent_browser._bg_page(fresh=True)
        self.assertEqual(connected.context_kwargs["user_agent"], "Mozilla/5.0 Chrome/123")
        self.assertEqual(connected.context_kwargs["locale"], "ko-KR")
        self.assertEqual(connected.context_kwargs["viewport"], browser.BG_SIZE)
        self.assertEqual(connected.context.cookies, [{"name": "sid", "value": "test"}])

    def test_empty_cookie_list_and_invalid_cookies_do_not_block_page_creation(self):
        agent_browser, connected = self._browser()
        add_cookies = Mock(side_effect=ValueError("invalid cookie"))
        connected.context.add_cookies = add_cookies
        agent_browser.ctx.cookies.return_value = [{"name": "bad", "value": "cookie"}]
        with patch.object(browser.Browser, "_launch_background", return_value=connected):
            page = agent_browser._bg_page(fresh=True)
        self.assertIs(page, connected.context.new_page_result)
        self.assertEqual(add_cookies.call_count, 2)

    def test_context_creation_failure_falls_back_to_next_channel(self):
        agent_browser, connected = self._browser()
        broken = _FakeConnectedBrowser()
        broken.new_context = Mock(side_effect=RuntimeError("context failed"))
        with patch.dict(os.environ, {"AGENT_CHANNEL": "msedge"}), patch.object(
            browser.Browser, "_launch_background", side_effect=[broken, connected]
        ), patch.object(browser.Browser, "_profile_process_ids", return_value=[]):
            page = agent_browser._bg_page(fresh=True)
        self.assertIs(page, connected.context.new_page_result)
        self.assertEqual(agent_browser.bg_channel, "chrome")
        self.assertIsNone(agent_browser._bg_profile)

    def test_exception_reading_user_context_keeps_default_context_settings(self):
        agent_browser, connected = self._browser()
        page = Mock()
        page.is_closed.return_value = False
        page.evaluate.side_effect = RuntimeError("closed tab")
        agent_browser.user_page = page
        agent_browser.ctx.cookies.side_effect = RuntimeError("closed context")
        with patch.object(browser.Browser, "_launch_background", return_value=connected):
            result = agent_browser._bg_page(fresh=True)
        self.assertIs(result, connected.context.new_page_result)
        self.assertIsNone(connected.context_kwargs["user_agent"])
        self.assertIsNone(connected.context_kwargs["locale"])
        self.assertEqual(connected.context_kwargs["viewport"], browser.BG_SIZE)

    def test_close_background_closes_context_and_owned_browser(self):
        agent_browser, connected = self._browser()
        proc = _FakeProcess()

        profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
        try:
            agent_browser.bg_ctx = connected.context
            agent_browser._bg = connected
            agent_browser._bg_process = proc
            agent_browser._bg_profile = profile

            with patch.object(browser.Browser, "_profile_process_ids", return_value=[]):
                agent_browser._close_bg()

            self.assertFalse(profile.exists())
        finally:
            if profile.exists():
                import shutil
                shutil.rmtree(profile)

        connected.context.close.assert_called_once_with()
        connected.browser_cdp_session.send.assert_called_once_with("Browser.close")
        self.assertGreaterEqual(proc.wait.call_count, 1)
        self.assertIsNone(agent_browser.bg_ctx)
        self.assertIsNone(agent_browser._bg)
        self.assertIsNone(agent_browser._bg_process)
        self.assertIsNone(agent_browser._bg_profile)

    def test_startup_timeout_closes_owned_process_and_removes_temp_profile(self):
        profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
        proc = _FakeProcess()
        try:
            agent_browser, result = self._launch(profile, proc)
            self.assertIsInstance(result, TimeoutError)
            proc.wait.assert_called()
            self.assertFalse(profile.exists())
            self.assertIsNone(agent_browser._bg_process)
            self.assertIsNone(agent_browser._bg_profile)
        finally:
            if profile.exists():
                import shutil
                shutil.rmtree(profile)

    def test_early_child_exit_cleans_profile(self):
        profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
        proc = _FakeProcess()
        proc.returncode = 3
        try:
            agent_browser, result = self._launch(profile, proc, endpoint_text=None)
            self.assertIsInstance(result, RuntimeError)
            self.assertFalse(profile.exists())
            self.assertIsNone(agent_browser._bg_process)
        finally:
            if profile.exists():
                import shutil
                shutil.rmtree(profile)

    def test_user_attached_browser_is_never_closed_by_agent_close(self):
        agent_browser, attached = self._browser()
        agent_browser._browser = attached
        playwright = agent_browser._pw
        playwright.stop = Mock()
        agent_browser.close()

        attached.close.assert_not_called()
        self.assertEqual(attached.browser_cdp_session.send.call_count, 0)
        playwright.stop.assert_called_once_with()

    def test_close_terminates_only_owned_process_if_browser_close_does_not_exit(self):
        agent_browser, connected = self._browser()
        proc = _FakeProcess()
        proc.wait.side_effect = [browser.subprocess.TimeoutExpired("browser", 3), None]
        profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
        try:
            agent_browser._bg = connected
            agent_browser.bg_ctx = connected.context
            agent_browser._bg_process = proc
            agent_browser._bg_profile = profile
            scan = Mock(side_effect=[[proc.pid], [proc.pid], []])
            ticks = iter([0, 0, 4, 4])
            with patch.object(browser.Browser, "_profile_process_ids", scan), patch.object(
                browser.time, "monotonic", side_effect=lambda: next(ticks)
            ), patch.object(browser.Browser, "_kill_bg_processes") as kill_owned:
                agent_browser._close_bg()

            self.assertEqual(kill_owned.call_count, 1)
            self.assertTrue(all(call.args == ({proc.pid},) for call in kill_owned.call_args_list))
            self.assertFalse(profile.exists())
        finally:
            if profile.exists():
                import shutil
                shutil.rmtree(profile)

    def test_process_scan_failure_preserves_temporary_profile(self):
        agent_browser, connected = self._browser()
        profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
        agent_browser._bg = connected
        agent_browser.bg_ctx = connected.context
        agent_browser._bg_profile = profile
        try:
            with patch.object(browser.Browser, "_profile_process_ids", return_value=None):
                agent_browser._close_bg()
            self.assertTrue(profile.exists())
            self.assertIsNone(agent_browser._bg_profile)
        finally:
            if profile.exists():
                import shutil
                shutil.rmtree(profile)


if __name__ == "__main__":
    unittest.main()
