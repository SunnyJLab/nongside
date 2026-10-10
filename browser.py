import builtins
import contextlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse

DOM_JS = Path(__file__).with_name("dom.js").read_text(encoding="utf-8")

CDP_PORT = int(os.environ.get("AGENT_CDP_PORT", "9222"))

# 챗 UI 탭 칩용 [제목, 아이콘 URL]
# 탭 선택용: [보이는지, 실제 포커스]. playwright는 붙은 탭마다 포커스 에뮬레이션을 켜서
# 모든 탭이 hasFocus()=true가 되므로 잠깐 끄고 읽는다(_real_focus)
FOCUS_JS = "() => [document.visibilityState === 'visible', document.hasFocus()]"

TAB_INFO_JS = """() => {
  const l = document.querySelector('link[rel~="icon"]');
  const icon = l ? l.href : (location.protocol.startsWith('http') ? location.origin + '/favicon.ico' : '');
  return [document.title, icon];
}"""


def background_enabled():
    """AGENT_BACKGROUND=0이면 예전처럼 사용자 브라우저에서 바로 이동한다(백그라운드 창을 막는 환경용)."""
    return (os.environ.get("AGENT_BACKGROUND") or "1").strip().lower() not in ("0", "false", "no", "off")


# 백그라운드 브라우저 창 위치. 일부 환경에서 --headless가 막히는 경우 일반 창을 화면 밖에 띄운다
# (-32000은 Windows가 최소화 창에 쓰는 좌표라 피한다)
BG_POS = (-20000, -20000)
BG_ARGS = [
    f"--window-position={BG_POS[0]},{BG_POS[1]}", "--window-size=1280,900",
    "--no-first-run", "--no-default-browser-check",
    # 화면 밖 창은 가려진 창으로 판정돼 렌더링·타이머가 멈출 수 있다 — 끈다
    "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
    # --disable-features는 playwright 기본 목록(HttpsUpgrades 등)을 덮어쓰므로 넘기지 않는다
    "--disable-background-timer-throttling",
]

# 백그라운드 창의 window.open 팝업은 같은 숨긴 창의 탭으로 연다. 크기 지정 팝업은 새 창으로 뜨면서
# 크롬이 화면 안으로 끌어오고, 숨김(page 이벤트)은 sync API가 다음 playwright 호출 때에야 돌려
# LLM 대기 내내 보인다. noopener만 남겨도 팝업으로 판정되므로(HTML 표준) popup=0을 붙인다
_TAB_POPUP_JS = """(() => {
  const open = window.open;
  window.open = function (url, name, features) {
    const keep = String(features || '').split(',').map(s => s.trim()).filter(s => /^no(opener|referrer)$/i.test(s));
    return open.call(this, url, name, keep.concat('popup=0').join(','));
  };
})();"""

# 사이트 이름 후보: 지금 제목, 홈(/) 제목, 메타 사이트 이름. 홈은 fetch로 제목만 읽는다(이동 없음 — EUC-KR 페이지는
# 선언한 charset으로 다시 읽는다. DOMParser는 TrustedHTML 페이지(Gmail)에서 막혀 정규식으로 뽑는다)
_SITE_NAME_JS = r"""async () => {
  const m = n => ((document.querySelector('meta[property="' + n + '"],meta[name="' + n + '"]') || {}).content || '').trim();
  let home = '';
  try {
    const r = await fetch(location.origin + '/', {credentials: 'include', signal: AbortSignal.timeout(3000)});
    const buf = await r.arrayBuffer();
    const cs = ((r.headers.get('content-type') || '').match(/charset=([\w-]+)/i) || [])[1];
    let t = new TextDecoder(cs || 'utf-8').decode(buf);
    const mc = (t.match(/<meta[^>]+charset=["']?([\w-]+)/i) || [])[1];
    if (!cs && mc && !/^utf-?8$/i.test(mc)) t = new TextDecoder(mc).decode(buf);
    home = ((t.match(/<title[^>]*>([^<]*)<\/title>/i) || [])[1] || '').trim();
  } catch (e) {}
  return [document.title, home, m('og:site_name') || m('application-name') || m('apple-mobile-web-app-title')];
}"""


class _Stopped(BaseException):
    """run_code 중 사용자 중지. BaseException이라 코드 안의 except Exception에 삼켜지지 않는다."""


class _Denied(BaseException):
    """run_code 중 사용자가 위험 클릭 승인을 취소함."""


# run_code 클릭 직전에 대상 요소의 보이는 이름을 읽는다 — CSS 셀렉터(a.btn_register)만으로는
# "등록" 버튼인지 코드에서 알 수 없어서다. 안쪽 span을 눌러도 감싼 버튼의 이름을 본다
_LABEL_JS = """e => {
  const t = e.closest('button,a,[role=button],input,label,summary') || e;
  return [t.innerText, t.value, t.getAttribute('aria-label'), t.title, t.alt]
    .filter(x => typeof x === 'string' && x.trim()).join(' ').replace(/\\s+/g, ' ').slice(0, 200);
}"""


class _StopTime:
    """run_code의 time 대역: sleep만 중지 신호를 보며 자고 나머지는 time 모듈 그대로."""

    def __init__(self, sleep):
        self._sleep = sleep

    def sleep(self, seconds):
        if self._sleep(float(seconds)):
            raise _Stopped()

    def __getattr__(self, name):
        return getattr(time, name)


class _WinHide:
    """Windows 전용(ctypes): 백그라운드 브라우저 창을 화면 밖·작업표시줄 밖에 두고,
    새 창이 가져간 포커스를 원래 창으로 돌려준다. 다른 OS에서는 아무것도 하지 않는다."""

    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x80, 0x40000
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x4, 0x10, 0x20
    SW_HIDE, SW_SHOWNOACTIVATE = 0, 4

    def __init__(self):
        self.ok = False
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes as w
            u = ctypes.WinDLL("user32", use_last_error=True)
            self._ct, self._w, self._u = ctypes, w, u
            u.GetForegroundWindow.restype = w.HWND
            u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
            u.GetWindowThreadProcessId.restype = w.DWORD
            u.IsWindowVisible.argtypes = [w.HWND]
            u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
            u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
            u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            u.SetForegroundWindow.argtypes = [w.HWND]
            u.BringWindowToTop.argtypes = [w.HWND]
            u.IsWindow.argtypes = [w.HWND]
            u.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
            long_ptr = ctypes.c_ssize_t
            get = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
            set_ = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
            get.argtypes, get.restype = [w.HWND, ctypes.c_int], long_ptr
            set_.argtypes, set_.restype = [w.HWND, ctypes.c_int, long_ptr], long_ptr
            self._get, self._set = get, set_
            self._enum_t = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
            u.EnumWindows.argtypes = [self._enum_t, w.LPARAM]
            self._tid = ctypes.WinDLL("kernel32").GetCurrentThreadId
            self.ok = True
        except Exception:
            self.ok = False

    def foreground(self):
        return self._u.GetForegroundWindow() if self.ok else None

    def pid_of(self, hwnd):
        """창을 소유한 프로세스 id. 실패하면 None."""
        if not self.ok or not hwnd:
            return None
        owner = self._w.DWORD()
        self._u.GetWindowThreadProcessId(hwnd, self._ct.byref(owner))
        return owner.value or None

    def _windows(self, pid):
        """pid 프로세스의 보이는 크롬 최상위 창들."""
        found = []
        buf = self._ct.create_unicode_buffer(64)

        def cb(hwnd, _):
            owner = self._w.DWORD()
            self._u.GetWindowThreadProcessId(hwnd, self._ct.byref(owner))
            if owner.value == pid and self._u.IsWindowVisible(hwnd):
                self._u.GetClassNameW(hwnd, buf, 64)
                if buf.value.startswith("Chrome_WidgetWin"):
                    found.append(hwnd)
            return True

        self._u.EnumWindows(self._enum_t(cb), 0)
        return found

    def hide(self, pid, prev=None, wait=1.0):
        """pid의 창을 작업표시줄에서 빼고 화면 밖으로 옮긴 뒤 prev 창에 포커스를 돌려준다.
        창은 페이지를 만든 직후 비동기로 뜰 수 있어 새 창이 보일 때까지 최대 wait초 다시 찾는다.
        새로 숨긴 창 수를 반환."""
        if not self.ok or not pid:
            return 0
        u, n, start = self._u, 0, time.monotonic()
        # 이미 숨긴 창만 있으면 새 탭이 기존 창에 열린 것이다 — 새 창이 뜰 수 있는 짧은 시간만 기다린다
        grace = wait if not self._windows(pid) else min(wait, 0.25)
        while True:
            wins = self._windows(pid)
            for hwnd in wins:
                ex = self._get(hwnd, self.GWL_EXSTYLE)
                want = (ex | self.WS_EX_TOOLWINDOW) & ~self.WS_EX_APPWINDOW
                if ex != want:
                    self._set(hwnd, self.GWL_EXSTYLE, want)
                    # 작업표시줄은 창을 다시 보일 때 스타일을 반영한다(화면 밖이라 깜빡임이 안 보인다)
                    u.ShowWindow(hwnd, self.SW_HIDE)
                    u.ShowWindow(hwnd, self.SW_SHOWNOACTIVATE)
                    n += 1
                u.SetWindowPos(hwnd, None, BG_POS[0], BG_POS[1], 0, 0,
                               self.SWP_NOSIZE | self.SWP_NOZORDER | self.SWP_NOACTIVATE
                               | self.SWP_FRAMECHANGED)
            if n or time.monotonic() - start >= grace:
                break
            time.sleep(0.05)
        # 포커스가 아직 백그라운드 창에 있을 때만 되돌린다 — 그사이 사용자가 고른 다른 창은 뺏지 않는다
        cur = u.GetForegroundWindow()
        if not cur or self.pid_of(cur) == pid:
            self.restore(prev)
        return n

    def restore(self, prev):
        """prev 창이 포커스를 잃었으면 되돌린다. 다른 프로세스 창은 SetForegroundWindow만으로는
        거부될 수 있어 지금 포커스 창의 입력 스레드에 잠깐 붙어서 요청한다."""
        if not self.ok or not prev or not self._u.IsWindow(prev):
            return
        u = self._u
        cur = u.GetForegroundWindow()
        if cur == prev:
            return
        me = self._tid()
        fg = u.GetWindowThreadProcessId(cur, None) if cur else 0
        attached = bool(fg and fg != me and u.AttachThreadInput(me, fg, True))
        try:
            u.BringWindowToTop(prev)
            u.SetForegroundWindow(prev)
        finally:
            if attached:
                u.AttachThreadInput(me, fg, False)


