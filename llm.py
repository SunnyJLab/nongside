import http.client
import base64
import functools
import json
import os
import platform
import socket
import sys
import time
from urllib.parse import urlparse
from pathlib import Path


# .env에서 읽어 채운 키 — 설정 화면이 출처를 process env와 구분해 보여준다
_ENV_FILE_KEYS = set()
DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"


def _load_env_file(path=None):
    """레포 루트 .env를 읽어 os.environ에 채운다. setdefault라 이미 있는 process env가 이긴다.
    값 양끝의 따옴표는 벗기고, '#' 주석·빈 줄·'=' 없는 줄은 무시. 파일이 없으면 조용히 넘어간다."""
    try:
        raw = (path or Path(__file__).with_name(".env")).read_bytes()
    except OSError:
        return
    try:
        # PowerShell 5.1의 '>' 리다이렉션은 UTF-16LE(BOM)로 저장한다
        text = raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig")
    except UnicodeDecodeError as e:
        print(f".env 디코딩 실패, 건너뜀: {e}", file=sys.stderr)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (p.strip() for p in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            if key not in os.environ:
                os.environ[key] = value
                _ENV_FILE_KEYS.add(key)


_load_env_file()

SYSTEM = """너는 브라우저를 조작하는 에이전트다. 매 턴 주어지는 요소 목록을 보고 도구로 다음 행동을 선택한다. 아래 규칙을 지켜라.
1. 한 턴에 도구를 여러 개 호출해도 된다. click/navigate처럼 페이지를 바꾸는 도구는 그 턴 마지막에 두거나 단독으로 호출해라 — 뒤 도구의 인덱스는 무효가 된다. 단 extract/wait는 인덱스를 쓰지 않으므로 페이지 변경 도구 뒤에 와도 된다. 검색/목록/가격 조회처럼 읽기가 목적이면 navigate(또는 click) 직후 extract를 같은 턴에 묶어라. 검색·목록 URL 패턴을 아는 사이트는 UI 조작 대신 그 URL로 바로 navigate해라(모르면 기존처럼 검색창을 써라).
2. 게시판/검색결과/상품목록 같은 목록 페이지에서는 먼저 extract로 목록 전체를 읽어라. 제목/날짜/작성자/조회수/가격처럼 목록 화면에 이미 보이는 정보는 개별 항목을 클릭하지 말고 목록에서 읽어 답해라. 목록에 없는 정보(본문 내용, 상세 스펙)가 필요할 때만 항목을 연다. 항목을 하나씩 다 열어보는 것은 금지다.
3. screenshot은 마지막 수단이다. 순서는 관찰 목록, find, extract, 그래도 안 되면 screenshot. 지도/차트/캔버스처럼 텍스트가 없는 화면에만 쓴다.
4. 관찰 목록의 +접힘 / +팝업 / +숨은메뉴 마커는 하위 항목이 가려져 있다는 뜻이다. +팝업 / +숨은메뉴 는 hover, +접힘 은 click으로 펼친 뒤 다시 관찰해라. 찾는 메뉴가 목록에 없으면 마커가 붙은 상위 메뉴를 먼저 펼쳐봐라.
5. 입력 후 자동완성 후보가 관찰 목록에 (+후보 마커나 새 항목으로) 나타나면 원하는 후보를 click해 확정해라. 후보를 무시하고 Enter를 누르면 엉뚱한 검색이 될 수 있다. 후보를 닫아야 하면 press(Escape).
6. select 는 라벨에 {옵션|옵션} 으로 선택지가 표시된다. 그 중 하나를 골라 type의 text로 줘라. 표시되지 않은 값을 지어내지 마라.
7. 본문·목록·가격·시간표처럼 페이지의 실제 내용은 extract나 screenshot으로 확인한 것만 보고해라. 라벨 추측·사전지식 금지. 확인하지 못했으면 못 했다고 말해라. extract 결과가 메뉴/헤더뿐이면 스크롤 후 다시 extract하거나 screenshot으로 확인해라.
8. 이전 페이지로 돌아갈 때는 URL을 다시 입력하지 말고 back을 써라.
9. 인덱스를 추측하지 말고 누를 요소의 텍스트를 알면 find로 인덱스를 먼저 확인해라. 화면 아래 내용은 scroll(index)로 보이게 한 뒤 다시 관찰해라. 검색창 입력 후 바로 제출하려면 type의 submit=true를 써라(자동완성 후보를 골라야 하면 쓰지 마라).
10. 같은 도구를 같은 인자로 반복하지 마라. 두 번 시도해서 안 되면 다른 방법을 쓰거나 wait으로 기다렸다 다시 관찰해라.
11. 사이트가 차단·캡차로 막혔으면 반복하지 말고 done으로 상황을 보고해라.
12. 요소 목록에 없거나 판단이 어려우면 추측하지 말고 ask_user로 묻는다. 필요한 최소한의 행동만 하고, 끝났으면 done으로 결과를 요약한다.
13. 페이지 순회·반복 수집·연속 폼 입력처럼 절차가 정해진 다중 동작은 run_code로 한 번에 처리해라 — 같은 도구 반복은 반복 차단에 걸린다. 삭제/전송/결제 등 위험 동작이 포함되면 run_code를 쓰지 말고 기존 도구로 한 스텝씩 확인해라. 페이지 구조가 불확실하면 먼저 관찰 목록이나 extract로 확인한 뒤 코드를 써라. 코드는 짧게: 관찰 목록에 있는 요소는 el(인덱스)로 잡고, 없으면 page.get_by_role/get_by_text/get_by_label 로케이터를 써라(자동 대기된다). CSS 셀렉터 추측, time.sleep 대기, 오류를 삼키는 try/except는 쓰지 마라 — 기다려야 하면 locator.wait_for()나 page.wait_for_load_state()를 써라. 메일·게시판 본문 에디터(iframe, contenteditable, @f 표시 요소)는 run_code 대신 관찰 목록의 인덱스로 type해라.
14. [사이트 메모리]의 작업·페이지는 URL 대신 navigate page="사이트/이름"으로 가라(이름 그대로, {q}는 fill). 요청에 URL이 있어도 [사이트 메모리]에 그 사이트의 작업이 있으면 page로 가라. 결과에 ▸ 단계가 붙으면 성공했던 경로다 — 관찰에서 같은 라벨을 찾아 입력·선택·클릭을 한 응답에 묶어 실행하고(위험 동작은 확인을 거친다), 확인 조건을 본 뒤 done. 단계의 {대상}은 이번 요청이 말한 글·메일·항목이다 — 목록에서 그것을 찾아 눌러라. 라벨이 안 보이면 평소처럼 찾아라. "다른 사이트에서 성공한 길"이 붙으면 그 순서로 먼저 찾아라. 기억과 실제가 다르면 페이지를 믿어라.
15. 기억에 없는 이름(약칭·별명)은 도메인을 추측해 바로 navigate하지 마라. 검색(한국어 이름이면 네이버 검색 https://search.naver.com/search.naver?query=…)으로 사용자가 말한 곳을 찾고, 후보가 여럿이라 확신이 없으면 ask_user로 확인해라.
16. 조회·조작을 해냈으면 done의 op에 일반 작업 이름(게시글 읽기·댓글 작성·메일 작성·안읽은 메일 확인)을 줘라 — 에이전트가 실제 경로를 기억해 다른 글·메일에도 쓴다. 글 번호·제목·사람 이름은 op에 넣지 마라. 처음 간 사이트([사이트 메모리]에 그 사이트 줄이 없는 곳)면 remember에 사이트 이름·별칭·대표 URL. 이름은 사용자가 부른 이름, 말하지 않았으면 페이지에 보이는 사이트 이름(TITLE·로고 — '받은메일함 - 웹메일'이면 웹메일)이다. 도메인·IP와 일반 명사(메일·카페·게시판), 페이지·경기·공연 제목은 이름이 아니다 — 브랜드까지 써라(네이버 메일, Gmail, 인터파크 티켓). 사용자가 기억한 사이트를 거기 없는 이름으로 불렀으면(기억은 '웹메일'인데 요청은 '회사 메일') done의 called에 그 말을 그대로 줘라. URL·절차·조회 결과·요청에 나온 값(역·날짜·상품·팀)·개인정보는 쓰지 마라. 정보를 못 찾았으면 아무것도 저장하지 마라.
17. 매 턴은 사용자가 보고 있는 탭(WHERE: 사용자 탭)에서 시작한다. "이 페이지/이 탭/여기" 요약·질문처럼 답이 현재 탭에 있으면 navigate하지 말고 그 탭에서 extract/find/click으로 처리해라. 현재 탭에 없는 내용이면 navigate/new_tab을 써라 — 사용자 화면은 그대로 두고 보이지 않는 백그라운드 브라우저(WHERE: 백그라운드)에서 열린다. 사용자가 화면에 띄워 달라고 했거나("열어줘", "보여줘") 로그인·캡차처럼 사용자가 직접 봐야 하는 경우에만 visible=true를 줘라. 백그라운드에서 로그인이 필요하면 visible=true로 사용자 탭에서 열고 ask_user로 로그인을 부탁해라. 첫 메시지에 [작업 위치: 백그라운드]가 있으면 사용자가 현재 탭을 뺀 요청이다 — 백그라운드 탭(WHERE: 백그라운드)에서 시작하니 사용자 탭은 보지도 쓰지도 말고 필요한 페이지로 navigate해라."""


@functools.lru_cache(maxsize=1)
def _claude_env():
    """~/.claude/settings.json의 env 블록 (Claude Code 커스텀 엔드포인트 설정 위치).
    프로세스 시작 후 한 번만 읽는다 — 매 스텝 파일 I/O+JSON 파싱을 없앤다."""
    try:
        s = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
        return s.get("env") or {}
    except Exception:
        return {}


class Cancelled(Exception):
    """사용자 중단으로 요청을 버렸다(abort)."""


def abort():
    """다른 스레드에서 진행 중인 요청을 끊는다. 소켓을 닫아 막힌 read를 풀고 연결은 버린다
    (Windows는 shutdown만으로 recv가 안 풀릴 때가 있어 close까지 한다)."""
    global _conn
    c, _conn = _conn, None
    s = getattr(c, "sock", None) if c is not None else None
    if s is not None:
        for f in (lambda: socket.socket.shutdown(s, socket.SHUT_RDWR), s.close):
            try:
                f()
            except OSError:
                pass


def chat(messages, tools, cancel=None):
    """Anthropic Messages API 호환 엔드포인트에 POST. 반환은 파싱된 dict.
    설정 우선순위: 프로세스 env > ~/.claude/settings.json env > 기본값.
    cancel: 참을 돌려주면 중단 — 재시도하지 않고 Cancelled를 올린다(abort와 함께 쓴다)."""
    cenv = _claude_env()
    body = {
        "model": os.environ.get("AGENT_MODEL") or cenv.get("ANTHROPIC_MODEL") or DEFAULT_MODEL,
        "max_tokens": int(os.environ.get("AGENT_MAX_TOKENS") or 16384),
        "system": _system_blocks(),
        "messages": _cache_messages(messages),
        "tools": tools[:-1] + [dict(tools[-1], cache_control={"type": "ephemeral"})],
    }
    return _post(body, cenv, cancel)


def _cache_messages(messages):
    """대화 기록 캐시 브레이크포인트를 직전 assistant 메시지 끝에 단다.
    최신 user 메시지(tool_result+관찰)는 다음 턴에 agent._stub_observations가 줄이므로
    거기 달면 매번 캐시가 깨진다. 직전 assistant까지는 다음 턴에도 내용이 같아 캐시가 맞는다.
    원본 messages는 건드리지 않는다(히스토리에 cache_control이 쌓이면 브레이크포인트 4개 제한을 넘는다)."""
    for k in range(len(messages) - 1, -1, -1):
        m = messages[k]
        c = m.get("content")
        if m.get("role") != "assistant" or not isinstance(c, list):
            continue
        for j in range(len(c) - 1, -1, -1):
            if c[j].get("type") in ("text", "tool_use"):
                content = list(c)
                content[j] = dict(c[j], cache_control={"type": "ephemeral"})
                return messages[:k] + [dict(m, content=content)] + messages[k + 1:]
        break
    return messages


SITES_PATH = Path(__file__).with_name("_local").joinpath("sites.md")


def _system_blocks():
    """SYSTEM + (있으면) _local/sites.md 사이트 플레이북. 캐시 브레이크는 마지막 블록에 둔다.
    플레이북은 매 호출 다시 읽는다 — 실행 중 파일을 고쳐도 다음 턴부터 반영된다."""
    blocks = [{"type": "text", "text": SYSTEM}]
    try:
        sites = SITES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        sites = ""
    if sites:
        blocks.append({"type": "text", "text": f"[사이트 플레이북]\n{sites}"})
    # 실행 중 바뀌지 않는 값이라 캐시 접두를 깨지 않는다
    blocks.append({"type": "text", "text": f"[환경] OS: {platform.system()} {platform.release()}, "
                   f"홈 폴더: {Path.home()}. 파일 경로는 이 OS 형식으로 쓰고, 모르면 추측하지 말고 ask_user로 물어라."})
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def vision_model():
    """비전 전용 모델 이름. AGENT_VISION_MODEL > settings.json의 ANTHROPIC_VISION_MODEL > None."""
    return os.environ.get("AGENT_VISION_MODEL") or _claude_env().get("ANTHROPIC_VISION_MODEL") or None


def _lookup(env_keys=(), claude_keys=(), default=None):
    """chat/_post와 같은 우선순위로 값을 찾고 (값, 출처)를 돌려준다."""
    for k in env_keys:
        v = os.environ.get(k)
        if v:
            return v, (".env " if k in _ENV_FILE_KEYS else "환경변수 ") + k
    cenv = _claude_env()
    for k in claude_keys:
        v = cenv.get(k)
        if v:
            return v, "~/.claude/settings.json " + k
    return default, "기본값"


def _mask_key(v):
    return ("****" + v[-4:]) if v and len(v) > 8 else ("****" if v else None)


def config():
    """설정 화면용 LLM 설정 목록 [{name, value, source}]. API 키는 끝 4자리만 보인다."""
    base, base_src = _lookup(["ANTHROPIC_BASE_URL"], ["ANTHROPIC_BASE_URL"], DEFAULT_BASE_URL)
    key, key_src = _lookup(["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
                           ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"], None)
    model, model_src = _lookup(["AGENT_MODEL"], ["ANTHROPIC_MODEL"], DEFAULT_MODEL)
    vis, vis_src = _lookup(["AGENT_VISION_MODEL"], ["ANTHROPIC_VISION_MODEL"], None)
    mt, mt_src = _lookup(["AGENT_MAX_TOKENS"], (), "16384")
    return [
        {"name": "엔드포인트", "value": base, "source": base_src},
        {"name": "API 키", "value": _mask_key(key) or "(없음)", "source": key_src if key else "-"},
        {"name": "모델", "value": model, "source": model_src},
        {"name": "비전 모델", "value": vis or "(없음 — screenshot 판독 비활성)",
         "source": vis_src if vis else "-"},
        {"name": "max_tokens", "value": str(mt), "source": mt_src},
    ]


VISION_SYSTEM = ("화면 스크린샷을 보고 질문에 답한다. 화면에 실제로 보이는 것만 사실대로 "
                 "기술하고 절대 추측하거나 사전지식으로 채우지 마라. 질문한 내용이 화면에 "
                 "보이지 않으면 보이지 않는다고 답해라.")


def vision(jpeg_bytes, question):
    """vision_model()로 단일 턴 이미지 질의. 응답 text 블록을 이어붙여 반환."""
    model = vision_model()
    if not model:
        raise RuntimeError("vision model not configured (AGENT_VISION_MODEL / ANTHROPIC_VISION_MODEL)")
    body = {
        "model": model,
        "max_tokens": int(os.environ.get("AGENT_MAX_TOKENS") or 8192),
        "system": VISION_SYSTEM,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(jpeg_bytes).decode()}},
            {"type": "text", "text": question},
        ]}],
    }
    resp = _post(body, _claude_env())
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")


