import os
import sys
import time
from pathlib import Path

import agent
import llm
from browser import Browser


_TTY = sys.stdout.isatty()
_C_Q = "\033[36m" if _TTY else ""     # 질문: cyan
_C_W = "\033[1;33m" if _TTY else ""   # 확인: 굵은 노랑
_C_B = "\033[1m" if _TTY else ""
_C_0 = "\033[0m" if _TTY else ""


def _enable_vt():
    """Windows 콘솔에서 ANSI 이스케이프가 먹히도록 VT 모드를 켠다."""
    if os.name != "nt" or not _TTY:
        return
    try:
        import ctypes
        h = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        ctypes.windll.kernel32.GetConsoleMode(h, ctypes.byref(mode))
        ctypes.windll.kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


class CliIO:
    def __init__(self):
        self.secrets = set()  # ask_fields의 secret 값 — 로그에서 가리기 위함

    @staticmethod
    def _fallback():
        return "(비대화형 실행: 사용자 응답 불가)"

    def say(self, text):
        t = str(text)
        print(t.split("\n", 1)[0] if t.startswith("· ") else t)  # 스크립트 코드는 웹 UI에서만 접어 보인다

    def ask(self, question, options=None):
        print(f"\a\n{_C_Q}{'─' * 46}\n❓ {_C_B}{question}{_C_0}\n{_C_Q}{'─' * 46}{_C_0}")
        for i, o in enumerate(options or [], 1):
            print(f"  {i}) {o}")
        if options:
            print("  (번호 선택, 또는 직접 입력)")
        if not sys.stdin.isatty():
            return self._fallback()
        try:
            a = input(f"{_C_Q}답변> {_C_0}")
        except (EOFError, KeyboardInterrupt):
            return self._fallback()
        if options and a.strip().isdigit() and 1 <= int(a) <= len(options):
            return str(options[int(a) - 1])
        return a

    def ask_fields(self, question, fields):
        """항목별 입력. secret=True 필드는 에코 없이 받는다."""
        print(f"\a\n{_C_Q}{'─' * 46}\n❓ {_C_B}{question}{_C_0}\n{_C_Q}{'─' * 46}{_C_0}")
        if not sys.stdin.isatty():
            return self._fallback()
        answers = []
        try:
            for f in fields:
                name = f.get("name", "?") if isinstance(f, dict) else str(f)
                secret = isinstance(f, dict) and f.get("secret")
                if secret:
                    import getpass
                    v = getpass.getpass(f"{_C_Q}{name} (비공개)> {_C_0}")
                    if v:
                        self.secrets.add(v)
                else:
                    v = input(f"{_C_Q}{name}> {_C_0}")
                answers.append(f"{name}: {v}")
        except (EOFError, KeyboardInterrupt):
            return self._fallback()
        return "\n".join(answers)

    def confirm(self, question):
        print(f"\a\n{_C_W}{'━' * 46}\n⚠️  {_C_B}{question}{_C_0}\n{_C_W}{'━' * 46}{_C_0}")
        if not sys.stdin.isatty():
            return "n"
        try:
            return input(f"{_C_W}확인> {_C_0}")
        except (EOFError, KeyboardInterrupt):
            return "n"


class FakeBrowser:
    """--selftest용: playwright 없이 동작."""

    def observe(self):
        return 'URL: about:blank\nTITLE: test\n[0] <a> "test"'

    def act(self, name, args):
        return "ok"

    def close(self):
        pass