# macOS: JXA(osascript)의 ObjC 브리지로 NSRunningApplication을 부른다. 손쉬운 사용 권한이 필요 없다.
# 창이 비동기로 뜨거나 새 창이 앱 숨김을 풀 수 있어 잠깐 동안 여러 번 반복한다.
_MAC_HIDE_JS = r"""
ObjC.import('AppKit');
function app(pid) {
  if (!pid) return null;
  const a = $.NSRunningApplication.runningApplicationWithProcessIdentifier(pid);
  return (a && !a.isNil()) ? a : null;
}
function run(argv) {
  const pid = +argv[0], prev = +argv[1];
  for (let i = 0; i < 8; i++) {
    const a = app(pid);
    if (!a) { delay(0.15); continue; }  // 앱 등록 전일 수 있다
    if (!a.hidden) a.hide();
    const p = app(prev);
    if (p && !p.active) p.activateWithOptions(2);  // NSApplicationActivateIgnoringOtherApps
    delay(0.15);
  }
}
"""
_MAC_FRONT_JS = "ObjC.import('AppKit'); $.NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier"


class _MacHide:
    """macOS 전용: 화면 밖 좌표는 macOS가 화면 안으로 되돌리므로 백그라운드 브라우저 앱을 숨기고(⌘H와 같음)
    창이 뜨기 전 맨 앞에 있던 앱을 다시 활성화한다. 숨김은 osascript를 백그라운드로 띄워 기다리지 않는다."""

    def __init__(self):
        import shutil
        self.ok = sys.platform == "darwin" and bool(shutil.which("osascript"))

    def foreground(self):
        """맨 앞 앱의 pid. 실패하면 None."""
        if not self.ok:
            return None
        import subprocess
        try:
            out = subprocess.run(["osascript", "-l", "JavaScript", "-e", _MAC_FRONT_JS],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
            return int(out) if out.isdigit() else None
        except Exception:
            return None

    def hide(self, pid, prev=None, wait=None):
        if not self.ok or not pid:
            return 0
        import subprocess
        try:
            subprocess.Popen(["osascript", "-l", "JavaScript", "-e", _MAC_HIDE_JS, str(pid), str(prev or 0)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 1
        except Exception:
            return 0


def _window_hider():
    return _MacHide() if sys.platform == "darwin" else _WinHide()

# extract 결과 캡. aria_snapshot은 요소마다 role/들여쓰기 줄이 붙어 순수 텍스트보다
# 같은 내용에 약 1.5~2배 길이가 필요하므로, 텍스트 방식 4000자와 비슷한 분량이 남도록 올렸다.
# 이전 턴의 결과는 agent._stub_observations가 앞부분만 남기고 자르므로 늘어난 분량은 현재 턴에서만 쓰인다.
# 넘치는 부분은 잘림 표시와 함께 extract(offset=...)로 이어 읽는다.
EXTRACT_CAP = 6000
# extract에서 본문 iframe으로 보고 앞에 둘 최소 면적 비율(최상위 뷰포트 대비)
MAJOR_FRAME = 0.25


def _page_slice(text, offset=None):
    """text의 [offset, offset+EXTRACT_CAP) 조각. 뒤가 남으면 이어 읽는 법을 붙인다."""
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0
    end = start + EXTRACT_CAP
    chunk = text[start:end]
    if start and not chunk:
        return f"(offset {start}: 본문 끝을 넘었다 - 전체 {len(text)}자)"
    if end < len(text):
        chunk += (f"\n…(잘림: 전체 {len(text)}자 중 {start}~{end}자. "
                  f"이어서 읽으려면 extract(offset={end}))")
    return chunk


# navigate 후 고정 sleep 대신: DOM 변경이 quiet ms 동안 없을 때까지, 최대 cap ms 대기.
_SETTLE_JS = """
([quiet, cap]) => new Promise((resolve) => {
  const t0 = performance.now();
  let last = t0;
  const mo = new MutationObserver(() => { last = performance.now(); });
  mo.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  const tick = () => {
    const now = performance.now();
    if (now - last >= quiet || now - t0 >= cap) { mo.disconnect(); resolve(); }
    else setTimeout(tick, 50);
  };
  setTimeout(tick, 50);
})
"""

# run_code 실행 상한(초). sys.settrace로 검사하므로 OS·스레드와 무관하게 걸린다.
RUN_CODE_CAP = 60

# extract: 값이 든 비밀번호/OTP 칸이 있는 프레임인지
_SENSITIVE_JS = ("() => [...document.querySelectorAll("
                 "'input[type=password], input[autocomplete=one-time-code]')].some(e => e.value)")
# run_code 출력 마스킹용: 비밀번호/OTP 칸에 든 값 목록
_SECRET_VALUES_JS = ("() => [...document.querySelectorAll("
                     "'input[type=password], input[autocomplete=one-time-code]')].map(e => e.value).filter(v => v)")
# click 폴백 판정: 요소 중심이 overflow로 잘리는 조상(가로 슬라이더·스크롤 목록) 밖에 있는지
_CLIPPED_JS = """e => {
  const r = e.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
  for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) {
    const cs = getComputedStyle(p);
    if (/(hidden|clip|auto|scroll)/.test(cs.overflowX + cs.overflowY)) {
      const q = p.getBoundingClientRect();
      if (x < q.left || x > q.right || y < q.top || y > q.bottom) return true;
    }
  }
  return false;
}"""

# run_code에 허용하는 builtins. open, __import__, eval 등을 뺐지만 샌드박스가 아니다 —
# json.__builtins__ 같은 경로로 쉽게 우회되며, 코드는 에이전트 프로세스 권한으로 임의
# Python을 실행한다. builtins 축소는 LLM의 실수를 줄이는 용도일 뿐 보안 경계가 아니다.
SAFE_BUILTINS = {n: getattr(builtins, n) for n in (
    "print len range str int float bool list dict set tuple enumerate zip map filter "
    "min max sum sorted reversed repr isinstance abs round any all getattr hasattr".split())}

# extract용: 보이는 텍스트만 수집, nav/header/footer/숨은 요소 제외
# 태그명뿐 아니라 role/id/class 휴리스틱으로 GNB(div 기반 메뉴)도 거른다.
# 과필터로 본문까지 날아가면 휴리스틱 없이 다시 수집(안전장치).
_EXTRACT_JS = """
() => {
  const root = document.querySelector('main') ||
               document.querySelector('article') || document.body;
  if (!root) return '';
  const BAD = new Set(['NAV','HEADER','FOOTER','SCRIPT','STYLE',
                       'TEMPLATE','NOSCRIPT','ASIDE']);
  const NAV_ROLE = new Set(['navigation','banner','contentinfo','menubar','menu']);
  const NAV_RE = /(^|[^a-z])(gnb|lnb|snb|nav|navi|navbar|header|footer|breadcrumb|skip|sitemap|quickmenu)([^a-z]|$)/i;
  const navLike = (n) => {
    const role = n.getAttribute('role');
    if (role && NAV_ROLE.has(role)) return true;
    const idc = n.id + ' ' + n.className;
    if (typeof idc === 'string' && NAV_RE.test(idc)) return true;
    // 스킵링크: #앵커 + "바로가기" 텍스트
    if (n.tagName === 'A' && (n.getAttribute('href') || '').startsWith('#') &&
        /바로\uAC00\uAE30/.test(n.textContent)) return true;
    return false;
  };
  // 한 번만 순회하며 strict(nav 휴리스틱 제외)와 loose(전체)를 함께 모은다.
  // getComputedStyle/getClientRects 비용이 순회 한 번분만 든다.
  const strict = [], loose = [];
  const walk = (el, inNav) => {
    for (const n of el.childNodes) {
      if (loose.length > 2000 && strict.length > 2000) return;
      if (n.nodeType === 3) {
        const t = n.textContent.trim();
        if (!t) continue;
        if (loose.length <= 2000) loose.push(t);
        if (!inNav && strict.length <= 2000) strict.push(t);
      } else if (n.nodeType === 1) {
        if (BAD.has(n.tagName)) continue;
        if (!n.getClientRects().length) continue;  // 렌더 안 됨(display:none 등)
        const s = getComputedStyle(n);
        if (s.visibility === 'hidden' || s.opacity === '0') continue;
        walk(n, inNav || navLike(n));
      }
    }
  };
  walk(root, false);
  const strictText = strict.join('\\n');
  const looseText = loose.join('\\n');
  // 휴리스틱: 과필터 안전장치 — 필터 결과가 원본의 30% 미만이거나 200자 미만이면 원래 방식
  if (strictText.length < 200 || strictText.length < looseText.length * 0.3) {
    return looseText;
  }
  return strictText;
}
"""


class Browser:
    def __init__(self):
        self._pw = None
        self._browser = None  # attach 모드일 때만 세팅
        self.ctx = None
        self.page = None
        self._tabs = []
        self._observed = None
        self._frame_map = []  # 전역 index -> (frame, 프레임 내 index)
        self._lines = []  # 직전 observe의 '[i] <tag> "label"' 줄들 (find용)
        self._tiny = set()  # 직전 observe에서 숨은/1px로 판정된 프레임
        self._titles = {}  # page -> (url, title) 배경 탭 제목 캐시
        self.ui_page = None
        self.mode = "시작 전"  # 설정 화면 표시용: attach/새로 실행 여부
        # 백그라운드 브라우저(화면 밖 창): 사용자 탭에 없는 내용을 찾을 때 처음 필요할 때 띄우고 재사용한다
        self._bg = None
        self.bg_ctx = None
        self._bg_pid = None  # 백그라운드 브라우저 프로세스 id (창 숨기기용)
        self._bg_fg = None  # 백그라운드 창이 뜨기 직전의 포커스 창 — 새 창이 뺏어가면 여기로 돌려준다
        self._win = _window_hider()  # Windows: 화면 밖·작업표시줄 밖 / macOS: 앱 숨김
        self.user_page = None  # 사용자가 보고 있는 탭 (턴 시작마다 focus_user_tab이 갱신)
        # 대화에 고정한 사용자 탭. 첫 요청에서 정하고 새 채팅(unpin) 전까지 사용자가 탭을 바꿔도 유지한다
        self.pinned = None
        # 대화를 탭 없이 시작했는지. 첫 요청에서 탭을 빼면 새 채팅(unpin) 전까지 계속 백그라운드에서 작업한다
        self.detached = False
        self.pin_lost = False  # 고정한 탭이 닫혔는지(다음 요청에서 안내)
        self._user_channel = None  # attach한 브라우저 종류 — 백그라운드도 같은 브라우저로 띄운다
        self.bg_channel = None
        # 중지 신호(threading.Event). agent.run이 io.stop_requested를 넣는다 — wait·run_code가 중간에 멈춘다
        self.stop = None
        # run_code 클릭 직전 검사(label -> 허용 여부). agent가 넣는다 — 위험 버튼이면 사용자에게 묻는다
        self.guard = None

    def launch(self, ask_tab=True):
        """ask_tab=False(챗 UI): attach 시 터미널에서 탭을 묻지 않고 사용자가 보고 있는 탭을 고른다."""
        from playwright.sync_api import sync_playwright  # playwright import는 여기서만

        self._pw = sync_playwright().start()
        try:
            # 디버그 포트로 떠 있는 브라우저가 있으면 attach
            urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1)
            self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            self.ctx = self._browser.contexts[0]
            self._user_channel = self._cdp_channel()
            self.page = self._pick_tab() if ask_tab else self._first_tab()
            self.mode = f"CDP attach (127.0.0.1:{CDP_PORT}) — 종료 시 브라우저는 그대로 둠"
        except Exception:
            if self._browser is not None:
                try:
                    self._browser.close()
                except Exception:
                    pass
                self._browser = None
            channel = os.environ.get("AGENT_CHANNEL", "msedge")
            profile = os.environ.get("AGENT_PROFILE", str(Path.cwd() / ".edge-profile"))
            self.ctx = self._pw.chromium.launch_persistent_context(
                profile, channel=channel, headless=False,
                color_scheme="null",  # playwright 기본은 light 에뮬레이션 — OS 다크모드를 따르게 한다
                args=["--no-first-run", "--no-default-browser-check"],
            )
            self.page = self.ctx.pages[-1] if self.ctx.pages else self.ctx.new_page()
            self.mode = f"새로 실행 ({channel}, 프로필 {profile})"
        self.ctx.on("page", self._adopt_popup)
        self.user_page = self.page
        if not ask_tab:
            self.focus_user_tab()  # 활성(보이는) 탭을 기본 선택 — 챗 창 칩에 그대로 뜬다
        return self

    def is_background(self, page=None):
        """page(기본: 세션 탭)가 사용자에게 안 보이는 백그라운드 브라우저의 탭인지."""
        page = page or self.page
        try:
            return self.bg_ctx is not None and page is not None and page.context is self.bg_ctx
        except Exception:
            return False

    def _user_pages(self):
        return [p for p in self.ctx.pages if not p.is_closed() and p is not self.ui_page]

    def _bg_pages(self):
        return [p for p in (self.bg_ctx.pages if self.bg_ctx else []) if not p.is_closed()]

    def focus_user_tab(self, prefer_url=None):
        """턴 시작 시 세션 탭을 사용자가 지금 보고 있는 탭으로 되돌린다.
        지난 턴이 백그라운드에서 끝났어도 "이 탭 요약해줘"는 사용자 탭을 봐야 한다.
        백그라운드 탭은 TABS 목록에 남아 있어 모델이 switch_tab으로 돌아갈 수 있다."""
        try:
            pages = self._user_pages()
        except Exception:
            return
        if not pages:
            return
        if self.pinned is not None:
            if self.pinned in pages:  # 대화 중에는 고정한 탭으로 돌아간다 — 맥락이 섞이지 않게
                self.user_page = self.page = self.pinned
                return
            self.pinned = None  # 고정한 탭이 닫혔다 — 지금 보는 탭을 다시 고른다
            self.pin_lost = True  # 다음 요청에서 안내한다(유휴 칩 갱신이 먼저 지워도 남도록)
        if prefer_url:  # 사용자가 챗 창 칩에서 확인한 탭이 아직 열려 있으면 그 탭을 쓴다
            hit = [p for p in pages if p.url == prefer_url]
            if hit:
                self.user_page = self.page = self.user_page if self.user_page in hit else hit[-1]
                return

        # 창이 여러 개면 각 창의 활성 탭이 모두 visible이다. 그래서 OS 포커스로 고른다:
        # 지금 포커스된 탭 > 마지막으로 포커스를 받았던 보이는 탭 > 기존 탭 > 마지막 보이는 탭.
        # 챗 창을 누르면 어떤 탭도 포커스가 없으므로 마지막 포커스 탭(_last_focus)을 기억해 둔다.
        # 보이는지는 에뮬레이션과 무관하게 바로 읽는다. 포커스 판정(에뮬레이션 끄고 켜기 — 페이지에
        # focus/blur 이벤트가 생긴다)은 보이는 탭이 여러 개(창 여러 개)일 때만 한다
        vis = [p for p in pages if self._visible(p)]
        focused = [p for p in vis if (self._real_focus(p) or [0, 0])[1]] if len(vis) > 1 else []
        if focused:
            self._last_focus = focused[-1]
        last = getattr(self, "_last_focus", None)
        if focused:
            cur = focused[-1]
        elif last in vis:
            cur = last
        elif self.user_page in vis:
            cur = self.user_page
        else:
            # 챗 UI가 에이전트 브라우저의 탭이면 다른 탭은 모두 hidden이다 — 그때는 기존 탭 유지
            cur = vis[-1] if vis else (self.user_page if self.user_page in pages else pages[-1])
        self.user_page = self.page = cur

    def pin_user_tab(self):
        """아직 고정한 탭이 없으면 지금 사용자 탭을 이 대화의 탭으로 고정한다."""
        if (self.pinned is None and not self.detached and self.user_page is not None
                and not self.is_background(self.user_page)):
            self.pinned = self.user_page

    def unpin(self):
        """새 채팅: 탭 고정·탭 없이 작업을 풀어 다음 요청에서 사용자가 보는 탭을 다시 고른다."""
        self.pinned = None
        self.detached = False
        self.pin_lost = False

    def _real_focus(self, page):
        """[보이는지, 실제 OS 포커스]. 포커스 에뮬레이션을 잠깐 끄고 읽은 뒤 다시 켠다
        (에이전트 조작은 에뮬레이션이 켜져 있어야 포커스 이벤트가 정상이다). 실패하면 None."""
        cs = None
        try:
            cs = self.ctx.new_cdp_session(page)
            cs.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
            return page.evaluate(FOCUS_JS)
        except Exception:
            return None
        finally:
            if cs is not None:
                try:
                    cs.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
                    cs.detach()
                except Exception:
                    pass

    @staticmethod
    def _visible(page):
        try:
            return bool(page.evaluate("() => document.visibilityState === 'visible'"))
        except Exception:
            return False

    def _cdp_channel(self):
        """attach한 브라우저 종류(msedge/chrome). 모르면 None."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1) as r:
                name = json.loads(r.read().decode("utf-8", "replace")).get("Browser", "")
        except Exception:
            return None
        return "msedge" if name.startswith("Edg") else "chrome" if name.startswith("Chrome") else None

    def _bg_channels(self):
        """백그라운드 브라우저 후보: attach한 브라우저와 같은 종류 > AGENT_CHANNEL > 나머지.
        실행에 실패한 채널은 기억해 두고 다시 시도하지 않는다."""
        order = [getattr(self, "_user_channel", None), os.environ.get("AGENT_CHANNEL", "msedge"), "msedge", "chrome"]
        failed = getattr(self, "_bg_failed", set())
        seen = []
        for c in order:
            if c and c not in seen and c not in failed:
                seen.append(c)
        return seen

    def _bg_page(self, fresh):
        """백그라운드 탭. 백그라운드 브라우저가 죽었으면(프로세스 종료·사용자가 닫음) 한 번 새로 띄운다.
        sync API는 다음 호출 때에야 연결 끊김을 알기 때문에 is_connected()만으로는 부족하다."""
        try:
            return self._bg_page_once(fresh)
        except Exception:
            self._close_bg()
        try:
            return self._bg_page_once(fresh)
        except Exception:
            self._close_bg()
            return None

    def _bg_page_once(self, fresh):
        """백그라운드 탭. fresh면 새 탭, 아니면 열려 있는 마지막 백그라운드 탭을 재사용한다.
        화면 밖 창으로 띄운 브라우저를 대화 내내 재사용한다(창이 뜰 때만 포커스를 되돌린다).
        실행에 실패하면 None — 호출한 쪽이 사용자 브라우저로 폴백한다."""
        prev = None  # 새 창이 포커스를 가져가면 돌려줄 창(macOS는 앱 pid). 새 창을 띄울 때만 읽는다
        try:  # 백그라운드 브라우저가 죽었거나(프로세스 종료·사용자가 닫음) 연결이 끊겼으면 새로 띄운다
            if self._bg is not None and not self._bg.is_connected():
                self._close_bg()
        except Exception:
            self._close_bg()
        if self.bg_ctx is None:
            prev = self._win.foreground()
            src = self.user_page if self.user_page and not self.user_page.is_closed() else None
            ua = lang = None
            try:  # 사용자 브라우저와 같은 UA·언어로 맞춘다
                ua, lang = src.evaluate("[navigator.userAgent, navigator.language]")
                ua = ua.replace("HeadlessChrome", "Chrome")
            except Exception:
                pass
            for channel in self._bg_channels():
                try:
                    # 일부 환경에서 --headless가 막히는 경우 일반 창을 화면 밖에 띄운다
                    self._bg = self._pw.chromium.launch(channel=channel, headless=False, args=BG_ARGS)
                    self.bg_ctx = self._bg.new_context(user_agent=ua, locale=lang, color_scheme="null")
                    self.bg_channel = channel
                    break
                except Exception:
                    self._close_bg()
                    if not hasattr(self, "_bg_failed"):
                        self._bg_failed = set()
                    self._bg_failed.add(channel)
            if self.bg_ctx is None:
                return None
            self._bg_pid = self._browser_pid(self._bg)
            if self._bg_pid is None and self._win.ok:
                print("[경고] 백그라운드 브라우저 프로세스를 찾지 못해 창을 숨기지 못할 수 있습니다", file=sys.stderr)
            self.bg_ctx.on("page", self._adopt_popup)
            self.bg_ctx.add_init_script(_TAB_POPUP_JS)
            # new_page는 새 창으로 뜬다 — page 이벤트에서 숨기고 포커스를 돌려준다
            self.bg_ctx.on("page", lambda _p: self._hide_bg())
        # 사용자 브라우저의 로그인 쿠키를 매번 동기화한다 (사용자가 방금 로그인했을 수 있다)
        try:
            cookies = self.ctx.cookies()
        except Exception:
            cookies = []
        try:
            self.bg_ctx.add_cookies(cookies)
        except Exception:
            for c in cookies:  # 형식이 안 맞는 쿠키 하나 때문에 전부 버리지 않는다
                try:
                    self.bg_ctx.add_cookies([c])
                except Exception:
                    pass
        alive = self._bg_pages()
        if alive and not fresh:
            return alive[-1]
        self._bg_fg = prev if prev is not None else self._win.foreground()
        return self.bg_ctx.new_page()  # 비영속 컨텍스트의 새 탭은 새 창으로 뜬다(page 이벤트가 숨김)

    @staticmethod
    def _browser_pid(browser):
        """CDP SystemInfo로 브라우저 프로세스 id를 읽는다. 실패하면 None."""
        try:
            cs = browser.new_browser_cdp_session()
            try:
                info = cs.send("SystemInfo.getProcessInfo")
            finally:
                cs.detach()
            return next((p["id"] for p in info.get("processInfo", []) if p.get("type") == "browser"), None)
        except Exception:
            return None

    def _hide_bg(self):
        """백그라운드 창을 화면 밖·작업표시줄 밖으로 보내고 포커스를 창이 뜨기 전 창으로 돌린다."""
        try:
            self._win.hide(self._bg_pid, self._bg_fg)
        except Exception:
            pass

    def site_names(self, url):
        """url 사이트의 이름 후보 (지금 제목, 홈(/) 제목, 메타 사이트 이름). 지금 탭이 그 호스트가 아니면 빈 값."""
        try:
            page = self._active()
            if urlparse(page.url).netloc != urlparse(url).netloc:
                return "", "", ""
            title, home, meta = page.evaluate(_SITE_NAME_JS)
            return str(title or ""), html.unescape(re.sub(r"\s+", " ", str(home or ""))).strip(), str(meta or "")
        except Exception:
            return "", "", ""

    def tab_info(self):
        """챗 UI 칩에 보여줄 사용자 탭 {title, url, icon}. 백그라운드 브라우저는 띄우지 않는다. 탭이 없으면 None.
        탭 없이 시작한 대화면 {off: True} — 세션 탭(백그라운드)을 사용자 탭으로 되돌리지 않는다."""
        if self.detached:
            return {"off": True, "title": "", "url": "", "icon": "", "pinned": False}
        self.focus_user_tab()
        p = self.user_page
        try:
            if p is None or p.is_closed() or self.is_background(p):
                return None
        except Exception:
            return None
        try:
            title, icon = p.evaluate(TAB_INFO_JS)
        except Exception:
            title, icon = "", ""
        icon = str(icon or "")
        return {"title": str(title or "")[:200], "url": p.url, "pinned": p is self.pinned,
                "icon": icon if len(icon) <= 4000 else ""}  # 큰 data: 아이콘은 버린다(poll 크기)

    def use_background(self, first=False):
        """사용자가 챗 창에서 현재 탭을 뺐을 때: 세션 탭을 백그라운드 탭으로 바꾼다. 쓸 수 없으면 False.
        대화의 첫 요청(first)이면 새 채팅 전까지 탭 없이 작업하도록 기억한다."""
        if not background_enabled():
            return False
        self.focus_user_tab()  # 쿠키·UA를 복사할 사용자 탭을 정한다
        bg = self._bg_page(fresh=False)
        if bg is None:
            return False
        self.page = bg
        if first and self.pinned is None:
            self.detached = True
        return True

    def prepare_background(self):
        """챗 UI를 띄울 때 백그라운드 창을 미리 띄워 둔다. 대화 중에 창이 뜨며 포커스를 뺏지 않게 한다."""
        if background_enabled() and self.bg_ctx is None:
            self._bg_page(fresh=False)

    def _close_bg(self):
        for obj in (self.bg_ctx, self._bg):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._bg = self.bg_ctx = self._bg_pid = None

    def _first_tab(self):
        """탭을 묻지 않을 때의 시작 탭. 탭이 없으면 _pick_tab처럼 새 창을 연다."""
        pages = self.ctx.pages
        return pages[-1] if pages else self._pick_tab()

    def _pick_tab(self):
        """attach한 브라우저의 탭 목록을 보여주고 작업할 탭을 고른다."""
        pages = self.ctx.pages
        if not pages:
            # 창 없이 떠 있는 브라우저(macOS에서 창만 닫은 Chrome)에 탭을 만들면
            # 화면에 안 보이니 새 창을 명시적으로 연다
            session = self._browser.new_browser_cdp_session()
            with self.ctx.expect_page() as new:
                session.send("Target.createTarget",
                             {"url": "about:blank", "newWindow": True})
            session.detach()
            return new.value
        if len(pages) == 1:
            return pages[0]
        for i, p in enumerate(pages):
            try:
                print(f"  [{i}] {p.title()[:50]}  {p.url[:70]}")
            except Exception:
                print(f"  [{i}] (제목 읽기 실패)")
        pick = input("어느 탭에서 작업할까요? (번호, Enter=새 탭) ").strip()
        if pick.isdigit() and int(pick) < len(pages):
            return pages[int(pick)]
        return self.ctx.new_page()

    def _adopt_popup(self, page):
        # 세션 고정 탭이 연 팝업만 따라간다. 사용자가 직접 연 탭은 건드리지 않음
        try:
            if page.opener() == self.page:
                self.page = page
        except Exception:
            pass

    def _active(self):
        if self.page.is_closed():
            alive = [p for p in (self._tabs or self.ctx.pages + self._bg_pages())
                     if not p.is_closed() and p is not self.ui_page]
            if not alive:
                raise RuntimeError("no tabs left")
            self.page = alive[-1]
        return self.page

    def current_url(self) -> str:
        """현재 탭의 실제 URL. 한 턴 안에서 navigate가 먼저 돌면 관찰 시점 URL과 다르다."""
        return self._active().url

    def observe(self) -> str:
        try:
            page = self._active()
            # 프레임마다 개별 evaluate하므로 cross-origin/중첩 iframe도 수집된다.
            # 각 프레임의 window.__agentEls에 로컬 index로 저장된다.
            self._frame_map = []
            self._tiny = set()
            frame_url = {}
            els = []
            n_file = 0
            modal = ""
            # 모든 프레임의 후보에서 화면에 가까운 300개를 고른다(출력은 프레임·문서 순서). 프레임 순서로 채우면
            # 바깥 페이지(GNB·긴 게시판 메뉴)가 자리를 다 써서 본문 iframe(네이버 카페 글 목록)이 앞 몇 개만 남았다
            frames, cands, top_vh = page.frames, [], None
            for fi, fr in enumerate(frames):
                view = {}
                # about:blank 프레임(광고 자리·빈 틀)은 위치를 묻지 않는다 — 프레임마다 왕복 2번이라 관찰이 느려진다
                if fr is not page.main_frame and top_vh and fr.url not in ("", "about:blank"):
                    try:  # 프레임의 최상위 화면 위치 — iframe 안 요소의 화면 거리를 실제 위치로 잰다
                        box = fr.frame_element().bounding_box()
                    except Exception:
                        box = None
                    if box:
                        view = {"offY": box["y"], "vh": top_vh}
                try:
                    local = fr.evaluate(DOM_JS, view)
                    frame_url[fi] = fr.url
                except Exception:
                    continue  # detach됐거나 evaluate 불가한 프레임
                if fr is page.main_frame:
                    top_vh = local.get("vh")
                if local.get("tiny"):
                    self._tiny.add(fr)  # extract도 이 프레임을 건너뛴다
                    continue
                n_file += local.get("nFile", 0)
                if local.get("modal") and not modal:
                    modal = local["modal"]
                for k, e in enumerate(local.get("els") or []):
                    e["frame"] = fi
                    cands.append((e.get("d", 0), fi, k, e))
            for _, fi, _, e in sorted(sorted(cands, key=lambda c: c[:3])[:300], key=lambda c: c[1:3]):
                self._frame_map.append((frames[fi], e["i"], e.get("tag") or ""))
                e["i"] = len(els)
                els.append(e)
            self._observed = page
            self._tabs = self._user_pages() + self._bg_pages()
            tabs = []
            title = ""
            for i, p in enumerate(self._tabs):
                try:
                    mark = "*" if p == page else " "
                    where = " (백그라운드)" if self.is_background(p) else ""
                    t = self._title(p, fresh=p is page)
                    if p is page:
                        title = t
                    tabs.append(f"{mark}[t{i}]{where} {t[:40]} {p.url[:60]}")
                except Exception:
                    pass
            if page not in self._tabs:
                title = self._title(page, fresh=True)
            where = ("백그라운드 브라우저 (사용자에게 안 보임)" if self.is_background(page)
                     else "사용자 탭 (사용자가 보고 있는 화면)")
            head = (
                "TABS:\n" + "\n".join(tabs)
                + f"\nWHERE: {where}\nURL: {page.url}\nTITLE: {title}\n"
            )
            used = sorted({e["frame"] for e in els if e.get("frame")})
            if used:
                head += "IFRAMES:\n" + "".join(
                    f"  f{fi}: {frame_url.get(fi, '')[:70]}\n" for fi in used
                )
            if n_file:
                head += f"FILE INPUTS: {n_file}개 - attach(path)로 첨부 가능\n"
            if modal:
                head += (f"MODAL: 열린 레이어({modal!r}) 안의 요소만 표시한다 - 뒤 화면은 가려져 "
                         "클릭할 수 없다. 끝나면 적용/확인/닫기로 레이어를 닫아라\n")
        except Exception as e:
            return f"(observe error: {e})"
        if not els:
            self._lines = []
            return head + "(인터랙티브 요소 없음)"
        # <input:password>처럼 눈에 띄는 type만 표기 (text 등은 생략)
        def fmt(e):
            t = e.get("type") or ""
            t = f":{t}" if t and t != "text" else ""
            hint = f' +{e["hint"]}' if e.get("hint") else ""  # A1
            opts = f' {e["opts"]}' if e.get("opts") else ""   # A2
            f = f' @f{e["frame"]}' if e.get("frame") else ""
            return f'[{e["i"]}] <{e["tag"]}{t}> "{e["text"]}"{hint}{opts}{f}'
        self._lines = [fmt(e) for e in els]
        return head + "\n".join(self._lines)

    def _title(self, p, fresh=False):
        """탭 제목. title()은 CDP 왕복이라 배경 탭은 URL이 같으면 캐시를 쓴다
        (url은 playwright가 로컬로 들고 있어 왕복이 없다). 현재 탭은 매번 새로 읽는다."""
        hit = self._titles.get(p)
        if not fresh and hit and hit[0] == p.url:
            return hit[1]
        t = p.title()
        self._titles[p] = (p.url, t)
        return t

    def _element(self, index):
        """관찰 시점 인덱스의 요소 핸들. 페이지가 바뀌었거나 요소가 DOM에서 떨어졌으면
        재관찰 후 같은 번호로 재시도하지 않고 에러를 낸다 — 새 목록의 같은 번호는 다른
        요소일 수 있는데 agent의 위험 동작 확인은 모델이 본 이전 목록의 라벨로 했기 때문이다.
        에러가 나면 agent가 턴 끝에 재관찰해 새 목록을 모델에게 준다."""
        try:
            i = int(index)
        except (TypeError, ValueError):
            return None, None, f"error: invalid index {index!r}"
        page = self._active()
        if self._observed is not None and page is not self._observed:
            return None, None, "error: 관찰 이후 페이지가 바뀌었다 - 새 관찰 목록을 보고 다시 골라라"
        if not (0 <= i < len(self._frame_map)):
            return None, None, f"error: no element at index {i}, run observe again"
        fr, local_i, tag = self._frame_map[i]
        try:
            # 핸들 조회와 isConnected 확인을 한 번의 왕복으로 묶는다
            el = fr.evaluate_handle(
                "i => { const e = (window.__agentEls || [])[i]; return e && e.isConnected ? e : null; }",
                local_i).as_element()
        except Exception:
            el = None
        if el is None:
            return None, None, (f"error: [{i}] 요소가 관찰 이후 사라졌거나 바뀌었다 - "
                                "새 관찰 목록을 보고 다시 골라라")
        return el, tag, None

    def act(self, name, args) -> str:
        try:
            if name == "navigate":
                page = self._active()
                if args.get("visible"):
                    if self.is_background(page):  # 사용자 화면에 띄우라는 요청: 사용자 탭에서 연다
                        up = self.user_page
                        page = self.page = up if up and not up.is_closed() else self.ctx.new_page()
                        page.bring_to_front()
                elif not self.is_background(page) and background_enabled():
                    # 사용자 탭은 그대로 두고 백그라운드 탭에서 연다
                    bg = self._bg_page(fresh=False)
                    if bg is not None:
                        page = self.page = bg
                page.goto(args["url"], wait_until="domcontentloaded")
                self._settle(page)
                where = " in background (사용자에게 안 보임)" if self.is_background(page) else ""
                return f"navigated{where}: {page.url}"

            if name == "new_tab":
                bg = None
                if not args.get("visible") and background_enabled():
                    bg = self._bg_page(fresh=True)
                self.page = bg or self.ctx.new_page()
                if bg is None:
                    self.page.bring_to_front()
                self.page.goto(args["url"], wait_until="domcontentloaded")
                self._settle(self.page)
                where = " in background (사용자에게 안 보임)" if bg is not None else ""
                return f"new tab{where}: {self.page.url}"

            if name == "switch_tab":
                i = int(args["index"])
                tabs = [p for p in (self._tabs or self._user_pages()) if p is not self.ui_page]
                if not (0 <= i < len(tabs)) or tabs[i].is_closed():
                    return f"error: no tab {i}"
                self.page = tabs[i]
                if self.is_background():
                    return f"switched to tab {i} (백그라운드)"
                self.page.bring_to_front()
                self.user_page = self.page
                return f"switched to tab {i}"

            if name == "run_code":
                return self.run_code(args.get("code", ""))

            if name == "click":
                el, _, err = self._element(args.get("index"))
                if err:
                    return err
                before = self.page
                try:
                    el.click(timeout=2500)  # 가려진 요소에 8초씩 묶이지 않게 빨리 실패
                except Exception:
                    try:  # 화면 밖일 수 있으니 스크롤 후 한 번만 재시도
                        el.scroll_into_view_if_needed(timeout=1500)
                        el.click(timeout=2500)
                    except Exception:
                        # 가로 슬라이더(overflow:hidden) 안에서 잘려 보이지 않는 항목은 좌표 클릭이
                        # 불가능하다. 잘린 경우에만 DOM click으로 누른다 — 다른 요소에 가려진 경우는
                        # 사용자도 못 누르는 상태라 그대로 실패로 돌려준다.
                        try:
                            clipped = el.evaluate(_CLIPPED_JS)
                        except Exception:
                            clipped = False
                        if not clipped:
                            return ("error: 클릭 실패 - 요소가 가려졌거나 화면 밖일 수 "
                                    "있다. scroll(index)나 find로 다른 요소를 시도해라")
                        el.evaluate("e => e.click()")
                try:
                    cur = self._active()
                    if cur is not before:  # 팝업 채택됨: 실제 URL 뜰 때까지 대기
                        cur.wait_for_url(lambda u: u not in ("about:blank", ""), timeout=3000)
                    cur.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                return f"clicked [{args.get('index')}]"

            if name == "type":
                el, tag, err = self._element(args.get("index"))
                if err:
                    return err
                text = str(args.get("text", ""))
                if tag == "select":
                    try:
                        el.select_option(label=text)
                    except Exception:
                        el.select_option(value=text)
                else:
                    page = self._active()
                    # 에디터 대응: iframe이면 안쪽 body, 래퍼면 안쪽 입력 요소로 바꾼다
                    if tag == "iframe":
                        try:
                            el = el.content_frame().locator("body").element_handle(timeout=2000)
                        except Exception:
                            pass
                    el = el.evaluate_handle(
                        "e => e.matches('input,textarea,select,[contenteditable]') || e.isContentEditable ? e"
                        " : (e.querySelector('[contenteditable=true],[contenteditable=\"\"],textarea,"
                        "input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=button])"
                        ":not([type=submit]):not([type=reset]):not([type=file]):not([type=image])') || e)"
                    ).as_element() or el
                    done_msg = None
                    if el.evaluate("e => e.isContentEditable || e.ownerDocument.designMode === 'on'"):
                        # contenteditable/designMode 에디터: 클릭 없이 포커스 → 기존 내용 전체 선택 → insertText
                        # (fill처럼 내용을 바꾼다. 선택 영역을 덮어쓰므로 에디터의 undo 기록도 남는다)
                        # (가림막·placeholder에 click이 30초 묶이던 문제, 한글도 한 번에 들어간다)
                        ok = el.evaluate("""(e, t) => {
                            const d = e.ownerDocument; e.focus();
                            const r = d.createRange(); r.selectNodeContents(e);
                            const s = d.getSelection(); s.removeAllRanges(); s.addRange(r);
                            return d.execCommand('insertText', false, t);
                        }""", text)
                        if not ok:
                            page.keyboard.insert_text(text)
                        done_msg = f"typed into [{args.get('index')}]"
                    elif not el.evaluate("e => e.matches('input,textarea,select')"):
                        # 가짜 입력면(에디터 placeholder 문단): 클릭으로 에디터가 숨은 캡처 요소에
                        # 포커스를 옮기게 한 뒤 키 입력한다(SmartEditor ONE 실측)
                        el.click(timeout=3000)
                        page.keyboard.type(text)
                        done_msg = f"typed into [{args.get('index')}]"
                    if done_msg:
                        if args.get("submit"):
                            page.keyboard.press("Enter")
                            try:
                                page.wait_for_load_state("domcontentloaded", timeout=3000)
                            except Exception:
                                pass
                            self._settle(self._active())
                            return done_msg + " and pressed Enter"
                        return done_msg
                    # A3: focus 먼저(보정 2), 실패 시에만 click
                    try:
                        el.focus()
                        focused = el.evaluate("e => e === document.activeElement")
                    except Exception:
                        focused = False
                    if not focused:
                        try:
                            el.click(timeout=2500)
                        except Exception:
                            try:
                                el.click(timeout=1500, force=True)  # 가림막 무시
                            except Exception:
                                pass
                    # 앞부분은 fill로 한 번에 넣고 마지막 글자만 실제 키 입력한다 —
                    # 자동완성은 keydown/keyup으로 뜨므로 마지막 한 글자면 충분하다.
                    # fill이 안 되는 요소(커스텀 위젯)는 전부 키 입력으로 폴백.
                    try:
                        el.fill(text[:-1])
                        head_ok = True
                    except Exception:
                        head_ok = False
                    rest = text[-1:] if head_ok else text
                    delay = 0 if len(rest) > 200 else 10
                    try:
                        el.type(rest, delay=delay)
                    except Exception:
                        page.keyboard.type(rest, delay=delay)
                    # playwright는 키보드 배열에 없는 글자(한글 등)를 insertText로 넣어
                    # keydown/keyup이 안 생긴다 — 키 이벤트에만 반응하는 자동완성이 안 뜬다.
                    # 실제 키(Space→Backspace)는 maxlength가 찬 칸에서 마지막 글자를 지운다 —
                    # 값은 건드리지 않고 JS로 키/input 이벤트만 보낸다.
                    if any(ord(c) > 127 for c in rest):
                        try:
                            el.evaluate("""(e, k) => {
                                const a = e.ownerDocument.activeElement;
                                const t = a && a !== e.ownerDocument.body ? a : e;
                                const o = {key: k, bubbles: true, cancelable: true};
                                t.dispatchEvent(new KeyboardEvent('keydown', o));
                                t.dispatchEvent(new KeyboardEvent('keyup', o));
                                t.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: k}));
                            }""", rest[-1])
                        except Exception:
                            pass
                    if args.get("submit"):
                        page.keyboard.press("Enter")
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=3000)
                        except Exception:
                            pass
                        self._settle(self._active())
                        return f"typed into [{args.get('index')}] and pressed Enter"
                    if el.get_attribute("type") != "password":  # 비밀번호엔 자동완성이 없다
                        page.wait_for_timeout(350)  # 네트워크 자동완성 후보 대기
                return f"typed into [{args.get('index')}]"

            if name == "scroll":
                if args.get("index") is not None:
                    el, _, err = self._element(args.get("index"))
                    if err:
                        return err
                    el.scroll_into_view_if_needed()
                    return f"scrolled to [{args.get('index')}]"
                dy = -600 if args.get("direction") == "up" else 600
                self._active().evaluate(f"window.scrollBy(0, {dy})")
                return f"scrolled {args.get('direction', 'down')}"

            if name == "back":
                page = self._active()
                try:
                    page.go_back(wait_until="domcontentloaded")
                except Exception:
                    return "error: 뒤로 갈 페이지 없음"
                return f"went back: {page.url}"

            if name == "wait":
                text = args.get("text")
                if text:
                    # 페이지 안에서 100ms 폴링 (최대 10초). 파이썬 쪽 0.5초 폴링보다 왕복·지연이 적다.
                    try:
                        self._active().wait_for_function(
                            "t => !!document.body && document.body.innerText.includes(t)",
                            arg=text, polling=100, timeout=10000)
                        return f"waited for text: {text}"
                    except Exception:
                        return f"error: text 안 나타남: {text}"
                n = min(float(args.get("seconds", 2)), 10)
                if self._sleep(n):
                    return "error: 사용자가 중단함"
                return f"waited {n}s"

            if name == "find":
                norm = lambda s: " ".join(str(s).split()).lower()
                q = norm(args["text"])
                hits = [l for l in self._lines if q in norm(l)][:20]
                if hits:
                    return "\n".join(hits)
                for fr in self._active().frames:
                    try:
                        if q in norm(fr.inner_text("body")):
                            return (f"본문에는 {args['text']!r} 텍스트가 있지만 클릭 가능한 "
                                    "요소가 아님 - scroll/extract/screenshot으로 확인하세요")
                    except Exception:
                        pass
                return f"no match: {args['text']} (페이지 본문에도 없음)"

            if name == "press":
                key = str(args["key"])
                self._active().keyboard.press(key)
                return f"pressed {key}"

            if name == "hover":
                el, _, err = self._element(args.get("index"))
                if err:
                    return err
                try:
                    el.hover(timeout=8000)
                except Exception:
                    try:
                        el.scroll_into_view_if_needed(timeout=3000)
                        el.hover(timeout=5000)
                    except Exception:
                        return ("error: hover 실패 - 요소가 가려졌거나 화면 밖일 수 "
                                "있다. scroll(index)나 find로 다른 요소를 시도해라")
                return f"hovered [{args.get('index')}]"

            if name == "attach":
                raw = str(args["path"])
                path = os.path.abspath(os.path.expanduser(raw))
                if not os.path.isfile(path):
                    # 다른 OS 형식 경로(C:\Users\..)를 추측한 경우: 파일명만 떼서 홈의 흔한 폴더에서 찾는다.
                    # 찾아도 바로 올리지 않는다 — 동명의 다른 파일이 외부로 업로드될 수 있어 사용자 확인을 거친다
                    base = PureWindowsPath(raw).name if "\\" in raw else Path(raw).name
                    found = [str(c) for d in ("Downloads", "Desktop", "Documents")
                             if (c := Path.home() / d / base).is_file()]
                    if found:
                        return (f"error: 파일 없음: {raw}. 같은 이름의 파일 후보: {', '.join(found)} - "
                                "이 파일이 맞는지 ask_user로 확인한 뒤 그 경로로 다시 attach해라")
                    return (f"error: 파일 없음: {raw} (홈: {Path.home()}) - "
                            "경로를 ask_user로 물어라")
                page = self._active()
                inputs = []
                for fr in page.frames:
                    try:
                        inputs += fr.query_selector_all("input[type=file]")
                    except Exception:
                        pass
                if not inputs:
                    return ("error: input[type=file] 없음 - "
                            "'파일첨부' 버튼을 먼저 누르고 다시 시도")
                try:
                    i = int(args.get("index", 0))
                except (TypeError, ValueError):
                    i = 0
                if not (0 <= i < len(inputs)):
                    return f"error: file input {i} 없음 (총 {len(inputs)}개)"
                inputs[i].set_input_files(path)
                return f"attached {os.path.basename(path)} (file input {i+1}/{len(inputs)})"

            if name == "extract":
                page = self._active()
                # 숨은/1px 프레임(광고·추적)은 건너뛴다 — 프레임마다 최대 3초 타임아웃이 걸린다.
                # fi는 observe의 @f 번호와 같은 page.frames 순번이다.
                frames = [(fi, fr) for fi, fr in enumerate(page.frames) if fr not in self._tiny]
                # 본문이 iframe에 있는 페이지(네이버 카페 cafe_main, 블로그 mainFrame 등)는
                # 최상위 프레임이 GNB/메뉴뿐인데 먼저 붙으면 EXTRACT_CAP을 다 차지해 본문이
                # offset 뒤로 밀린다. 화면을 크게 차지하는 본문 iframe을 앞에 둔다.
                rank = self._frame_ranks(page, [fr for _, fr in frames])
                frames.sort(key=lambda x: rank.get(x[1], (False, 0)), reverse=True)
                # 접근성 트리 우선: 요소 구조/역할까지 남아 목록·표 판독이 쉽다
                parts = [(fi, fr, self._aria_main(fr)) for fi, fr in frames]
                parts = [p for p in parts if p[2]]
                if sum(len(p[2]) for p in parts) < 200:
                    # 트리가 빈약한 페이지(캔버스/커스텀 위젯 등)는 기존 텍스트 수집으로 폴백.
                    # 화면에 보이는 텍스트만, nav/header/footer 제외 (본문 우선)
                    parts = []
                    for fi, fr in frames:
                        try:
                            t = fr.evaluate(_EXTRACT_JS)
                        except Exception:
                            continue
                        if t and t.strip():
                            parts.append((fi, fr, t))
                if len(parts) > 1:
                    # 프레임 경계를 표시해 모델이 본문/메뉴/광고를 구분하고 offset으로 건너뛰게 한다
                    text = "\n".join(
                        f"[frame {'top' if fr is page.main_frame else f'f{fi}'} "
                        f"{(fr.name or '')[:30]} {fr.url[:70]}]\n{t}"
                        for fi, fr, t in parts)
                else:
                    text = "\n".join(t for _, _, t in parts)
                return _page_slice(text, args.get("offset"))

            return f"error: unsupported action {name}"
        except Exception as e:
            return f"error: {name} failed: {e}"

    @staticmethod
    def _settle(page, quiet_ms=150, cap_ms=1500):
        """고정 sleep 대신 DOM 변경이 quiet_ms 동안 멈출 때까지(최대 cap_ms) 기다린다.
        정적 페이지는 거의 즉시, 늦게 그리는 SPA는 그릴 때까지 기다린다."""
        try:
            page.evaluate(_SETTLE_JS, [quiet_ms, cap_ms])
        except Exception:
            pass  # 대기 중 이동으로 컨텍스트가 사라지면 그냥 넘어간다

    @staticmethod
    def _frame_ranks(page, frames):
        """프레임별 정렬 키 (본문 iframe 여부, 직접 차지하는 면적 px²). 큰 값이 앞.
        본문 iframe: 최상위 뷰포트 면적의 MAJOR_FRAME 비율 이상을 덮는 iframe. 부모 기준으로 재면
        같은 크기 safeframe 안에 중첩된 광고(뽐뿌 criteo 160x600)가 부모의 100%라 본문으로 뽑힌다.
        면적만 비교하면 최상위 프레임이 좌우 빈 여백까지 자기 면적으로 가져가 본문 iframe을
        이긴다(1911px 뷰포트에 860px 폭 카페 본문 = 45% vs 여백 포함 나머지 55%).
        면적은 자기 영역(뷰포트와 교집합)에서 자식 iframe 영역을 뺀 값 — 같은 등급 안에서만 비교.
        측정 실패(detach, cross-process 지연 등)한 프레임은 (False, 0)."""
        try:
            vw, vh = page.evaluate("[innerWidth, innerHeight]")
        except Exception:
            return {}
        rect = {page.main_frame: (0, 0, vw, vh)}

        def box(fr):
            if fr in rect:
                return rect[fr]
            parent = fr.parent_frame
            r = None
            if parent is not None and box(parent) is not None:
                try:
                    b = fr.frame_element().bounding_box()  # 최상위 뷰포트 좌표
                except Exception:
                    b = None
                if b:
                    px0, py0, px1, py1 = rect[parent]  # 부모 보이는 영역으로 자른다
                    x0, y0 = max(b["x"], px0), max(b["y"], py0)
                    x1 = min(b["x"] + b["width"], px1)
                    y1 = min(b["y"] + b["height"], py1)
                    if x1 > x0 and y1 > y0:
                        r = (x0, y0, x1, y1)
            rect[fr] = r
            return r

        def size(r):
            return (r[2] - r[0]) * (r[3] - r[1]) if r else 0

        out = {}
        for fr in frames:
            mine = size(box(fr))
            own = mine
            for ch in fr.child_frames:
                own -= size(box(ch))
            major = (fr is not page.main_frame and mine > 0
                     and mine >= MAJOR_FRAME * vw * vh)
            out[fr] = (major, max(own, 0))
        return out

    @staticmethod
    def _aria_main(fr):
        """본문(main/[role=main]) 접근성 트리를 먼저, 없거나 빈약하면 body 전체.
        body에 걸면 GNB/헤더가 EXTRACT_CAP 앞부분을 차지한다."""
        # aria_snapshot은 입력 값을 그대로 노출하고 password 타입이 스냅샷에 드러나지 않아 마스킹할 수 없다 — 값 든 비밀번호/OTP 칸이 있는 프레임은 텍스트 노드만 모으는 _EXTRACT_JS로 수집
        try:
            if fr.evaluate(_SENSITIVE_JS):
                return (fr.evaluate(_EXTRACT_JS) or "").strip()
        except Exception:
            pass
        for sel in ("main, [role=main]", "body"):
            try:
                loc = fr.locator(sel)
                if sel != "body" and loc.count() != 1:
                    continue
                # 기본 타임아웃 30초라 body 없는/늦은 프레임마다 블록된다 — 즉시 실패에 가깝게 묶는다
                t = (loc.aria_snapshot(mode="default", timeout=3000) or "").strip()
            except Exception:
                continue
            if sel == "body" or len(t) >= 200:
                return t
        return ""

    def run_code(self, code) -> str:
        """LLM이 준 playwright Python 코드를 현재 탭에서 실행한다.
        네임스페이스: page(현재 탭) / ctx / browser(이 객체) / json / re / time.
        print() 출력과 코드가 남긴 result 변수를 이어붙여 돌려준다."""

        # 휴리스틱: 상한(60초)은 <run_code> 프레임의 줄 실행마다만 검사한다 — time.sleep 같은 C 블로킹 호출은 끊지 못하고 Playwright 호출은 실행 동안 기본 10초 타임아웃에 맡긴다. upgrade: 서브프로세스 격리.
        page = self._active()
        stop = self.stop

        def el(index):
            """관찰 목록 [index] 요소(ElementHandle). 로케이터를 새로 짜지 않고 본 요소를 그대로 쓴다."""
            h, _, e = self._element(index)
            if e:
                raise LookupError(e.removeprefix("error: "))
            return h

        ns = {"__builtins__": SAFE_BUILTINS, "page": page, "ctx": page.context,
              "browser": self, "json": json, "re": re, "time": _StopTime(self._sleep), "el": el}
        buf = io.StringIO()
        deadline = [time.monotonic() + RUN_CODE_CAP]  # 승인 창에서 기다린 시간만큼 늘린다
        expired = []

        def _local(frame, event, arg):
            if stop is not None and stop.is_set():
                raise _Stopped()
            if time.monotonic() > deadline[0]:
                expired.append(1)
                raise TimeoutError("run_code timeout")
            return _local

        def _global(frame, event, arg):
            # playwright 내부 프레임은 추적하지 않는다(성능)
            return _local if frame.f_code.co_filename == "<run_code>" else None

        patches = self._guard_clicks(deadline) if self.guard else []
        self._set_timeout(10000)
        prev = sys.gettrace()
        sys.settrace(_global)
        err = None
        try:
            with contextlib.redirect_stdout(buf):
                exec(compile(code, "<run_code>", "exec"), ns)
        except (_Stopped, _Denied) as e:
            err = e
        except Exception as e:
            err = e
        finally:
            sys.settrace(prev)
            for cls, meth, orig in patches:
                setattr(cls, meth, orig)
            self._set_timeout(30000)
        if isinstance(err, _Denied):
            return "error: 사용자가 취소함 — 이 클릭과 이후 코드는 실행하지 않았다"
        if isinstance(err, _Stopped) or (stop is not None and stop.is_set()):
            return "error: 사용자가 중단함"
        # 마스킹(fr.evaluate)은 trace 복원 뒤에 한다
        if err is not None:
            # agent가 "error:" 접두사로 실패를 판별하므로 partial stdout은 뒤에 붙인다
            if expired:
                partial = self._mask_secrets(buf.getvalue())[:EXTRACT_CAP].rstrip("\n")
                return "\n".join(filter(None, (f"error: run_code timeout ({RUN_CODE_CAP}초)",
                                                partial, "— 작업을 더 작게 나눠 다시 시도")))
            if isinstance(err, SyntaxError) and "await" in code:
                return "error: SyntaxError - run_code는 동기 API다. await/async를 빼고 다시 써라"
            # 에디터를 run_code로 뚫으려다 반복 실패하는 경우가 많아 대안을 알려준다
            return (self._mask_secrets(f"error: {type(err).__name__}: {err}")[:EXTRACT_CAP - 120]
                    + "\n(입력이 목적이면 관찰 목록 인덱스로 type을 써라 — 에디터·iframe도 된다)")
        out = buf.getvalue()
        if "result" in ns:
            out += repr(ns["result"])
        return self._mask_secrets(out)[:EXTRACT_CAP]

    def _guard_clicks(self, deadline):
        """run_code 실행 동안 playwright 클릭 메서드를 감싸 클릭 직전에 대상 요소 이름을 self.guard에 묻는다.
        돌려준 (클래스, 메서드, 원본) 목록으로 실행 뒤 복원한다."""
        from playwright.sync_api import ElementHandle, Frame, Locator, Page
        guard = self.guard

        def label_of(obj, selector):
            target = obj.locator(selector) if selector is not None else obj
            if isinstance(target, Locator):
                target = target.first  # 여러 개 걸린 로케이터도 이름은 판단할 수 있게
            # 요소가 없으면 여기서 기다리다 실패한다 — 어차피 클릭도 실패할 대상이다
            return target.evaluate(_LABEL_JS) or ""

        def wrap(cls, meth, has_sel, only_click=False):
            orig = getattr(cls, meth)

            def guarded(obj, *a, **kw):
                if only_click and (a[0] if a else kw.get("type")) != "click":
                    return orig(obj, *a, **kw)
                sel = (a[0] if a else kw.get("selector")) if has_sel else None
                label = label_of(obj, sel)
                t0 = time.monotonic()
                ok = guard(label)
                deadline[0] += time.monotonic() - t0
                if not ok:
                    raise _Denied()
                return orig(obj, *a, **kw)

            setattr(cls, meth, guarded)
            return cls, meth, orig

        patches = []
        for meth in ("click", "dblclick", "tap"):
            patches += [wrap(Locator, meth, False), wrap(ElementHandle, meth, False),
                        wrap(Page, meth, True), wrap(Frame, meth, True)]
        patches += [wrap(Locator, "dispatch_event", False, True),
                    wrap(ElementHandle, "dispatch_event", False, True)]
        return patches

    def _sleep(self, seconds):
        """중지 신호를 보며 잔다. 중지되면 True."""
        if self.stop is None:
            time.sleep(seconds)
            return False
        return self.stop.wait(seconds)

    def _mask_secrets(self, text):
        """run_code는 input_value() 등으로 비밀번호/OTP 값을 그대로 읽을 수 있다 — 출력에서 가린다."""
        try:
            frames = self._active().frames
        except Exception:
            return text
        for fr in frames:
            try:
                vals = fr.evaluate(_SECRET_VALUES_JS) or []
            except Exception:
                continue
            for v in sorted(vals, key=len, reverse=True):
                text = text.replace(v, "****")
        return text

    def _set_timeout(self, ms):
        for ctx in (self.ctx, self.bg_ctx):
            try:
                ctx.set_default_timeout(ms)
                # 탐색(goto/reload)은 느린 페이지를 고려해 30초 유지 — 조작 대기만 줄인다
                ctx.set_default_navigation_timeout(30000)
            except Exception:
                pass  # ctx 없음/닫힘 — run_code는 계속

    def screenshot(self):
        # jpeg로 눌러 토큰을 아낀다 (png는 3~5배 큼)
        return self._active().screenshot(type="jpeg", quality=60)

    def close(self):
        self._close_bg()
        try:
            # attach 모드(_browser 세팅)는 탭도 브라우저도 그대로 두고 연결만 끊는다.
            # browser.close()는 CDP Browser.close를 보내 브라우저 프로세스를
            # 통째로 종료시키고, page.close()도 마지막 탭이면 창을 닫아 종료시킨다.
            # 연결 해제는 finally의 _pw.stop()이 한다.
            if self._browser is None and self.ctx is not None:
                self.ctx.close()
        except Exception:
            pass
        finally:
            self._browser = None
            self.ctx = None
            self.page = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None
