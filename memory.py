"""사이트 메모리 v3 — 에이전트가 성공한 실제 경로를 기록하고, 다음 요청엔 이름만 보여 주고 고른 것만 붙인다.

사이트 하나 = 사용자가 부르는 이름 하나:
  aliases   다른 호칭 (정식 명칭, 카페 ID 등). 작업·메모 이름 같은 일반 명사는 안 된다
  url       대표 URL (홈)
  match     이 사이트 페이지임을 알아보는 URL 조각. url의 호스트(포트 포함)+첫 경로는 자동으로 포함된다
  notes     키 → 사이트 제약 한 줄(로그인 필요 등). 최대 5개
  tasks     작업 이름(done.op) → 에이전트가 본 성공 경로. 최대 20개
    kind read|act  url 진입 URL(검색 자리는 {q})  path 진입 URL에 오기까지 누른 라벨들
    how  읽는 법(read)  steps 진입 뒤 type/select/click(act, 입력값은 {칸 라벨} 자리)  check 마지막 동작 뒤 URL
    ok 성공 횟수  fail 연속 실패(2 이상이면 숨긴다)  last 마지막 성공 날짜
  hits/used 성공 기록 횟수·마지막 사용 — 퇴출 점수(횟수 × 최근성)

읽기: 요청마다 이름·URL로 걸린 사이트는 한 줄(작업·페이지 이름 + 메모 2줄), 나머지는 작업 이름별 목록 한 줄.
URL은 보여주지 않는다 — 모델은 navigate에 '사이트/작업'을 주고 에이전트가 URL과 단계 한 줄로 바꾼다(resolve).
옛 형식(v1·v2) 파일은 읽을 때 v3로 바꾸고 원본을 sites.v1.bak / sites.v2.bak로 남긴다.
저장 파일은 기본 _local/memory/sites.json (git 미추적). AGENT_MEMORY 환경변수로 바꿀 수 있다.
"""
import copy
import json
import os
import re
import shutil
import time
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

DEFAULT_PATH = Path(__file__).with_name("_local") / "memory" / "sites.json"
VERSION = 3
SITE_CAP = 50         # 넘으면 점수(사용 횟수 × 최근성)가 가장 낮은 사이트부터 버린다
TASK_CAP = 20         # 사이트당 작업 수
NOTE_CAP = 5          # 사이트당 메모 수
LIST_CAP = 8          # aliases / match 항목 수
KEY_LEN = 30
VALUE_LEN = 300
LIST_BUDGET = 400     # '기억한 작업' 목록 글자 예산
REST_BUDGET = 150     # '그 밖에' 사이트 이름 글자 예산
NAME_SITES = 3        # 목록에서 작업 이름 하나에 적는 사이트 수 — 흔한 이름(글쓰기) 하나가 예산을 다 먹지 않게
HIDE_FAIL = 2         # 연속 실패가 이만큼이면 목록에서 숨긴다
HALF_LIFE = 30        # 사이트 점수의 최근성 반감기(일)

# 페이지가 바뀌면 틀리는 것(셀렉터·코드)과 금방 낡는 조회 결과(가격·날짜)는 저장하지 않는다
_TAGS = r"a|div|span|button|input|textarea|li|ul|ol|td|tr|table|form|iframe|img|section|article|select|label|nav"
SELECTOR = re.compile(rf"\b(?:{_TAGS})[.#][a-z_][\w-]*|\b[a-z]+\[[a-z-]+[~|^$*]?=|(?:^|\s)#[a-z_][\w-]*"
                      r"|nth-child|:has\(", re.I)
CODE = re.compile(r"run_code|\bfill\(|query_?selector|locator\(|evaluate\(|get_by_\w+", re.I)
VOLATILE = re.compile(r"[$₩€£]\s?\d|\d[\d,.]*\s?(?:원|달러|usd|krw)|\d{4}\s?년|\d{4}[-./]\d{1,2}[-./]\d{1,2}"
                      r"|\d{1,2}\s?월\s?\d{1,2}\s?일", re.I)
_SLD = {"co", "or", "go", "ne", "ac", "re", "pe", "com", "net", "org", "gov", "edu"}
URLS = re.compile(r"https?://[A-Za-z0-9.:/?&=%#~+_-]+")  # 요청 문구 속 URL(뒤에 붙은 한글·괄호는 뺀다)
# 스킴 없는 주소(search.naver.com/search.naver?…)와 쿼리 꼴(?query=)도 URL로 본다
URLISH = re.compile(r"://|\b[\w-]+\.[a-z]{2,}(?::\d+)?/\S*|\?\w+=", re.I)
# 조작 계열(정규식): 요청 '글을 써줘'와 작업 이름 '글쓰기'처럼 _grams가 안 겹치는 같은 조작을 묶는다. 앞 계열부터 본다(댓글쓰기 = 댓글)
# 휴리스틱: 손으로 만든 작은 표 — 표에 없는 말(업로드·기안 등)은 못 묶고 '회원 등록'도 글쓰기로 본다. 틀리면 표를 늘린다
FAMILIES = {"댓글": ("댓글", "답글"), "승인": ("승인", "결재(?:해|하|처리)"), "메일": ("메일", "보내", "발송"),  # 반려는 승인과 다른 작업
            "글쓰기": ("쓰기", "써", "작성", "올려", "게시(?![판글물])", "등록")}
# 결재 메뉴·함 이름(전자결재, 결재대기함) — 계열·위험 판단 전에 지운다
BOX = re.compile(r"\S*(?:결재함|대기함|완료함|기안함)|전자결재|결재\s?시스템")


def _norm(url):
    """비교용 URL: 스킴·www. 제거, 소문자, 프래그먼트 제거."""
    u = url.strip().lower().split("#", 1)[0]
    u = re.sub(r"^[a-z]+://", "", u)
    return u[4:] if u.startswith("www.") else u


def _host(norm_url):
    return norm_url.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]


def _url_host(url):
    return _host(_norm(url))