def selftest():
    for t in agent.TOOLS:
        assert set(t) >= {"name", "description", "input_schema"}, t
        assert t["input_schema"]["type"] == "object", t
        assert "properties" in t["input_schema"], t

    calls = {"n": 0}

    def fake_chat(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            content = [{"type": "tool_use", "id": "t1", "name": "click", "input": {"index": 0}}]
        else:
            # 직전 user 메시지가 tool_result(t1) + 새 관찰 텍스트를 함께 담는지 검증
            last = messages[-1]
            assert last["role"] == "user" and isinstance(last["content"], list)
            tr = next(b for b in last["content"] if b.get("type") == "tool_result")
            assert tr["tool_use_id"] == "t1" and tr["content"] == "ok"
            assert any(b.get("type") == "text" and b["text"].startswith("현재 페이지:") for b in last["content"])
            content = [{"type": "tool_use", "id": "t2", "name": "done", "input": {"summary": "ok"}}]
        return {"stop_reason": "tool_use", "content": content}

    import tempfile
    from memory import Memory

    tmp = tempfile.TemporaryDirectory()
    mem = Memory(Path(tmp.name) / "sites.json")  # 실제 _local/memory를 건드리지 않는다
    real_chat = llm.chat
    llm.chat = fake_chat
    try:
        result = agent.run("테스트 작업", FakeBrowser(), CliIO(), mem=mem)
        assert result == "ok", result
        assert calls["n"] == 2, calls

        # 사이트 메모리: 첫 요청의 remember(+op) → 다음 요청(새 대화)에 이름으로 한 줄이 붙고 page로 홈에 간다
        seen = {}

        def remember_chat(messages, tools):
            if not seen:
                seen["first"] = True
                return {"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "r1", "name": "done", "input": {
                        "summary": "ok", "op": "카페 찾기", "remember": [{
                            "site": "예시카페", "url": "https://cafe.naver.com/examplecafe",
                            "notes": {"본문": "iframe cafe_main 안"}}]}}]}
            seen["texts"] = [b["text"] for b in messages[-1]["content"] if b.get("type") == "text"]
            return {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "r3", "name": "done", "input": {"summary": "ok"}}]}

        llm.chat = remember_chat
        agent.run("예시카페 카페 찾아줘", FakeBrowser(), CliIO(), mem=mem)
        agent.run("예시카페 새글 알려줘", FakeBrowser(), CliIO(), mem=mem)
        recalled = next(t for t in seen["texts"] if t.startswith(agent.MEMORY_HEADER))
        assert "■ 예시카페" in recalled and "cafe_main" in recalled, recalled
        assert mem.resolve("예시카페")[3] == "https://cafe.naver.com/examplecafe"
    finally:
        llm.chat = real_chat
        tmp.cleanup()
    print("selftest ok")


def run_task(task, browser, io, messages, use_tab=True, tab_url=None):
    try:
        summary = agent.run(task, browser, io, messages, use_tab=use_tab, tab_url=tab_url)
        for s in getattr(io, "secrets", ()):
            if s:
                summary = summary.replace(s, "****")
        io.say(f"[result] {summary}")
    except Exception as e:
        io.say(f"[error] {e}")


EXIT_WORDS = {"종료", "exit", "quit", "q", "끝"}


def _is_exit(text):
    return text.strip().lower().lstrip("/") in EXIT_WORDS


def chat_loop(browser, io, first_message=None):
    """브라우저 위에서 돌아가는 대화 루프. 프로그램의 기본 인터페이스."""
    io.say("준비됐습니다. 대화를 시작하세요 (종료: exit, 현재 탭 없이 백그라운드에서: /bg 작업)")
    messages = []
    pending = first_message
    while True:
        if pending is not None:
            line, pending = pending, None
        else:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
        if not line:
            continue
        if _is_exit(line):
            break
        use_tab = True
        if line.lower().startswith("/bg "):  # 챗 UI에서 탭 칩을 x로 닫은 것과 같다
            line, use_tab = line[4:].strip(), False
        try:
            run_task(line, browser, io, messages, use_tab=use_tab)
        except KeyboardInterrupt:
            io.say("[중단] 이어서 대화할 수 있습니다")