def _post(body, cenv, cancel=None):
    """요청 전송부. chat/vision이 공유한다."""
    global _conn
    base = (os.environ.get("ANTHROPIC_BASE_URL") or cenv.get("ANTHROPIC_BASE_URL")
            or DEFAULT_BASE_URL)
    key = (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
           or cenv.get("ANTHROPIC_API_KEY") or cenv.get("ANTHROPIC_AUTH_TOKEN") or "")
    # urllib는 매 호출 TCP+TLS를 새로 맺는다(프록시를 거치면 핸드셰이크만 1~2.5초 걸리기도 한다).
    # keep-alive 연결을 재사용해 스텝당 핸드셰이크 비용을 없앤다.
    u = urlparse(base)
    path = u.path.rstrip("/") + "/v1/messages"
    headers = {
        "content-type": "application/json",
        "x-api-key": key,
        "authorization": f"Bearer {key}",  # AUTH_TOKEN 계열 프록시 대비
        "anthropic-version": "2023-06-01",
    }
    data = json.dumps(body).encode()
    last = None
    cancelled = cancel or (lambda: False)
    for i in range(3):
        if cancelled():
            _conn = None
            raise Cancelled()
        try:
            conn = _connection(u)
            conn.request("POST", path, data, headers)
            r = conn.getresponse()
            payload = r.read()  # 다 읽어야 연결이 재사용된다
            if cancelled():  # abort로 끊겨 잘린 응답일 수 있다
                _conn = None
                raise Cancelled()
            if r.status >= 400:
                if r.status in (429, 500, 502, 503, 529) and i < 2:
                    _conn = None
                    time.sleep(_backoff(i, r.getheader("retry-after")))
                    continue
                raise RuntimeError(f"LLM HTTP {r.status}: {payload[:500].decode('utf-8', 'replace')}")
            return json.loads(payload)
        except TimeoutError:
            # 120초 응답 없음은 재시도해도 같은 대기만 반복된다(최악 6분 멈춤) — 바로 올린다
            _conn = None
            if cancelled():
                raise Cancelled()
            raise
        except (http.client.HTTPException, OSError) as e:
            # 끊긴 keep-alive 연결/DNS 일시 실패는 연결을 버리고 재시도
            _conn = None
            if cancelled():
                raise Cancelled()
            last = e
            time.sleep(_backoff(i))
    raise last


def _backoff(i, retry_after=None):
    """재시도 대기(초). 서버가 retry-after(초)를 주면 따르되 10초로 캡, 없으면 1,2,4… 지수 백오프."""
    try:
        if retry_after is not None:
            return min(max(float(retry_after), 0.0), 10.0)
    except ValueError:
        pass  # HTTP-date 형식은 무시하고 기본 백오프
    return float(2 ** i)


_conn = None
_conn_key = None


def _connection(u):
    """엔드포인트별 keep-alive 연결을 재사용한다."""
    global _conn, _conn_key
    key = (u.scheme, u.hostname, u.port)
    if _conn is None or _conn_key != key:
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        _conn = cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=120)
        _conn_key = key
    return _conn