def host_name(url):
    """remember 없이 처음 간 사이트의 자동 이름: 호스트[:포트] (www. 제외)."""
    return urlparse(url).netloc.lower().removeprefix("www.")


TITLE_SEP = re.compile(r"\s+[-·•–—]\s+|\s*:\s+|\s*\|\s*|\s*::\s*")


def _title_segs(title):
    """제목 조각: 구분자로 자르고 '(3)' 같은 개수는 지운다. 긴 숫자·@가 든 조각(계정·번호)과 긴 조각(설명문)은 뺀다.
    'Cafe24'처럼 짧은 숫자가 든 이름은 둔다. 콜론은 뒤에 공백이 있을 때만 구분자다('10:30'은 시각)."""
    segs = (re.sub(r"[(\[]\d+[)\]]", "", s).strip() for s in TITLE_SEP.split(title or ""))
    return [s for s in segs if 2 <= len(s) <= 20 and not re.search(r"@|\d{4,}", s)]


def title_name(title, home):
    """제목에서 사이트 이름: 지금 제목과 홈(/) 제목에 함께 있는 조각이 하나면 그것('결재대기함 - 결재시스템' + '결재시스템').
    조각이 안 겹치면 낱말로 본다('코레일 - …' + '코레일 승차권예매'). 홈 제목이 없거나 같으면 페이지 이름과 사이트 이름을
    가를 수 없어 조각이 하나인 제목('NAVER')만 받는다. 겹치는 게 여럿이면(계정 이름이 든 메일 제목) None."""
    title, home = (title or "").strip(), (home or "").strip()
    a, b = _title_segs(title), _title_segs(home)
    if not b or title == home:
        common = a if len(TITLE_SEP.split(title)) == 1 else []
    else:
        words = lambda segs: re.split(r"[\s()（）]+", " ".join(segs))  # noqa: E731 — 'NOL(야놀자)'의 NOL도 낱말
        bw = words(b)
        common = [s for s in a if s in b] or [w for w in words(a) if len(w) >= 2 and w in bw]
    common = list(dict.fromkeys(common))
    return common[0].split(", ")[0] if len(common) == 1 else None


def _family(text):
    return next((f for f, words in FAMILIES.items() if any(re.search(w, text) for w in words)), None)


def _drop(text, names):
    """요청에서 사이트 이름·별칭을 지운다 — 판단이 이름 속 말('결재시스템'의 결재)에 걸리지 않게. 동사로 쓴 별칭('결재해줘')은 둔다."""
    names = sorted((re.escape(n.lower()) for n in names if n.strip()), key=len, reverse=True)
    return re.sub(f"(?:{'|'.join(names)})(?!해|하|처리)", " ", text) if names else text


def _kin(text):
    """작업 이름 n이 text와 같은 작업인가: text가 조작 계열에 들면 같은 계열, 아니면 _grams 겹침."""
    fam, grams = _family(text), _grams(text) - STOP
    return lambda n: _family(n) == fam if fam else bool(grams & _grams(n))


def _base(host):
    """등록 도메인(naver.com, ppomppu.co.kr) — 한 사이트의 서브도메인끼리는 같다."""
    parts = host.split(".")
    n = 3 if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SLD else 2
    return ".".join(parts[-n:])


def _id_at(segs):
    """경로 셋째·넷째 칸의 숫자 ID(5자리+) 위치 — 새 네이버 카페(f-e/cafes/12345678)처럼 앞 칸이 모든 카페에 같은
    주소는 ID가 사이트를 가른다. 바로 앞 칸이 모음 이름(cafes·groups)일 때만 — 글 번호(clien.net/service/board/park/
    18817112, blog.naver.com/아이디/글번호)는 사이트가 아니다."""
    return next((i for i, s in enumerate(segs[:4])
                 if i >= 2 and re.fullmatch(r"\d{5,}", s) and re.fullmatch(r"[a-z]+s", segs[i - 1])), None)


def _default_match(url):
    """url의 사이트 부분: 호스트(포트 포함 — 127.0.0.1:8765와 :8766은 다른 사이트)+첫 경로 칸(파일 이름이면 뺀다).
    사이트를 가르는 숫자 ID가 있으면 그 ID까지(cafe.naver.com/f-e/cafes/12345678)."""
    p = urlparse(url if "://" in url else f"https://{url}")
    host = p.netloc.lower().rsplit("@", 1)[-1]
    host = host[4:] if host.startswith("www.") else host
    segs = [s.lower() for s in p.path.split("/") if s]
    i = _id_at(segs)
    # ID 앞 칸(f-e·ca-fe)은 뺀다 — 카페 목록(f-e/cafes/ID)과 글 iframe(ca-fe/cafes/ID)은 같은 카페다
    keep = segs[i - 1:i + 1] if i is not None else segs[:1] if segs and "." not in segs[0] else []
    return "/".join([host, *keep])


def site_home(url):
    """대표 URL로 둘 사이트 홈: 사이트를 가르는 숫자 ID가 있으면 그 ID까지(인기글·글 주소 대신 카페 홈). 없으면 None."""
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    i = _id_at([s.lower() for s in segs])
    return f"{p.scheme}://{p.netloc}/{'/'.join(segs[:i + 1])}/" if i is not None and p.scheme else None


def _params(template):
    """템플릿의 자리 이름 순서대로 ({q}, {folder})."""
    return list(dict.fromkeys(re.findall(r"\{(\w+)\}", template)))


def same_page(stored, url):
    """url이 기억한 페이지 stored와 같은 진입점인가: 호스트·경로가 같고({이름} 경로 자리는 아무 세그먼트),
    양쪽에 다 있는 쿼리 파라미터의 값이 같다({이름} 값은 아무 값). 한쪽에만 있는 파라미터(page=2)는 무시한다.
    뽐뿌처럼 게시판이 쿼리(id=freeboard)로 갈리는 사이트가 있어 경로만으로는 부족하다."""
    s, u = urlparse("//" + _norm(stored)), urlparse("//" + _norm(url))
    sp, up = s.path.rstrip("/"), u.path.rstrip("/")
    if s.netloc != u.netloc:
        return False
    if "{" in sp:
        if not re.fullmatch(re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(sp)), up):
            return False
    elif sp != up:
        return False
    sq, uq = parse_qs(s.query), parse_qs(u.query)
    # 템플릿 자리(q={q})는 url에 있어야 한다 — menus/0?viewType=L은 검색 menus/0?q={q}가 아니다
    return all(k in uq if "{" in "".join(v) else k not in uq or uq[k] == v for k, v in sq.items())