def _task_loop(browser, io, alive, idle):
    """UI에서 들어온 작업을 차례로 실행한다. alive()가 False가 되면 끝난다."""
    messages = []
    last_task = [time.monotonic()]
    tab_info = getattr(browser, "tab_info", None)
    last = [0.0]

    def refresh_tab(force=False):
        # 챗 창 탭 칩: 사용자가 보는 탭을 주기적으로 알린다(playwright 스레드인 여기서만 조회)
        if tab_info and (force or time.monotonic() - last[0] >= TAB_REFRESH_SEC):
            last[0] = time.monotonic()
            try:
                io.tab = tab_info()
            except Exception:
                pass

    refresh_tab(force=True)
    prepare = getattr(browser, "prepare_background", None)
    # 첫 사용 지연을 줄이도록 쿠키 없이 headless 브라우저를 미리 띄운다.
    warm_at = time.monotonic() + 1.5
    while alive():
        if io.new_session.is_set():  # 새 채팅: 대화 기록을 비운다
            io.new_session.clear()
            messages.clear()
            unpin = getattr(browser, "unpin", None)
            if unpin:
                unpin()  # 새 채팅은 다시 지금 보는 탭에서 시작한다
            refresh_tab(force=True)
        if io.tasks.empty():
            if prepare and time.monotonic() >= warm_at:
                prepare, _p = None, prepare
                try:
                    _p()
                except Exception:
                    pass
            if time.monotonic() - last_task[0] >= BG_IDLE_SEC:
                close_background = getattr(browser, "close_background", None)
                if close_background:
                    try:
                        close_background()
                    except Exception:
                        pass
                last_task[0] = time.monotonic()
            refresh_tab()
            idle()
            continue
        item = io.tasks.get()
        # WebIO는 (작업, use_tab, tab_url, epoch), 인자로 받은 첫 메시지는 문자열
        task, use_tab, tab_url, epoch = (tuple(item) + (None,) * 4)[:4] if isinstance(item, tuple) \
            else (item, True, None, None)
        if epoch is not None and epoch != getattr(io, "epoch", epoch):
            continue  # 새 채팅 전에 들어온 이전 대화의 작업은 버린다
        if io.new_session.is_set():  # 새 채팅 직후의 첫 작업: 대화 기록부터 비운다
            io.new_session.clear()
            messages.clear()
            unpin = getattr(browser, "unpin", None)
            if unpin:
                unpin()
        io.stop_requested.clear()
        io.task = task
        io.busy = True
        try:
            run_task(task, browser, io, messages, use_tab=use_tab, tab_url=tab_url or None)
        finally:
            io.busy = False
            last_task[0] = time.monotonic()
            refresh_tab(force=True)
            io._drop = False  # 새 채팅으로 중단된 이전 작업의 출력 버리기 끝


UI_BROWSERS = ("msedge", "chrome")
TAB_REFRESH_SEC = 1.5  # 챗 창 탭 칩 갱신 주기
BG_IDLE_SEC = 600  # 백그라운드 브라우저 유휴 종료 시간


def ui_browser():
    """AGENT_UI_BROWSER: 챗 UI 창 종류. 비우면 pywebview(없으면 브라우저 앱 창), webview면 pywebview만,
    msedge/chrome이면 pywebview가 있어도 그 브라우저의 --app 창으로 띄운다(에이전트 브라우저와 별개)."""
    v = (os.environ.get("AGENT_UI_BROWSER") or "").strip().lower()
    v = {"edge": "msedge", "google-chrome": "chrome"}.get(v, v)
    if v and v != "webview" and v not in UI_BROWSERS:
        raise ValueError(f"AGENT_UI_BROWSER={v!r}: webview, msedge, chrome 중 하나여야 합니다")
    return v


def run_ui(first_message=None):
    webview = None
    if ui_browser() in ("", "webview"):
        try:
            import webview
        except ImportError:
            if ui_browser() == "webview":
                raise SystemExit("[error] AGENT_UI_BROWSER=webview인데 pywebview가 설치되지 않았습니다")
    if webview is None:
        browser = Browser()
        try:
            browser.launch(ask_tab=False)
            _run_ui_app(browser, first_message)
        finally:
            browser.close()
        return
    _run_ui_window(webview, first_message)


def config_rows(browser=None):
    """설정 화면에 보여줄 현재 설정값. 비밀값은 llm.config()가 가린다."""
    import memory

    rows = llm.config()
    rows.append({"name": "브라우저", "value": browser.mode if browser else "시작 중",
                 "source": "환경변수 AGENT_CDP_PORT/AGENT_CHANNEL/AGENT_PROFILE" if any(
                     os.environ.get(k) for k in ("AGENT_CDP_PORT", "AGENT_CHANNEL", "AGENT_PROFILE"))
                 else "기본값"})
    ub = os.environ.get("AGENT_UI_BROWSER")
    rows.append({"name": "챗 UI 창", "value": getattr(browser, "ui_mode", None) or "pywebview",
                 "source": "환경변수 AGENT_UI_BROWSER" if ub else "기본값"})
    lv = os.environ.get("AGENT_LOG")
    rows.append({"name": "로그 레벨", "value": agent.log_level(),
                 "source": "환경변수 AGENT_LOG" if lv else "기본값"})
    mp = os.environ.get("AGENT_MEMORY")
    rows.append({"name": "사이트 메모리", "value": str(Path(mp) if mp else memory.DEFAULT_PATH),
                 "source": "환경변수 AGENT_MEMORY" if mp else "기본값"})
    return rows


