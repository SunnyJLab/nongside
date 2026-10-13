"""Regression tests for the app-owned background browser launch."""

import ctypes
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
        self.kill = Mock()

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
            browser.time, "monotonic", side_effect=self._ticks()
        ), patch.object(browser.time, "sleep"), patch.object(
            browser.Browser, "_profile_process_ids", return_value=[]
        ):
            result = None
            try:
                result = agent_browser._launch_background("msedge")
            except Exception as e:
                result = e
        return agent_browser, result

    @staticmethod
    def _ticks():
        tick = [0]
        def advance():
            tick[0] += 1
            return tick[0]
        return advance

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
        self.assertIn(f"--user-data-dir={profile.resolve()}", command)
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
        self.assertEqual(connected.context_kwargs["viewport"], {"width": 1280, "height": 900})
        self.assertIn("--window-size=1280,900", command)
        self.assertEqual(agent_browser._bg_profile, profile.resolve())

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
        ctx = agent_browser.ctx
        playwright = agent_browser._pw
        playwright.stop = Mock()
        agent_browser.close()

        attached.close.assert_not_called()
        ctx.close.assert_not_called()
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

    def test_close_kills_popen_child_after_both_waits_time_out(self):
        agent_browser, connected = self._browser()
        proc = _FakeProcess()
        timeout = browser.subprocess.TimeoutExpired("browser", 3)
        proc.wait.side_effect = [timeout, timeout]
        agent_browser._bg = connected
        agent_browser._bg_process = proc
        with patch.object(browser.Browser, "_profile_process_ids", return_value=[]):
            agent_browser._close_bg()
        proc.kill.assert_called_once_with()
        self.assertEqual(proc.wait.call_count, 2)

    def test_profile_process_scan_requires_exact_macos_user_data_dir_token(self):
        profile = Path("/tmp/nongside-bg-target")
        output = ("101 /Chrome --user-data-dir=/tmp/nongside-bg-target\n"
                  "102 /Chrome --user-data-dir=/tmp/nongside-bg-target-decoy\n"
                  "103 /Chrome --user-data-dir=/tmp/nongside-bg-target-extra\n")
        agent_browser = browser.Browser()
        agent_browser._bg_profile = profile
        with patch.object(browser.sys, "platform", "darwin"), patch.object(
            Path, "is_dir", return_value=False
        ), patch.object(browser.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)):
            self.assertEqual(agent_browser._profile_process_ids(), [101])

    def test_close_never_rmtree_outside_tempdir_or_without_prefix(self):
        for profile_name, allowed_root in (("nongside-bg-safe", "elsewhere"),
                                           ("unrelated-profile", "same")):
            with self.subTest(profile_name=profile_name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                profile = root / profile_name
                profile.mkdir()
                agent_browser, connected = self._browser()
                agent_browser._bg = connected
                agent_browser.bg_ctx = connected.context
                agent_browser._bg_profile = profile
                temp_root = root / "elsewhere" if allowed_root == "elsewhere" else root
                with patch.object(browser.tempfile, "gettempdir", return_value=str(temp_root)), patch.object(
                    browser.Browser, "_profile_process_ids", return_value=[]
                ):
                    agent_browser._close_bg()
                self.assertTrue(profile.exists())

    def test_kill_bg_processes_never_kills_its_own_pid(self):
        agent_browser = browser.Browser()
        with patch.object(browser.sys, "platform", "linux"), patch.object(browser.os, "kill") as kill:
            agent_browser._kill_bg_processes({os.getpid(), 987654})
        kill.assert_called_once_with(987654, 9)

    def test_bad_port_and_bad_websocket_path_are_independently_rejected(self):
        endpoints = ("65536\n/devtools/browser/12345678-1234-1234-1234-123456789abc",
                     "12345\n/devtools/browser/not-a-uuid")
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
                try:
                    _, result = self._launch(profile, _FakeProcess(), endpoint_text=endpoint)
                    self.assertIsInstance(result, TimeoutError)
                    self.assertFalse(profile.exists())
                finally:
                    if profile.exists():
                        import shutil
                        shutil.rmtree(profile)

    def test_unowned_cdp_browser_is_disconnected_without_browser_close(self):
        agent_browser, connected = self._browser()
        proc = _FakeProcess(pid=1234)
        connected.browser_cdp_session.send.side_effect = lambda command: (
            {"processInfo": [{"type": "browser", "id": 9999}]} if command == "SystemInfo.getProcessInfo" else {}
        )
        profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
        (profile / "DevToolsActivePort").write_text(
            "12345\n/devtools/browser/12345678-1234-1234-1234-123456789abc", encoding="utf-8"
        )
        try:
            with patch.object(browser.Browser, "_browser_executable", return_value="Chrome"), patch.object(
                browser.tempfile, "mkdtemp", return_value=str(profile)
            ), patch.object(browser.subprocess, "Popen", return_value=proc), patch.object(
                browser.time, "monotonic", side_effect=self._ticks()
            ), patch.object(browser.time, "sleep"), patch.object(
                browser.Browser, "_profile_process_ids", return_value=[]
            ):
                with self.assertRaisesRegex(RuntimeError, "소유권"):
                    agent_browser._launch_background("chrome")
            connected.close.assert_called_once_with()
            connected.browser_cdp_session.send.assert_called_once_with("SystemInfo.getProcessInfo")
            self.assertIsNone(agent_browser._bg)
        finally:
            if profile.exists():
                import shutil
                shutil.rmtree(profile)

    def test_owner_file_is_written_after_profile_creation(self):
        import json
        profile = Path(tempfile.mkdtemp(prefix="nongside-bg-test-"))
        try:
            agent_browser, result = self._launch(
                profile, _FakeProcess(), endpoint_text=
                "12345\n/devtools/browser/12345678-1234-1234-1234-123456789abc")
            self.assertNotIsInstance(result, Exception)
            self.assertEqual(json.loads((profile / browser.OWNER_FILE).read_text()), {"pid": os.getpid()})
            with patch.object(browser.Browser, "_profile_process_ids", return_value=[]):
                agent_browser._close_bg()
        finally:
            if profile.exists():
                import shutil
                shutil.rmtree(profile)

    def test_reap_stale_profiles_kills_dead_owned_profile(self):
        with tempfile.TemporaryDirectory() as td:
            stale = Path(td) / "nongside-bg-stale"
            stale.mkdir()
            (stale / browser.OWNER_FILE).write_text('{"pid": 12}')
            agent_browser = browser.Browser()
            with patch.object(browser.tempfile, "gettempdir", return_value=td), patch.object(
                browser.Browser, "_pid_alive", return_value=False
            ), patch.object(browser.Browser, "_profile_process_ids", side_effect=[[77], []]) as scan, patch.object(
                browser.Browser, "_kill_bg_processes"
            ) as kill, patch.object(browser.time, "monotonic", side_effect=self._ticks()), patch.object(
                browser.time, "sleep"
            ):
                agent_browser._reap_stale_profiles()
            kill.assert_called_once_with({77})
            self.assertFalse(stale.exists())
            scan.assert_any_call(stale.resolve())

    def test_reap_resolves_tempdir_symlink_before_scanning_and_preserves_live_profile(self):
        with tempfile.TemporaryDirectory() as td:
            actual = Path(td) / "actual-temp"
            actual.mkdir()
            temp_link = Path(td) / "temp-link"
            temp_link.symlink_to(actual, target_is_directory=True)
            profile = actual / "nongside-bg-orphan"
            profile.mkdir()
            (profile / browser.OWNER_FILE).write_text('{"pid": 123}')
            canonical = profile.resolve()
            ps_output = f"77 /Chrome --user-data-dir={canonical}\n"
            agent_browser = browser.Browser()
            ticks = [0]

            def advance():
                ticks[0] += 1
                return ticks[0]

            with patch.object(browser.tempfile, "gettempdir", return_value=str(temp_link)), patch.object(
                browser.Browser, "_pid_alive", return_value=False
            ), patch.object(browser.sys, "platform", "darwin"), patch.object(
                browser.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=ps_output
            )) as ps, patch.object(browser.Browser, "_kill_bg_processes") as kill, patch.object(
                browser.time, "monotonic", side_effect=advance
            ), patch.object(browser.time, "sleep"):
                agent_browser._reap_stale_profiles()

            kill.assert_called_once_with({77})
            ps.assert_called()
            self.assertTrue(profile.exists(), "live Chrome must keep its profile")


    def test_reap_preserves_live_unmarked_and_unscannable_profiles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live, unmarked, unscannable = [root / name for name in
                                            ("nongside-bg-live", "nongside-bg-old", "nongside-bg-unknown")]
            for profile in (live, unmarked, unscannable):
                profile.mkdir()
            (live / browser.OWNER_FILE).write_text('{"pid": 10}')
            (unscannable / browser.OWNER_FILE).write_text('{"pid": 30}')
            agent_browser = browser.Browser()
            with patch.object(browser.tempfile, "gettempdir", return_value=td), patch.object(
                browser.Browser, "_pid_alive", side_effect=lambda pid: pid == 10
            ), patch.object(browser.Browser, "_profile_process_ids", return_value=None):
                agent_browser._reap_stale_profiles()
            self.assertTrue(all(profile.exists() for profile in (live, unmarked, unscannable)))

    def test_prepare_skips_cookies_but_default_page_syncs_them(self):
        agent_browser, connected = self._browser()
        agent_browser.ctx.cookies.return_value = [{"name": "sid", "value": "secret"}]
        with patch.object(browser.Browser, "_launch_background", return_value=connected):
            agent_browser.prepare_background()
            self.assertEqual(connected.context.cookies, [])
            agent_browser._bg_page(fresh=False)
        self.assertEqual(connected.context.cookies, [{"name": "sid", "value": "secret"}])

    def test_close_background_return_value_and_cleanup(self):
        agent_browser, _ = self._browser()
        self.assertFalse(agent_browser.close_background())
        agent_browser.bg_ctx = _FakeContext()
        with patch.object(agent_browser, "_close_bg") as close:
            self.assertTrue(agent_browser.close_background())
        close.assert_called_once_with()

    def test_job_structure_size_and_non_windows_helper(self):
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(browser._JOBOBJECT_EXTENDED_LIMIT_INFORMATION), 144)
        with patch.object(browser.sys, "platform", "darwin"):
            self.assertIsNone(browser._kill_on_exit_job(_FakeProcess()))

    def test_profile_mask_and_windows_pid_alive_never_calls_os_kill(self):
        self.assertEqual(browser._mask_profile("failed at /tmp/nongside-bg-abc/file"), "failed at <profile>")
        kernel = Mock()
        kernel.OpenProcess.return_value = 1
        kernel.GetExitCodeProcess.side_effect = lambda handle, code: (setattr(code._obj, "value", 259) or 1)
        with patch.object(browser.sys, "platform", "win32"), patch.object(
            browser.ctypes, "WinDLL", return_value=kernel, create=True
        ), patch.object(browser.os, "kill") as kill:
            self.assertTrue(browser.Browser._pid_alive(5))
        kill.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(1)

    def test_windows_pid_alive_distinguishes_invalid_pid_from_access_denied(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 0
        with patch.object(browser.sys, "platform", "win32"), patch.object(
            browser.ctypes, "WinDLL", return_value=kernel, create=True
            ), patch.object(browser.ctypes, "get_last_error", return_value=87, create=True):
            self.assertFalse(browser.Browser._pid_alive(999999))
        with patch.object(browser.sys, "platform", "win32"), patch.object(
            browser.ctypes, "WinDLL", return_value=kernel, create=True
            ), patch.object(browser.ctypes, "get_last_error", return_value=5, create=True):
            self.assertTrue(browser.Browser._pid_alive(999999))

    def test_close_handle_sets_pointer_sized_argument_type(self):
        kernel = Mock()
        with patch.object(browser.ctypes, "WinDLL", return_value=kernel, create=True):
            browser._close_handle(0x1234)
        self.assertEqual(kernel.CloseHandle.argtypes, [ctypes.c_void_p])
        kernel.CloseHandle.assert_called_once_with(0x1234)

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

    def test_reap_ignores_profile_symlink_pointing_outside_tempdir(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            foreign = Path(outside) / "nongside-bg-foreign"
            foreign.mkdir()
            (foreign / browser.OWNER_FILE).write_text('{"pid": 12}')
            (Path(td) / "nongside-bg-link").symlink_to(foreign, target_is_directory=True)
            agent_browser = browser.Browser()
            with patch.object(browser.tempfile, "gettempdir", return_value=td), patch.object(
                browser.Browser, "_pid_alive", return_value=False
            ), patch.object(browser.Browser, "_profile_process_ids") as scan, patch.object(
                browser.Browser, "_kill_bg_processes") as kill:
                agent_browser._reap_stale_profiles()
            scan.assert_not_called()
            kill.assert_not_called()
            self.assertTrue(foreign.exists())

    def test_reap_preserves_profile_while_owner_process_is_alive(self):
        with tempfile.TemporaryDirectory() as td:
            live = Path(td) / "nongside-bg-live"
            live.mkdir()
            (live / browser.OWNER_FILE).write_text('{"pid": 10}')
            agent_browser = browser.Browser()
            with patch.object(browser.tempfile, "gettempdir", return_value=td), patch.object(
                browser.Browser, "_pid_alive", return_value=True
            ), patch.object(browser.Browser, "_profile_process_ids", return_value=[]) as scan, patch.object(
                browser.Browser, "_kill_bg_processes") as kill:
                agent_browser._reap_stale_profiles()
            scan.assert_not_called()
            kill.assert_not_called()
            self.assertTrue(live.exists())

    def test_kill_on_exit_job_does_not_touch_kernel32_off_windows(self):
        with patch.object(browser.sys, "platform", "darwin"), patch.object(
            browser.ctypes, "WinDLL", create=True
        ) as windll:
            self.assertIsNone(browser._kill_on_exit_job(_FakeProcess()))
        windll.assert_not_called()


if __name__ == "__main__":
    unittest.main()