def _grams(text):
    """관련성 비교용 조각: 영문·숫자 단어(2자 이상)와 한글 두 글자 조각."""
    out = set()
    for tok in re.findall(r"[a-z0-9]+|[가-힣]+", text.lower()):
        if tok.isascii():
            if len(tok) >= 2:
                out.add(tok)
        else:
            out.update(tok[i:i + 2] for i in range(len(tok) - 1))
    return out


# 어느 요청에나 드는 말 — '이 페이지 요약'이 '날씨 페이지'와 겹친다고 보지 않게 뺀다
STOP = _grams("페이지 알려줘 해줘 보여줘 찾아줘 주세요 요약 사이트 에서 오늘 지금")
# 읽기 작업 이름에 흔히 붙는 말 — '대기 문서 보기'와 '결재대기함 목록 조회'를 같은 작업으로 볼 때 이 말들은 빼고 견준다
READ_WORDS = _grams("목록 조회 보기 확인 내용 리스트 문서 정보 현황")


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def _today():
    return date.today().isoformat()


def _days(stamp):
    try:
        return (date.today() - date.fromisoformat(str(stamp)[:10])).days
    except ValueError:
        return 0


def _new_site():
    return {"aliases": [], "url": "", "match": [], "notes": {}, "tasks": {}, "hits": 0, "used": "", "updated": ""}


def _site_score(e):
    return (e["hits"] + 1) * 0.5 ** (_days(e["used"] or e["updated"]) / HALF_LIFE)


def _live(e):
    """숨기지 않은 작업들(연속 실패 HIDE_FAIL 미만)."""
    return {k: t for k, t in e["tasks"].items() if t["fail"] < HIDE_FAIL}


def item_id(url, keep=""):
    """URL 끝 경로·쿼리 값·# 뒤 끝 조각이 개별 글·항목 ID면 그 값. 사이트 자체 ID(keep 안)는 뺀다.
    ID = 6자리 이상 일련번호, 또는 대소문자가 섞인 16자 이상 토큰(Gmail #inbox/FMfcgz… 같은 메일 ID).
    게시판·메뉴 번호(menus/3, id=12345)는 짧아서, 0으로 시작하는 지역·분류 코드(today/09140104)는
    일련번호가 아니라서 걸리지 않는다."""
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    frag = [s for s in re.split(r"[/?&=]", p.fragment) if s]
    # 캐시 무효화용 시각 값(t=1790932238832)은 항목 ID가 아니다 — 검색 결과를 '글 하나를 연 것'으로 보지 않게
    vals = segs[-1:] + [v for k, vs in parse_qs(p.query).items() if k.lower() not in TIME_KEYS for v in vs]
    token = lambda v: re.fullmatch(r"[A-Za-z0-9_-]{16,}", v) and re.search(r"[A-Z]", v) and re.search(r"[a-z]", v)  # noqa: E731
    # 긴 토큰은 # 뒤(메일 ID)만 — 쿼리의 긴 토큰은 검색 조건(NOL filter=…)이다
    return next((v for v in vals + frag[-1:] if re.fullmatch(r"[1-9]\d{5,}", v) and v not in keep), None) or \
        next((v for v in frag[-1:] if token(v) and v not in keep), None)


TIME_KEYS = {"t", "ts", "_", "time", "timestamp", "nocache", "cb"}

# 사이트 이름·별칭이 될 수 없는 일반 명사 — '메일'로 기억하면 구글 메일·업무 메일 요청에도 네이버 메일이 걸린다
# (결재·그룹웨어는 보통 회사에 하나라 별칭으로 둔다). '네이버 카페'처럼 모든 카페 제목에 붙는 플랫폼 이름도 뺀다
GENERIC_NAMES = {"메일", "이메일", "카페", "블로그", "뉴스", "지도", "쇼핑", "게시판", "홈", "메인", "로그인", "검색",
                 "포털", "캘린더", "드라이브", "메신저", "mail", "email", "home", "main",
                 "login", "inbox", "news", "blog", "map", "search", "cafe", "네이버 카페", "다음 카페", "네이버 블로그"}
# 경기·공연 제목처럼 페이지 이름인 말(키움 vs KT (10.6)) — 사이트 이름이 아니다. 호스트 이름(127.0.0.1:8765)은 걸리지 않게 점 숫자는 안 본다
PAGE_TITLE = re.compile(r"\bvs\b|\d+\s*(?:월|일)\b", re.I)


def generic_name(name):
    """사이트 이름으로 쓰면 다른 서비스와 섞이는 일반 명사인가."""
    return str(name or "").strip().lower() in GENERIC_NAMES


def _keep(e):
    return e["url"] + " " + " ".join(e["match"])


def _check_text(kind, k, v):
    if URLISH.search(v):
        return f"error: {kind}[{k}]에 URL을 쓰지 마라 — 사이트 제약만 한 줄로"
    if SELECTOR.search(v) or CODE.search(v):
        return (f"error: {kind}[{k}]에 CSS 셀렉터·코드가 있다 — 페이지가 바뀌면 틀린다. "
                "화면에 보이는 요소 라벨로 써라")
    if VOLATILE.search(v):
        return f"error: {kind}[{k}]에 가격·날짜 같은 조회 결과가 있다 — 금방 낡으니 저장하지 마라"
    return None