def _run_ui_window(webview, first_message=None):
    """pywebview 네이티브 창에 챗 UI를 띄운다.

    pywebview는 메인 스레드를 차지하고, playwright sync API는 자기를 만든 스레드에서만 쓸 수 있으므로
    브라우저 실행·에이전트 루프는 작업 스레드에서 돌린다.
    UI와 파이썬은 pywebview 브리지(js_api)로만 통신한다 — HTTP 포트를 열지 않는다.
    """
    import threading
    import webui

    io = webui.WebIO()
    if first_message:
        io.tasks.put(first_message)  # 인자로 받은 첫 메시지는 창이 뜨면 바로 실행한다
    closed = threading.Event()
    state = {"browser": None}
    io.settings = lambda: config_rows(state["browser"])

    def worker():
        browser = Browser()
        state["browser"] = browser
        try:
            browser.launch(ask_tab=False)
            io.say("챗 UI 준비됨 — 작업을 입력하세요")
            _task_loop(browser, io, alive=lambda: not closed.is_set(),
                       idle=lambda: closed.wait(0.3))
        except Exception as e:
            io.unavailable = f"브라우저를 시작하지 못했습니다: {e}"  # 이후 입력은 버리지 않고 오류로 알린다
            io.say(f"[error] {e}")
        finally:
            try:
                browser.close()
            except Exception:
                pass

    window = _create_ui_window(webview, io)
    window.events.closed += lambda: closed.set()
    t = threading.Thread(target=worker, name="agent", daemon=True)
    t.start()
    try:
        # icon: Windows 창·작업표시줄(WinForms), macOS Dock(cocoa)에 쓰인다. 없으면 python 아이콘
        webview.start(localization=UI_LOCALE, icon=webui.icon_path())  # 창이 닫힐 때까지 막힌다
    finally:
        closed.set()
        io.stop()  # 대기 중인 질문·확인을 풀고 실행 중인 작업을 중단시킨다
        t.join(timeout=15)  # LLM 응답 대기 중이면 기다리지 않고 종료(데몬 스레드)


# 닫기 확인 대화상자 문구(pywebview 기본은 영어)
UI_LOCALE = {"global.quitConfirmation": "작업이 진행 중입니다. 창을 닫으면 작업이 중단됩니다. 닫을까요?"}


def _create_ui_window(webview, io):
    """브리지 모드 챗 창을 만들고 알림·닫기 확인을 연결한다. smoke 스크립트도 이 함수를 쓴다."""
    import webui

    if sys.platform == "win32":
        try:
            import ctypes
            # python.exe와 다른 앱으로 묶여야 작업표시줄에 python 아이콘 대신 창 아이콘이 나온다
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("browser-agent")
        except Exception:
            pass
    window = webview.create_window("browser-agent", html=webui.HTML_BRIDGE, js_api=webui.BridgeApi(io),
                                   width=480, height=900, min_size=(360, 480),
                                   text_select=True)  # 답변 복사 허용(pywebview 기본은 선택 막음)

    def closing():
        # 작업 중이거나 답을 기다릴 때만 닫기 확인을 띄운다(pywebview가 이 핸들러 직후 confirm_close를 본다)
        window.confirm_close = bool(io.busy or io.pending_kind)

    window.events.closing += closing
    io.on_attention = lambda kind: _attention(window, kind)
    return window