def _task_err(e, kind, url):
    """작업으로 저장하지 않을 진입 URL이면 그 이유."""
    if not url.startswith(("http://", "https://")):
        return f"진입 URL이 http(s)가 아니다: {url[:60]!r}"
    if e["url"] and _base(_url_host(url)) != _base(_url_host(e["url"])):
        return f"{_url_host(url)}는 이 사이트({_url_host(e['url'])})의 페이지가 아니다"
    if kind == "read" and item_id(url, _keep(e)):
        return "읽기 진입 URL이 개별 글·항목이다"
    return None


def _pattern(sites, label, kin, skip=()):
    """다른 사이트들에서 kin(작업 이름)인 조작 작업에 성공한 길(많은 순 2가지) 한 줄. 없으면 ''."""
    paths = Counter(" → ".join(t["path"]) for k, e in sites.items() if k not in skip
                    for n, t in _live(e).items() if kin(n) and t["kind"] == "act" and t["path"])
    top = paths.most_common(2)
    return f'다른 사이트에서 "{label}"에 성공한 길: ' + " · ".join(f"{p}({c}곳)" for p, c in top) if top else ""


def _fit(parts, budget):
    """' · '로 이었을 때 글자 예산 안에 드는 앞쪽 개수."""
    n = size = 0
    for p in parts:
        size += len(p) + 3
        if size - 3 > budget:
            break
        n += 1
    return n


def _detail(site, key, t):
    """고른 작업의 1단계 한 줄: 부를 page, 조작은 단계와 확인, 읽기는 진입 뒤 누를 것과 읽는 법.
    시작 때·page 이동·도착 때 같은 문자열이어야 같은 대화에서 다시 보내지 않는다."""
    page = f" (page={site}/{key})"
    if t["kind"] == "act":
        return (f"▸ {key}(성공 {t['ok']}회){page}: {' · '.join(t['steps'])}"
                + (f" | 확인: {t['check']}" if t.get("check") else ""))
    return f"▸ {key}{page}: " + "".join(f"{s} → " for s in t.get("steps", [])) + f"읽기 {t.get('how') or ''}".rstrip()


def upgrade(data):
    """옛 형식(v1: pages/notes, v2: sections) → (v3 sites, 버린 항목). pages는 확인된 읽기 작업(ok=1)으로,
    v2 procedures·notes와 v1 notes('→' 절차 포함)는 notes 한 줄로 옮긴다. 저장 검증에 걸리는 것은 버린다."""
    out, dropped = {}, []
    for name, s in (data.get("sites") or {}).items():
        e = _new_site()
        e.update(aliases=list(s.get("aliases") or []), url=s.get("url") or "", match=list(s.get("match") or []),
                 hits=s.get("hits", int(bool(s.get("used")))), used=s.get("used") or "",
                 updated=s.get("updated") or "")
        if "sections" in s:  # v2
            secs = list(s["sections"].values())
            pages = {k: it["v"] for x in secs for k, it in x.get("pages", {}).items()}
            notes = {k: it["v"] for x in secs for kind in ("procedures", "notes") for k, it in x.get(kind, {}).items()}
        else:  # v1
            pages, notes = dict(s.get("pages") or {}), dict(s.get("notes") or {})
        last = (e["updated"] or _today())[:10]
        for k, v in pages.items():
            err = _task_err(e, "read", str(v)) or (len(e["tasks"]) >= TASK_CAP and "작업 상한")
            if err:
                dropped.append(f"{name}.pages[{k}]: {err}")
                continue
            e["tasks"][k] = {"kind": "read", "url": v, "path": [], "how": "", "ok": 1, "fail": 0, "last": last}
        for k, v in notes.items():
            err = _check_text("notes", k, str(v)) or (len(e["notes"]) >= NOTE_CAP and "메모 상한")
            if err:
                dropped.append(f"{name}.notes[{k}]: {err}")
                continue
            e["notes"][k] = v
        out[name] = e
    return out, dropped