def _attention(window, kind):
    """질문·확인 대기 시작(kind)이면 창을 복원하고 작업표시줄을 깜빡인다. None이면 깜빡임을 멈춘다."""
    if kind:
        try:
            window.restore()  # 최소화돼 있으면 되살린다(보통 상태면 변화 없음)
        except Exception:
            pass
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]

        hwnd = int(window.native.Handle.ToInt64()) if window.native is not None else 0
        if not hwnd:
            return
        # FLASHW_ALL(3)|FLASHW_TIMERNOFG(12): 사용자가 창으로 돌아올 때까지 깜빡임. FLASHW_STOP=0
        flags = 3 | 12 if kind else 0
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, flags, 0, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def _run_ui_app(browser, first_message=None):
    """pywebview가 없을 때: 같은 브라우저를 --app 모드 창으로 하나 더 띄운다."""
    import webui

    io = webui.WebIO()
    io.settings = lambda: config_rows(browser)
    url = webui.start_server(io)
    if first_message:
        io.tasks.put(first_message)
    print(f"챗 UI: {url}")
    ui_ctx = None
    try:
        # 별도 앱 창(주소창·탭 없음): 같은 브라우저를 임시 프로필 + --app 모드로 하나 더 띄운다
        import tempfile
        # AGENT_UI_BROWSER → AGENT_CHANNEL → 나머지 순. 에이전트 브라우저(CDP attach 포함)와 무관하게 고른다
        pref = [ui_browser(), os.environ.get("AGENT_CHANNEL", "msedge"), *UI_BROWSERS]
        channels = list(dict.fromkeys(c for c in pref if c and c != "webview"))
        err = None
        for ch in channels:
            try:
                ui_ctx = browser._pw.chromium.launch_persistent_context(
                    tempfile.mkdtemp(prefix="agent-ui-"),
                    channel=ch, headless=False, no_viewport=True,
                    color_scheme="null",  # OS 다크모드를 따른다(playwright 기본은 light 고정)
                    args=[f"--app={url}", "--window-size=480,900", "--no-first-run",
                          "--no-default-browser-check"])
                browser.ui_mode = f"{ch} --app 창"
                break
            except Exception as e:
                err = e
        if ui_ctx is None:
            raise err
        ui = next((p for p in ui_ctx.pages if p.url.startswith(url.split("?")[0])), None)
        for p in ui_ctx.pages:
            if p is not ui:
                p.close()
        if ui is None:
            ui = ui_ctx.new_page()
            ui.goto(url)
    except Exception:
        if ui_ctx is not None:
            ui_ctx.close()
            ui_ctx = None
        ui = browser.ctx.new_page()
        ui.goto(url)
        browser.ui_page = ui
        browser.ui_mode = "에이전트 브라우저의 탭"
    io.say("챗 UI 준비됨 — 작업을 입력하세요")
    try:
        # wait_for_timeout이 playwright 이벤트를 돌려 창 닫힘을 감지한다
        _task_loop(browser, io, alive=lambda: not ui.is_closed(),
                   idle=lambda: ui.wait_for_timeout(300))
    except Exception:
        pass  # UI 창을 닫으면 wait_for_timeout이 예외 → 종료
    finally:
        if ui_ctx is not None:
            try:
                ui_ctx.close()
            except Exception:
                pass


def main():
    # Windows cp949 콘솔에서 한글 출력 깨짐 방지
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _enable_vt()
    args = sys.argv[1:]
    for flag in ("--debug", "--quiet"):
        if flag in args:
            os.environ["AGENT_LOG"] = flag[2:]
    try:
        agent.log_level()  # 잘못된 AGENT_LOG·AGENT_UI_BROWSER는 브라우저를 띄우기 전에 알린다
        ui_browser()
    except ValueError as e:
        sys.exit(f"[error] {e}")
    if "--selftest" in args:
        selftest()
        return

    first = " ".join(a for a in args if not a.startswith("--")).strip() or None
    # --ui는 항상 챗 UI, --cli는 항상 터미널. 둘 다 없으면 stdin이 파이프(시나리오 입력 등)일 때만 터미널
    piped = sys.stdin is not None and not sys.stdin.isatty()
    if "--ui" in args or ("--cli" not in args and not piped):
        run_ui(first)
        return
    browser = Browser()
    try:
        browser.launch()
        chat_loop(browser, CliIO(), first)
    finally:
        browser.close()


if __name__ == "__main__":
    main()