class Memory:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("AGENT_MEMORY") or DEFAULT_PATH)

    # ---- 저장소 ----
    def _load(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(text)
            if data.get("version") == VERSION and isinstance(data["sites"], dict):
                return data["sites"]
            sites = upgrade(data)[0]  # 옛 형식 — 멈추지 않고 그 자리에서 바꾼다. 원본은 sites.v1.bak / sites.v2.bak
        except Exception:  # JSON이 아니거나 구조가 깨졌다 — 매 요청 멈추지 않게 원본을 남기고 빈 메모리로 시작
            shutil.copy(self.path, self.path.with_name(f"{self.path.stem}.corrupt.bak"))
            self._save({})
            return {}
        shutil.copy(self.path, self.path.with_name(f"{self.path.stem}.v{data.get('version', 1)}.bak"))
        self._save(sites)
        return sites

    def _save(self, sites):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": VERSION, "sites": sites}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)  # 중간에 죽어도 파일이 반쯤 쓰이지 않게

    @staticmethod
    def _find(sites, name):
        """이름·별칭(대소문자 무시)으로 사이트 키. 괄호째 저장된 옛 이름(NOL(야놀자))은 조각(NOL·야놀자)으로도 찾는다."""
        n = name.lower()
        hit = lambda ws: any(w.strip().lower() == n for x in ws for w in [x, *re.split(r"[()（）]", x)] if w.strip())  # noqa: E731
        return next((k for k, e in sites.items() if k.lower() == n or n in (a.lower() for a in e["aliases"])), None) or \
            next((k for k, e in sites.items() if hit([k, *e["aliases"]])), None)

    # ---- 쓰기 ----
    def remember(self, site, url="", aliases=(), match=(), notes=None):
        """사이트 이름·별칭·홈·match·메모를 추가/갱신한다(done.remember). 검증에 걸린 메모는 그 항목만 버리고
        결과 문자열에 적는다 — 모델에게 돌려보내지 않는다. 그 밖의 오류는 'error: …'이고 아무것도 쓰지 않는다."""
        site = (site or "").strip()
        if not site:
            return "error: site 이름이 필요하다"
        notes = notes or {}
        if not isinstance(notes, dict):
            return "error: notes는 {키: 값} 객체여야 한다"
        if isinstance(aliases, str) or isinstance(match, str):
            return "error: aliases/match는 문자열 배열이어야 한다"
        # 'NOL(야놀자)' → 이름 NOL + 별칭 야놀자 — 괄호째 두면 'NOL'·'야놀자'로 불러도 안 걸린다
        if m := re.fullmatch(r"(.+?)\s*[(（](.+?)[)）]", site):
            site, aliases = m[1].strip(), [m[2].strip(), *aliases]
        url = (url or "").strip()
        if url and not url.startswith(("http://", "https://")):
            return f"error: url은 http(s)://로 시작해야 한다: {url}"
        sites = self._load()
        key = self._find(sites, site) or site
        if key not in sites and generic_name(site):
            return (f"error: 사이트 이름 {site!r}은 일반 명사다 — 다른 서비스(구글 메일·업무 메일)의 요청도 이 사이트로 간다. "
                    "브랜드가 든 이름(예: 네이버 메일, Gmail)으로 줘라")
        if key not in sites and PAGE_TITLE.search(site):
            return f"error: 사이트 이름 {site!r}은 페이지(경기·공연·날짜) 제목이다 — 사이트 브랜드 이름(예: 인터파크 티켓)으로 줘라"
        if any(re.search(r"\s", str(m)) for m in match):
            return "error: match는 공백 없는 URL 조각이다(예: cafes/12345678) — 페이지 제목을 넣지 마라"
        # 호스트 이름으로 자동 만든 같은 사이트가 있으면 그 이름을 바꾼다(옛 이름은 버린다). 그 사이트의 작업이 모두
        # 새 url/match에 맞을 때만 — cafe.naver.com 자동 사이트가 다른 카페 하나로 통째로 넘어가지 않게
        if key not in sites and url:
            new = {"url": url, "match": [_norm(str(m)).rstrip("/") for m in match], "tasks": {}}
            auto = next((k for k in [host_name(url), *self._at(sites, _norm(url))]
                         if k in sites and k == host_name(sites[k]["url"])
                         and all(self._on_site(_norm(t["url"]), new) for t in sites[k]["tasks"].values())), None)
            if auto:
                sites[key] = sites.pop(auto)
            # 같은 홈(호스트+첫 경로가 똑같다)의 기억 사이트가 하나면 새 이름은 그 사이트의 다른 호칭이다 — 둘로 갈라지면
            # 그 주소가 어느 사이트인지 못 정한다(site_at=None). 서브도메인·다른 카페(첫 경로가 다르면)는 따로 둔다
            same = [k for k, x in sites.items() if x["url"] and _default_match(x["url"]) == _default_match(url)]
            if key not in sites and len(same) == 1:
                key = same[0]
        e = copy.deepcopy(sites[key]) if key in sites else _new_site()
        if url:
            url, old = site_home(url) or url, e["url"]
            # 같은 사이트 안의 더 깊은 페이지(결재완료함 주소 등)로 대표 URL을 바꾸지 않는다 — 처음 정한 홈을 둔다
            if old and (_default_match(url) + "/").startswith(_default_match(old) + "/"):
                url = site_home(old) or old
            e["url"] = url
        for m in match:
            m = _norm(str(m)).rstrip("/")
            if len(m.strip("/")) < 4:
                return f"error: match가 너무 짧다(호스트나 경로/ID 조각이어야 함): {m!r}"
            if "/" not in m and "." not in m:
                m = "/" + m  # 맨 ID(examplecafe, 12345678)는 경로 세그먼트로만 맞춘다
            if m not in self._patterns(e):  # url에서 자동으로 나오는 조각은 저장하지 않는다
                e["match"].append(m)
        dropped = []
        for k, v in notes.items():
            k, v = str(k).strip(), str(v).strip()
            err = (f"notes 키는 '/' 없이 1~{KEY_LEN}자: {k!r}" if not k or len(k) > KEY_LEN or "/" in k
                   else f"notes[{k}]가 {VALUE_LEN}자를 넘는다" if len(v) > VALUE_LEN
                   else _check_text("notes", k, v))
            if err:
                dropped.append(err.removeprefix("error: "))
                continue
            e["notes"].pop(k, None)
            if v:  # 빈 값 = 삭제
                e["notes"][k] = v
        while len(e["notes"]) > NOTE_CAP:
            dropped.append(f"notes 상한 {NOTE_CAP}개라 '{next(iter(e['notes']))}' 삭제")
            e["notes"].pop(next(iter(e["notes"])))

        # 별칭은 요청 문구와 부분 문자열로 맞춰 사이트 줄을 붙인다 — 작업·메모 이름 같은 일반 명사면
        # 그 말이 든 모든 요청(예: '날씨')에 사이트가 붙는다
        taken = {k.lower() for x in [*(x for n, x in sites.items() if n != key), e]
                 for k in [*x["tasks"], *x["notes"]]}
        known = {a.lower() for a in e["aliases"]} | {key.lower()}
        for a in [site, *aliases]:
            a = str(a).strip()
            if not a or a.lower() in known or (a != site and re.fullmatch(r"[\d\s./-]+", a)):  # 괄호 속 날짜(10.6)는 별칭이 아니다
                continue
            if len(a) < 2 or a.lower() in taken or generic_name(a):
                return (f"error: 별칭 {a!r}은 작업·메모 이름 같은 일반 명사다 — 그 말이 든 모든 요청에 "
                        "이 사이트가 붙는다. 빼라")
            e["aliases"].append(a)
            known.add(a.lower())
        for field in ("aliases", "match"):
            if len(e[field]) > LIST_CAP:
                return f"error: {field}가 상한 {LIST_CAP}개를 넘는다"

        e["updated"] = e["used"] = _now()
        sites[key] = e
        self._prune(sites, keep=key)
        self._save(sites)
        return f"기억함: {key}" + (f" (버림: {'; '.join(dropped)})" if dropped else "")

    def record(self, site, name, task):
        """에이전트가 본 성공 경로 task({kind, url, path, how | steps, check})를 site의 작업 name으로 저장한다.
        같은 이름은 덮어쓰고 ok+1, fail=0. task=None이면(진입을 모르는 회차) 있던 경로에 횟수만 더한다.
        거부하면 'error: …'."""
        name = re.sub(r"\{\w*\}", "", str(name or "")).strip()
        sites = self._load()
        key = self._find(sites, str(site or "").strip())
        if not key:
            return f"error: 기억한 사이트 '{site}'가 없다"
        if not name or len(name) > KEY_LEN or "/" in name:
            return f"error: 작업 이름은 '/' 없이 1~{KEY_LEN}자: {name!r}"
        e = sites[key]
        old = e["tasks"].get(name) or {}
        task = task or old
        if not task:
            return f"error: '{name}' 기억이 없고 진입을 몰라 새로 기록하지 않는다"
        err = _task_err(e, task["kind"], task["url"]) or _check_text("op", name, name)
        if not err and old.get("kind") == "act" and task["kind"] != "act":
            err = f"조작 작업 '{name}'을 읽기로 덮어쓰지 않는다"
        if err:
            return f"error: {err.removeprefix('error: ')}"
        # 기억으로 바로 간 회차는 path가 비지만, 처음 찾아간 길은 다른 사이트의 패턴으로 남긴다
        # 읽기 작업의 path는 어디에도 안 보인다(▸ 줄은 steps·how) — 남기면 거쳐 간 글 제목만 쌓인다
        e["tasks"][name] = {**task, "path": [] if task["kind"] == "read" else task.get("path") or old.get("path", []),
                            "ok": old.get("ok", 0) + 1, "fail": 0, "last": _today()}
        if len(e["tasks"]) > TASK_CAP:
            t = e["tasks"]
            del t[min((k for k in t if k != name), key=lambda k: (t[k]["fail"] < HIDE_FAIL, t[k]["ok"], t[k]["last"]))]
        e["hits"] += 1
        e["used"] = _now()
        self._prune(sites, keep=key)
        self._save(sites)
        return f"기억함: {key}/{name}({task['kind']})"

    def fail(self, site, name):
        """기억한 작업을 써 봤는데 끝내지 못했다 — 연속 실패 +1(HIDE_FAIL 이상이면 숨긴다. 성공하면 0)."""
        sites = self._load()
        t = sites.get(site, {}).get("tasks", {}).get(name)
        if t:
            t["fail"] += 1
            self._save(sites)

    def tasks(self, site):
        """사이트의 작업 전부(숨긴 것 포함) — 기록할 이름을 정할 때 쓴다."""
        return self._load().get(site, {}).get("tasks", {})

    def names(self, site):
        """사이트 이름과 별칭 — 개인정보 검사에서 사용자 문구와 겹쳐도 되는 말."""
        return [site, *self._load().get(site, {}).get("aliases", [])]

    def named_in(self, site, text):
        """text(요청)가 site를 이름·별칭·URL 조각으로 부르는가."""
        e, t = self._load().get(site), (text or "").lower()
        return bool(e) and self._named(t, site, e)

    @classmethod
    def _named(cls, t, k, e):
        """요청 t가 사이트 k를 이름·별칭·URL 조각으로 부르는가. 일반 명사 이름('메일')은 부른 것으로 보지 않는다 —
        '구글 메일 확인'이 '메일'로 기억한 네이버 메일에 걸리지 않게. 괄호째 저장된 옛 이름(NOL(야놀자))은 조각으로도 본다."""
        names = [k, *e["aliases"]]
        names += [p.strip() for n in names for p in re.split(r"[()（）]", n) if len(p.strip()) >= 2]
        pats = cls._patterns(e)
        return (any(cls._mentions(t, n) for n in names if not generic_name(n))
                or any(m in t for m in pats) or any(cls._url_match(_norm(u), m) for u in URLS.findall(t) for m in pats))

    @staticmethod
    def _prune(sites, keep=None):
        while len(sites) > SITE_CAP:
            del sites[min((k for k in sites if k != keep), key=lambda k: _site_score(sites[k]))]

    # ---- 읽기 ----
    def recall(self, task, current_url=""):
        """요청 시작 때 붙일 텍스트(없으면 None)와 줄을 보여준 사이트 집합.
        걸린 사이트(이름·별칭·match·현재 URL)는 한 줄씩, 나머지는 작업 이름별 목록과 '그 밖에' 이름,
        요청과 겹치는 조작 작업을 걸린 사이트가 모르면 다른 사이트들에서 성공한 길 한 줄."""
        sites = self._load()
        if not sites:
            return None, set()
        t = (task or "").lower()
        cur = current_url if current_url.startswith(("http://", "https://")) else ""
        named = [k for k, e in sites.items() if self._named(t, k, e)]
        # 요청이 기억에 없는 영문 이름을 '…에서'로 불렀다(NOL에서) — 열린 탭(앞 작업의 네이버 스포츠)도 다른 사이트의
        # 기억도 그곳이 아니다. 휴리스틱: 영문 이름만 — 한글 이름('야놀자에서')은 '서울에서'(출발역)와 못 가른다
        place = not named and re.search(r"(?<![a-z0-9])[a-z][a-z0-9]{1,19}(?=\s*에서)", URLS.sub(" ", t))
        hits = list(dict.fromkeys(named + (self._at(sites, _norm(cur)) if cur and not place else [])))
        lines = []
        for k in hits:  # 걸린 사이트 줄 + 요청한 조작(같은 계열)을 그 사이트가 알면 그 단계 줄
            lines += [self._line(k, sites[k], cur), (self.hint(k, t) or ("", ""))[1]]
        grams = _grams(t) - STOP
        rest = sorted((k for k in sites if k not in hits), key=lambda k: _site_score(sites[k]), reverse=True)
        owners = {}  # 작업 이름 → 사이트들. rest가 점수순이라 이름도 가장 높은 사이트 순서로 들어간다
        for k in rest:
            for n in _live(sites[k]):
                owners.setdefault(n, []).append(k)
        names = sorted(owners, key=lambda n: not grams & _grams(n))  # 요청과 겹치는 이름 먼저
        # page에 그대로 넣는 '사이트/작업'. 이름 하나에 사이트 NAME_SITES곳까지, 넘으면 '글쓰기 외 n곳'
        items = [x for n in names for x in [(f"{s}/{n}", s) for s in owners[n][:NAME_SITES]]
                 + ([(f"{n} 외 {len(owners[n]) - NAME_SITES}곳", "")] if len(owners[n]) > NAME_SITES else [])]
        # 요청에 URL이 있는데 어느 기억 사이트도 아니면 목록을 싣지 않는다 — 다른 사이트의 작업을 부르지 않게(패턴 줄만)
        # 사이트를 부르지 않은 요청을 열린 탭에서 하면 그 탭의 일이다 — 클리앙 탭의 '인기글'이 예시카페 인기글로 가지 않게
        # 휴리스틱: 열린 탭과 무관한 요청('메일 확인해줘' — 클리앙 탭)도 목록을 못 받는다. 사이트 이름으로 부르면 받는다
        urls = URLS.findall(t)
        foreign = urls and not any(self._at(sites, _norm(u)) for u in urls) or cur and not named or place
        n = 0 if foreign else _fit([x for x, _ in items], LIST_BUDGET)
        if n:
            lines.append("기억한 작업(page로 부른다): " + " · ".join(x for x, _ in items[:n]) + (" …" if n < len(items) else ""))
        listed = {s for _, s in items[:n]}
        others = [k for k in rest if k not in listed]
        m = 0 if foreign else _fit(others, REST_BUDGET)
        if m:
            lines.append(f"그 밖에: {' · '.join(others[:m])}" + (f" 외 {len(others) - m}곳" if m < len(others) else ""))
        # 요청한 조작(같은 계열)을 걸린 사이트가 모르면 다른 사이트들에서 성공한 길
        t = BOX.sub(" ", _drop(t, [n for k in hits for n in [k, *sites[k]["aliases"]]]))
        kin = _kin(t)
        acts = [x for e in sites.values() for x, v in _live(e).items() if v["kind"] == "act" and kin(x)]
        if acts and not any(kin(x) and v["kind"] == "act" for k in hits for x, v in _live(sites[k]).items()):
            lines.append(_pattern(sites, _family(t) or acts[0], kin, hits))
        return "\n".join(x for x in lines if x) or None, set(hits)

    def on_page(self, url, shown):
        """작업 중 들어온 url이 이번 요청에 아직 줄을 안 보낸 기억 사이트면 [(이름, 그 사이트 한 줄)]. shown을 갱신한다."""
        if not url.startswith(("http://", "https://")):
            return []
        sites = self._load()
        new = [k for k in self._at(sites, _norm(url)) if k not in shown]
        shown.update(new)
        return [(k, self._line(k, sites[k], url)) for k in new]

    def hint(self, site, text):
        """시작 때 줄 작업 (이름, ▸ 상세 + page): 요청과 같은 조작 계열의 조작 작업, 없으면 요청과 _grams(STOP 제외)가
        2개 이상 겹치는 읽기 작업이 하나뿐일 때 그것. 아니면 None. 사이트 이름·별칭은 지우고, 계열은 함·메뉴 이름도 지우고 본다."""
        live, text = _live(self._load()[site]), _drop(text, self.names(site))
        fam, grams = _family(BOX.sub(" ", text)), _grams(text) - STOP
        acts = [n for n, t in live.items() if fam and t["kind"] == "act" and _family(n) == fam]
        reads = [n for n, t in live.items() if t["kind"] == "read" and len(grams & _grams(n)) >= 2]
        items = [n for n, t in live.items() if t["kind"] == "read" and "click {대상}" in t.get("steps", [])]
        if re.search(r"\d+\s*번", text) and len(items) == 1:  # '인기글 5번째 글' — 목록 작업(인기글 조회)보다 글 읽기가 먼저
            reads = items
        n = acts[0] if acts else reads[0] if len(reads) == 1 else None
        return (n, _detail(site, n, live[n])) if n else None

    def arrived(self, url):
        """url이 걸린 사이트의 숨기지 않은 작업 진입 URL이면 [(사이트, 작업, ▸ 상세)] — page 없이 찾아와도 단계를 준다."""
        if not url.startswith(("http://", "https://")):
            return []
        sites = self._load()
        return [(k, n, _detail(k, n, t)) for k in self._at(sites, _norm(url))
                for n, t in _live(sites[k]).items() if same_page(t["url"], url)]

    def resolve(self, ref, fill=None):
        """navigate의 page '사이트/작업' 또는 '사이트'(홈) → (사이트, 작업 이름, 저장 URL, 이동할 URL, 상세 한 줄)
        또는 'error: …'. 이름이 딱 맞지 않으면 포함 관계로 하나, 아니면 _grams 겹침이 가장 큰 하나(동점이면 없음).
        사이트는 있는데 작업이 없으면 홈으로 가고 상세에 기억한 것(+다른 사이트의 길)을 적는다."""
        ref = str(ref).strip()
        sites = self._load()
        # 이름을 못 정해 주소로 남긴 사이트(cafe.naver.com/f-e/cafes/ID)도 page에 그대로 줄 수 있다
        name, _, key = ref.rpartition("/") if "/" in ref and not self._find(sites, ref) else (ref, "", "")
        # '검색{q}'처럼 자리 표기째, '페이지 인기글 조회'처럼 줄의 앞말(페이지·작업)째 줘도 찾는다
        key = re.sub(r"^(?:페이지|작업)\s+", "", re.sub(r"\{\w*\}", "", key).strip())
        site = self._find(sites, name.strip())
        if not site:  # 줄의 '이름(별칭)'을 그대로 page에 준 경우
            site = self._find(sites, re.sub(r"\s*[(（][^)）]*[)）]\s*$", "", name).strip())
        if not site:
            return f"error: 기억한 사이트 '{name}'가 없다 — page는 [사이트 메모리]의 '사이트/작업' 이름으로 줘라"
        e = sites[site]
        live = _live(e)
        if key and key not in live:  # '인기글' → '인기글 조회'
            fam = _family(key)  # 계열이 있으면 같은 계열만('글쓰기'가 '댓글쓰기'를 고르지 않게)
            cands = [n for n in live if not fam or _family(n) == fam]
            near = [n for n in cands if key in n or n in key]
            if len(near) != 1:
                score = {n: len(_grams(key) & _grams(n)) for n in cands}
                top = max(score.values(), default=0)
                near = [n for n, s in score.items() if s == top] if top else []
            key = near[0] if len(near) == 1 else key
        t = live.get(key)
        if not t:
            if not e["url"]:
                return f"error: {site}의 홈 URL을 모른다. 기억한 것: {'·'.join(_live(e)) or '없음'}"
            detail = ""
            if key:
                p = _pattern(sites, key, _kin(key), [site])
                detail = f'이 사이트에 "{key}" 기억 없음 — 기억한 것: {"·".join(_live(e)) or "없음"}' + (f"\n{p}" if p else "")
            return site, "", e["url"], e["url"], detail
        fill = fill if isinstance(fill, dict) else {}
        missing = [p for p in _params(t["url"]) if not str(fill.get(p, "")).strip()]
        if missing:
            return f"error: '{site}/{key}'는 템플릿이다 — fill에 {missing} 값을 줘라"
        target = t["url"]
        for p in _params(t["url"]):
            target = target.replace(f"{{{p}}}", quote(str(fill[p]).strip(), safe=""))
        return site, key, t["url"], target, _detail(site, key, t)

    def site_at(self, url):
        """url이 가장 구체적으로 맞는 기억 사이트 이름(없으면 None)."""
        if not url.startswith(("http://", "https://")):
            return None
        at = self._at(self._load(), _norm(url))
        return at[0] if len(at) == 1 else None

    def item(self, url):
        """url이 개별 글·항목이면 그 ID(맞는 사이트 자체 ID는 뺀다), 아니면 None."""
        site = self.site_at(url)
        return item_id(url, _keep(self._load()[site]) if site else "")

    @staticmethod
    def _line(name, e, cur=""):
        """걸린 사이트 한 줄: 작업·페이지 이름(현재 URL이면 '(지금 여기)') + 메모 최대 2줄."""
        here = lambda t: "(지금 여기)" if cur and same_page(t["url"], cur) else ""
        live = _live(e).items()
        acts = "·".join(k + here(t) for k, t in live if t["kind"] == "act")
        reads = "·".join(k + "".join(f"{{{p}}}" for p in _params(t["url"])) + here(t)
                         for k, t in live if t["kind"] != "act")
        body = " / ".join(x for x in (acts and f"작업 {acts}", reads and f"페이지 {reads}") if x)
        alias = f"({e['aliases'][0]})" if e["aliases"] else ""
        return "\n".join([f"■ {name}{alias}: {body or e['url'] or '(기억한 작업 없음)'}"]
                         + [f"  · {k}: {v}" for k, v in list(e["notes"].items())[:2]])

    # ---- 사이트 알아보기 ----
    @staticmethod
    def _patterns(e):
        """저장한 match + 대표 URL의 호스트/첫 경로(항상 포함 — url이 바뀌면 같이 바뀐다)."""
        auto = _default_match(e["url"]) if e["url"] else ""
        return e["match"] + ([auto] if auto and auto not in e["match"] else [])

    @classmethod
    def _on_site(cls, norm_url, e):
        """이 URL이 사이트 e의 페이지면 가장 길게 맞은 조각의 길이(구체성), 아니면 0.
        조각은 match와 기억한 작업 URL(쿼리·템플릿 앞까지)."""
        best = max((len(m) for m in cls._patterns(e) if cls._url_match(norm_url, m)), default=0)
        home = _host(_norm(e["url"])) if e["url"] else ""
        for t in e["tasks"].values():
            base = re.split(r"[?{]", _norm(t["url"]), 1)[0].rstrip("/")
            # 다른 호스트의 진입점은 이 사이트를 알아보는 데 쓰지 않는다 — 그러면
            # search.naver.com의 모든 페이지가 '네이버 블로그'로 인식된다
            if "/" in base and (not home or _host(base) == home) and cls._url_match(norm_url, base):
                best = max(best, len(base))
        return best

    @classmethod
    def _at(cls, sites, cur):
        """cur가 가장 구체적으로 맞는 사이트들. cafe.naver.com/…/12345678은 '네이버'(naver.com)가 아니라 예시카페다."""
        spec = {k: cls._on_site(cur, e) for k, e in sites.items()}
        best = max(spec.values(), default=0)
        return [k for k, v in spec.items() if best and v == best]

    @staticmethod
    def _url_match(norm_url, m):
        """m이 호스트면 호스트 일치(서브도메인 포함, m에 포트가 있으면 포트까지), 경로 조각이 있으면
        URL 안의 경로 경계 일치. 호스트+경로면 호스트가 같고 그 경로 조각이 경로 칸 경계에 있다(f-e/cafes/ID와
        ca-fe/cafes/ID 둘 다 cafe.naver.com/cafes/ID)."""
        if "/" not in m:
            host = norm_url.split("/", 1)[0].split("?", 1)[0]
            host = host if ":" in m else host.split(":", 1)[0]
            return host == m or host.endswith("." + m)
        head, _, rest = m.partition("/")
        if "." in head or ":" in head:
            host, _, path = norm_url.partition("/")
            host = host.split("?", 1)[0]
            return (host == head or host.endswith("." + head)) and Memory._url_match("/" + path, "/" + rest)
        i = norm_url.find(m)
        while i >= 0:
            end = i + len(m)
            if end == len(norm_url) or not norm_url[end].isalnum():
                return True
            i = norm_url.find(m, i + 1)
        return False

    @staticmethod
    def _mentions(text, name):
        n = name.lower().strip()
        if not n:
            return False
        if re.fullmatch(r"[a-z0-9]+", n):
            # 짧은 영문 별칭(EXC)이 단어 속에 우연히 섞인 경우는 제외
            return re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", text) is not None
        return n in text
