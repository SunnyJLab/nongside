import base64
import ast
import inspect
import os
import re
import threading
import time
from pathlib import PurePath
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

import llm
from memory import (BOX, FAMILIES, READ_WORDS, STOP, URLS, Memory, _default_match, _drop, _family, _grams, _norm,
                    TIME_KEYS, _title_segs, generic_name, host_name, same_page, site_home, title_name)

STEP_CAP = 25
SHOT_CAP = 3
VISION_CAP = 2000  # 비전 판독 텍스트는 스텁 치환 대상이 아니라 컨텍스트에 남으므로 길이 제한
REPEAT_WINDOW, REPEAT_MAX = 10, 3
BLOCK_CAP = 5  # 한 턴에 반복 차단이 이만큼 쌓이면 스텝을 더 태우지 않고 요약으로 마무리
CODE_FAIL_CAP = 2  # 한 턴에 run_code가 이만큼 실패하면 새 스크립트를 더 실행하지 않는다
TOOL_RESULT_CAP = 1500  # 이전 턴 도구 결과가 이보다 길면 앞부분만 남기고 자른다
TOOL_RESULT_KEEP = 500  # 자를 때 남기는 앞부분 — 통째로 버리면 모델이 같은 extract를 반복한다
TOOL_RESULT_STUB = "[시스템: 컨텍스트 절약을 위해 이전 도구 결과의 나머지를 제거함]"
ARCHIVE_MAX_CHARS = 200_000  # shortcut: full per-chat archive is capped; raise only if real investigations need more
RECALL_CHUNK_SIZE = 6_000

# 되돌리기 어려운 작업만 확인한다(그 외 클릭·입력·run_code는 바로 실행)
DANGER = re.compile(r"삭제|제거|비우기|발송|전송|보내기|등록|게시(?![판글물])|결제|결재|승인|제출"
                    r"|delete|remove|submit|publish|\bpay\b|\bsend\b", re.I)
# 휴리스틱: 라벨 키워드 휴리스틱. "검색어 삭제"(X 버튼) 같은 UI 정리 버튼을 걸러내지만
# "취소 후 삭제"류 진짜 파괴 동작도 통과시킬 수 있다. 오탐이 문제되면 요소 tag/role로 보강.
SAFE_UI = re.compile(r"검색어|지우기|초기화|내보내기|clear|닫기|close|취소", re.I)
# BOX(memory): 결재 메뉴·함 이름 — 이름 속 '결재'로 묻지 않게 지운 뒤 위험 단어를 본다. '대기함 삭제'는 묻는다


def _risky(text):
    """라벨·코드 문구가 확인할 위험 동작인가(_danger·_code_hits·run_code 클릭 직전 검사가 같이 쓴다)."""
    text = BOX.sub(" ", text)
    return bool(DANGER.search(text)) and not SAFE_UI.search(text)


# run_code는 코드를 파싱해 실제 조작 호출(click·submit 등)의 대상에만 위험 단어를 본다 —
# 주석·print 문구("삭제된 글 3건")나 읽기 전용 코드로는 묻지 않는다. 대상은 호출 인자와 로케이터 체인
# (page.get_by_role("button", name="삭제").click()), 변수에 담은 로케이터, el(인덱스)의 관찰 라벨이다.
CODE_ACTION = re.compile(r"\.(click|dblclick|tap|press|dispatch_event|submit|evaluate)\s*\(")  # 파싱 실패 시 폴백
ACT_CALLS = {"click", "dblclick", "tap", "press", "dispatch_event", "submit"}
JS_CALLS = {"evaluate", "evaluate_all", "evaluate_handle", "eval_on_selector", "eval_on_selector_all"}
# JS 안의 실제 이벤트/요청: el.click(), form.submit(), requestSubmit, dispatchEvent, fetch/XHR
JS_ACTION = re.compile(r"\.(click|submit|requestSubmit|dispatchEvent)\s*\(|\bfetch\s*\(|XMLHttpRequest|sendBeacon")
JS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\\])//[^\n]*", re.S)
# 페이지를 바꾸는 run_code(입력·클릭·제출) — 단계로 못 적는다. 이것이 없는 run_code는 읽기라 단계에서만 뺀다
CODE_WRITE = re.compile(r"\.(click|dblclick|tap|fill|type|press|check|uncheck|select_option|set_input_files|submit)"
                        r"\s*\(")
FORM_WRITE = re.compile(r"\.(fill|type|select_option|set_input_files|submit|press)\s*\(")  # 폼에 값을 넣는 run_code = 조작
# run_code를 단계로 옮길 때: 조작 호출 → 단계 도구, 로케이터, 무시해도 되는 대기·읽기 호출
STEP_CALLS = {"click": "click", "fill": "type", "select_option": "select"}
FIND_CALLS = {"get_by_role", "get_by_placeholder", "get_by_label", "get_by_text"}
SKIP_CALLS = {"wait_for_load_state", "wait_for_timeout", "wait_for"}
READ_CALLS = {"input_value", "inner_text", "text_content", "count", "is_visible"}


# 사용자가 챗 창에서 현재 탭을 뺐을 때 첫 메시지에 붙는 지시 (llm.SYSTEM 규칙 17과 짝)
BG_HEADER = ("[작업 위치: 백그라운드] 사용자가 현재 탭을 쓰지 않기로 했다. 사용자 탭으로 switch_tab하지 말고 "
             "사용자 탭 내용을 답에 쓰지 마라. 필요한 페이지는 navigate로 열어라(보이지 않는 백그라운드에서 열린다).")


def _tool(name, description, props, required):
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
        },
    }


class History(list):
    """Conversation messages plus research retained across turns until clear()."""

    def __init__(self):
        super().__init__()
        self.archive = {}
        self.archive_chars = 0
        self.archive_full = False
        self.archive_warnings = []

    def clear(self):
        super().clear()
        self.archive.clear()
        self.archive_chars = 0
        self.archive_full = False
        self.archive_warnings.clear()

    def retain(self, tool, url, text, secrets):
        ordered_secrets = sorted(secrets, key=len, reverse=True)
        text = _mask(text, ordered_secrets).strip()
        if not text:
            return None
        if self.archive_full:
            return False
        if self.archive_chars + len(text) > ARCHIVE_MAX_CHARS:
            self.archive_full = True
            return None
        eid = f"R{len(self.archive) + 1:04d}"
        self.archive[eid] = {"id": eid, "tool": tool, "url": _mask(url or "", ordered_secrets), "text": text}
        self.archive_chars += len(text)
        return eid

    def recall(self, args, secrets):
        try:
            offset = args.get("offset", 0)
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError
        except (TypeError, ValueError):
            return "error: offset는 0 이상의 정수여야 합니다"
        source = args.get("source")
        if "source" in args:
            if not isinstance(source, str):
                return "error: source는 자료 ID 문자열이어야 합니다"
            entry = self.archive.get(source)
            if entry is None:
                return f"error: 보관된 자료 ID가 없습니다: {source}"
            text = _mask_same_length(entry["text"], secrets)
            if offset == len(text) and text:
                return "자료 끝: 더 이상 읽을 내용이 없습니다"
            if offset > len(text):
                return f"error: offset가 자료 길이({len(text)})를 넘었습니다"
            end = min(offset + RECALL_CHUNK_SIZE, len(text))
            body = (f"자료 {entry['id']} ({entry['tool']}, {_mask_same_length(entry['url'], secrets)})\n"
                    f"{text[offset:end]}")
            if end < len(text):
                body += f"\n다음 offset: {end}"
            return body
        entries = list(self.archive.values())
        listing = "\n".join(f"{e['id']} | {e['tool']} | {_mask_same_length(e['url'], secrets)} | {len(e['text'])}자"
                             for e in entries)
        if offset == len(listing) and listing:
            return "보관 목록 끝: 더 이상 자료가 없습니다"
        if offset > len(listing):
            return f"error: offset가 목록 길이({len(listing)})를 넘었습니다"
        end = min(offset + RECALL_CHUNK_SIZE, len(listing))
        body = listing[offset:end] or "보관된 조사 자료가 없습니다"
        if end < len(listing):
            body += f"\n다음 offset: {end}"
        if self.archive_warnings:
            body += "\n" + "\n".join(self.archive_warnings)
        return body


def _retain_research(messages, tool, url, text, secrets, log):
    if not isinstance(messages, History) or not text or text.startswith("error:"):
        return
    eid = messages.retain(tool, url, text, secrets)
    if eid is False:
        return
    if eid is None and text.strip():
        warning = f"[시스템: 자료가 조사 보관 용량({ARCHIVE_MAX_CHARS}자)을 초과해 보관되지 않았습니다. 이후 자료도 보관되지 않습니다.]"
        messages.archive_warnings.append(warning)
        log.brief("조사 보관 한도 도달 — 이후 자료는 유지되지 않음")
        return warning
    return eid


TOOLS = [
    _tool(
        "navigate",
        "URL 또는 [사이트 메모리]의 기억한 페이지로 이동한다. 사용자 탭에서 부르면 사용자 화면은 그대로 두고 "
        "보이지 않는 백그라운드 탭에서 연다.",
        {"url": {"type": "string", "description": "이동할 URL (page를 주면 생략)"},
         "page": {"type": "string",
                  "description": "[사이트 메모리]에 보이는 '사이트 이름/작업 이름'(또는 '사이트 이름' = 홈). "
                                 "기억에 있는 곳은 URL 대신 이걸 써라. 기억에 없는 이름을 지어내지 마라"},
         "fill": {"type": "object", "additionalProperties": {"type": "string"},
                  "description": "작업 이름에 자리가 있으면 그 값 (예: 검색{q} → {\"q\": \"테슬라\"})"},
         "visible": {"type": "boolean",
                     "description": "사용자 화면(사용자 탭)에서 연다. 사용자가 보여달라고 했거나 로그인·캡차처럼 "
                                    "사용자가 직접 봐야 할 때만 true"}},
        [],
    ),
    _tool(
        "new_tab",
        "새 탭을 열어 URL로 이동한다. 이후 그 탭이 세션 탭이 된다. 기본은 보이지 않는 백그라운드 탭이다.",
        {"url": {"type": "string", "description": "열 URL"},
         "visible": {"type": "boolean", "description": "사용자 브라우저에 보이는 탭으로 연다"}},
        ["url"],
    ),
    _tool(
        "switch_tab",
        "TABS 목록의 [t i] 탭으로 전환한다. 이전 탭은 닫히지 않고 유지된다.",
        {"index": {"type": "integer", "description": "탭 인덱스"}},
        ["index"],
    ),
    _tool(
        "click",
        "요소 목록의 [i] 인덱스 요소를 클릭한다.",
        {"index": {"type": "integer", "description": "요소 인덱스"}},
        ["index"],
    ),
    _tool(
        "type",
        "인덱스 요소에 텍스트를 입력한다. 실제 키 입력이므로 입력 후 자동완성 후보가 "
        "관찰 목록에 나타날 수 있다 — 그러면 후보를 click하거나 press(ArrowDown)+press(Enter)로 "
        "확정해라. select 에는 라벨에 표시된 {옵션} 중 하나를 text로 준다. "
        "메일·게시판 본문 같은 에디터(contenteditable, iframe, @f 표시 요소)도 type으로 입력된다 — "
        "에디터 입력에 run_code를 쓰지 마라.",
        {
            "index": {"type": "integer", "description": "요소 인덱스"},
            "text": {"type": "string", "description": "입력할 텍스트"},
            "submit": {"type": "boolean",
                       "description": "입력 후 Enter로 제출 (자동완성 후보를 골라야 하면 쓰지 마라)"},
        },
        ["index", "text"],
    ),
    _tool(
        "attach",
        "로컬 파일을 첨부한다. 페이지의 input[type=file]에 파일을 직접 설정하므로 "
        "첨부 버튼 클릭 없이 호출 가능. 파일 입력이 여러 개면 index로 고른다.",
        {
            "path": {"type": "string", "description": "첨부할 파일의 로컬 경로"},
            "index": {"type": "integer", "description": "파일 입력 번호 (기본 0)"},
        },
        ["path"],
    ),
    _tool(
        "scroll",
        "페이지를 위/아래로 스크롤한다. index를 주면 그 요소가 보이도록 스크롤한다(direction 무시).",
        {
            "direction": {"type": "string", "enum": ["up", "down"]},
            "index": {"type": "integer", "description": "보이게 할 요소 인덱스 (선택)"},
        },
        [],
    ),
    _tool(
        "press",
        "키보드 키를 하나 누른다 (Enter, Tab, Escape, Backspace 등 Playwright 키 이름).",
        {"key": {"type": "string", "description": "키 이름"}},
        ["key"],
    ),
    _tool("back", "브라우저 뒤로가기. 잘못 들어간 페이지에서 빠져나올 때 쓴다.", {}, []),
    _tool(
        "wait",
        "페이지가 갱신될 때까지 기다린다. 클릭했는데 화면이 그대로면 이걸 쓰고 다시 관찰해라.",
        {
            "seconds": {"type": "number", "description": "대기 초 (기본 2, 최대 10)"},
            "text": {"type": "string", "description": "이 텍스트가 나타날 때까지 대기 (최대 10초)"},
        },
        [],
    ),
    _tool(
        "find",
        "요소 목록에서 텍스트로 요소를 찾는다. 목록이 길거나 라벨이 잘렸을 때 인덱스를 확실히 알아낸다.",
        {"text": {"type": "string", "description": "찾을 텍스트 (라벨 부분 일치, 대소문자 무시)"}},
        ["text"],
    ),
    _tool(
        "hover",
        "요소 위에 마우스를 올린다. 관찰 목록에서 +팝업 / +숨은메뉴 마커가 붙은 요소는 "
        "hover해야 하위 항목이 나타난다. hover 후 관찰 목록을 다시 확인해라.",
        {"index": {"type": "integer", "description": "요소 인덱스"}},
        ["index"],
    ),
    _tool(
        "screenshot",
        "**최후 수단.** extract/find/관찰 목록으로 알 수 없는 화면(지도/차트/캔버스/이미지)에서만 "
        "쓴다. 텍스트가 있는 페이지에서는 extract를 써라.",
        {"question": {"type": "string",
                      "description": "이 화면에서 무엇을 확인하려는지 적으면 그 질문에 맞춰 화면을 읽어준다 (선택)"}},
        [],
    ),
    _tool(
        "extract",
        "현재 페이지 본문 텍스트를 읽는다. 길면 잘림 표시와 함께 이어 읽을 offset을 알려준다.",
        {"offset": {"type": "integer", "description": "이어 읽을 시작 위치(문자). 기본 0"}},
        [],
    ),
    _tool(
        "recall",
        "이 대화에서 성공적으로 조사한 extract/run_code/화면 판독 자료를 보관 목록 또는 자료 ID로 다시 읽는다. "
        "먼저 source 없이 호출해 ID를 찾고, 긴 자료에는 반환된 offset을 그대로 줘서 이어 읽는다.",
        {"source": {"type": "string", "description": "자료 ID (생략하면 보관 목록)"},
         "offset": {"type": "integer", "description": "0 이상의 문자 위치 (목록 또는 자료 안에서 이어 읽기)"}},
        [],
    ),
    _tool(
        "run_code",
        "playwright Python 코드를 현재 탭에서 실행한다. 네임스페이스: page, ctx, browser, json, re, time, el "
        "(import는 차단됨 — 제공되는 것만 써라). 동기(sync) API다 — await/async를 쓰지 마라. "
        "print() 출력과 result 변수 값을 반환한다. "
        "페이지 순회·반복 수집·연속 폼 입력처럼 절차가 정해진 다중 동작을 한 번에 처리할 때 쓴다. "
        "짧고 단순하게 써라: (1) 관찰 목록에 있는 요소는 el(인덱스)로 바로 잡는다(예: el(12).click()) — "
        "셀렉터를 추측하지 마라. (2) 목록에 없으면 page.get_by_role('button', name='다음'), get_by_text, "
        "get_by_label, get_by_placeholder 순으로 쓰고, CSS는 extract·관찰로 확인한 구조에만 쓴다. "
        "(3) 반복 수집은 루프 대신 page.locator('css').all_inner_texts() 또는 "
        "page.eval_on_selector_all('css', 'els => els.map(e => e.innerText)') 한 번으로 읽는다. "
        "(4) locator 동작은 자동으로 기다리니 time.sleep을 넣지 말고, 필요하면 "
        "page.wait_for_load_state() 나 locator.wait_for()를 쓴다. (5) 여러 개가 걸리면 .first나 .nth(i)로 "
        "하나를 고른다. (6) try/except로 오류를 삼키지 마라 — 실패 원인이 보여야 고칠 수 있다.",
        {"code": {"type": "string", "description": "실행할 playwright Python 코드"}},
        ["code"],
    ),
    _tool(
        "done",
        "작업을 완료하고 결과를 요약한다. 조회·조작을 해냈으면 op에 작업 이름을 줘라 — 에이전트가 실제로 거친 "
        "경로를 기억해 다음에 바로 처리한다.",
        {
            "summary": {"type": "string", "description": "결과 요약. 마크다운(제목·목록·표·굵게·링크·코드) 사용 가능"},
            "op": {"type": "string",
                   "description": "이번에 해낸 조회·조작의 일반 작업 이름(게시글 읽기·댓글 작성·메일 작성·안읽은 메일 확인·"
                                  "인기글 조회 등 짧게). 다음에 다른 글·메일에도 쓰는 이름이다 — 글 번호·제목·사람 이름·"
                                  "사이트 이름은 넣지 마라('9번글 요약' 말고 '게시글 읽기'). 주면 에이전트가 실제로 거친 "
                                  "경로를 기억한다. 해당 없으면 생략"},
            "called": {"type": "string",
                       "description": "사용자가 요청에서 이 사이트를 부른 말이 [사이트 메모리]의 이름·별칭과 다르면 "
                                      "요청에 쓴 그대로(예: 기억 '업무 메일', 요청 '그룹웨어 메일에서…' → 그룹웨어 메일). "
                                      "이름 없이 요청했거나 기억한 이름으로 불렀으면 생략"},
            "remember": {
                "type": "array",
                "description": "처음 간 사이트의 이름·별칭·대표 URL(세션을 넘어 유지). URL·절차·조회 결과·개인정보는 "
                               "담지 마라. 기억에 이미 있으면 생략.",
                "items": {
                    "type": "object",
                    "properties": {
                        "site": {"type": "string",
                                 "description": "사용자가 부르는 사이트 이름 (예: 예시카페). 말하지 않았으면 페이지에 보이는 "
                                                "사이트 이름 — 도메인·IP와 일반 명사(메일·카페), 페이지·경기·공연 제목은 "
                                                "이름이 아니다. 브랜드까지(네이버 메일, Gmail, 인터파크 티켓)"},
                        "url": {"type": "string",
                                "description": "그 사이트 자체의 홈 URL (예: 카페 홈, blog.naver.com)"},
                        "aliases": {"type": "array", "items": {"type": "string"},
                                    "description": "다른 호칭 (정식 명칭, 카페 ID 등)"},
                        "match": {"type": "array", "items": {"type": "string"},
                                  "description": "이 사이트 페이지임을 알아보는 URL 조각. 호스트를 여러 사이트가 "
                                                 "공유하면 ID까지 (예: 'cafes/12345678'). 생략하면 url에서 정한다"},
                        "notes": {"type": "object", "additionalProperties": {"type": "string"},
                                  "description": "주제 → 사이트 제약 한 줄(로그인 필요·차단 등). URL·절차·조회 결과·요청에 "
                                                 "나온 값(역·날짜·상품·팀)은 쓰지 마라. 작업 이름을 키로 쓰지 마라(작업은 "
                                                 "에이전트가 기억한다). 빈 값이면 삭제. 예: {\"로그인\": \"글쓰기는 로그인 필요\"}"},
                    },
                    "required": ["site"],
                },
            },
        },
        ["summary"],
    ),
    _tool(
        "ask_user",
        "판단이 어려울 때 사용자에게 묻는다. 입력받을 정보가 여러 개면 "
        "fields에 항목을 나열해 하나씩 묻는다 (예: 로그인 폼의 id/pw).",
        {
            "question": {"type": "string", "description": "질문 또는 안내 문구"},
            "options": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                        "description": "고를 수 있는 답 후보 (선택). 사용자는 직접 입력하거나 취소할 수도 있다"},
            "fields": {
                "type": "array",
                "description": "항목별로 입력받을 필드 목록 (선택)",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "입력 항목 이름"},
                        "secret": {"type": "boolean",
                                   "description": "비밀번호 등 화면에 가려서 입력받을지"},
                    },
                    "required": ["name"],
                },
            },
        },
        ["question"],
    ),
]


def _result(tool_use_id, content, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": str(content)}
    if is_error:
        block["is_error"] = True
    return block


def _label_for(observation, index):
    """직전 관찰 결과에서 [index] 요소의 라벨을 뽑는다. 줄 형식(browser.py fmt):
    '[i] <tag[:type]> "라벨"[ +힌트][ {옵션}][ @f프레임]' — 꼬리를 못 읽으면 라벨이 비어 위험 확인이 빠진다."""
    m = re.search(rf'^\[{re.escape(str(index))}\] <[^>]+> "(.*)"(?: \+\S+)?(?: \{{.*\}})?(?: @f\d+)?$',
                  observation, re.M)
    return m.group(1) if m else ""


def _el_label(observation, index):
    """관찰 [index]의 (라벨, 옵션 칸인가). select 라벨 끝의 옵션 이름("말머리 선택 잡담 질문" {*말머리 선택|잡담|질문})은 뗀다."""
    label = _label_for(observation, index)
    m = re.search(rf'^\[{re.escape(str(index))}\] <[^>]+> ".*"(?: \+\S+)? \{{(.*)\}}', observation, re.M)
    for o in reversed(m.group(1).split("|") if m else []):
        if label.endswith(" " + o.lstrip("*")):
            label = label[:-len(o.lstrip("*")) - 1]
    return label, bool(m)


def _code_steps(code, observation):
    """단순한 조작만 있는 run_code를 단계 [(도구, 라벨, 위험, 값)]로 옮긴다. 옮길 수 없는 줄이 하나라도 있으면 None.
    조작: el(N)·page.get_by_role(…, name=…)[.first]·get_by_placeholder·get_by_label·get_by_text의 click/fill/select_option.
    무시: 주석·print·대기(wait_for_load_state·wait_for_timeout·wait_for)·값만 읽는 대입(input_value 등)."""
    try:
        body = ast.parse(code).body
    except SyntaxError:
        return None
    out = []
    for s in body:
        c = getattr(s, "value", None)
        if not (isinstance(s, (ast.Expr, ast.Assign)) and isinstance(c, ast.Call)):
            return None
        f = c.func
        attr = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
        if attr in (READ_CALLS if isinstance(s, ast.Assign) else SKIP_CALLS | {"print"}):
            if CODE_WRITE.search(ast.unparse(s)):  # print(btn.click()) 같은 숨은 조작
                return None
            continue
        args = [a.value for a in c.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if (isinstance(s, ast.Assign) or not isinstance(f, ast.Attribute) or attr not in STEP_CALLS
                or not len(args) == len(c.args) == (attr != "click")):  # click() / fill('값') / select_option('값')
            return None
        loc = f.value.value if isinstance(f.value, ast.Attribute) and f.value.attr == "first" else f.value
        if not isinstance(loc, ast.Call):
            return None
        lf, kw = loc.func, {k.arg: k.value for k in loc.keywords}
        key = kw.get("name") if isinstance(lf, ast.Attribute) and lf.attr == "get_by_role" else (loc.args or [None])[0]
        if isinstance(lf, ast.Name) and lf.id == "el" and isinstance(key, ast.Constant) and isinstance(key.value, int):
            label = _el_label(observation, key.value)[0]
        elif (isinstance(lf, ast.Attribute) and lf.attr in FIND_CALLS and isinstance(lf.value, ast.Name)
              and lf.value.id == "page" and isinstance(key, ast.Constant) and isinstance(key.value, str)):
            label = key.value
        else:
            return None
        out.append((STEP_CALLS[attr], label, attr == "click" and _risky(label), "".join(args)))
    return out


def _page_key(url):
    """URL에서 쿼리/프래그먼트를 뺀 페이지 키."""
    return re.split(r"[?#]", url, 1)[0]


def _url_key(observation):
    """관찰 첫머리의 "URL: " 줄에서 페이지 키를 뽑는다.
    같은 페이지의 팬/줌·SPA 라우팅으로 ? 뒤나 # 뒤만 바뀌는 경우를 같은 키로 묶는다."""
    m = re.search(r"^URL: (\S+)", observation, re.M)
    return _page_key(m.group(1)) if m else ""


def _mask_args(name, args, observation, secrets):
    """스텝 로그 출력용 args — 비밀값은 ****로 가린다."""
    shown = dict(args)
    if name == "type":
        m = re.search(
            rf'^\[{re.escape(str(args.get("index")))}\] <([^>]+)>',
            observation, re.M)
        if m and "password" in m.group(1):
            shown["text"] = "****"
    for k, v in shown.items():
        if isinstance(v, str):
            for s in secrets:
                if s and s in v:
                    shown[k] = v.replace(s, "****")
    return shown


class _NO:
    """ask_user를 취소한 뒤의 확인 요청은 사용자에게 띄우지 않고 바로 거절한다."""
    @staticmethod
    def confirm(_q):
        return "n"
    ask = confirm


def _code_hits(code, observation=""):
    """run_code에서 실제 조작 호출의 대상에 든 위험 단어(등장 순서). 없으면 []."""
    try:
        tree = ast.parse(code)
    except SyntaxError:  # 실행도 안 되겠지만, 판단할 수 없으면 예전처럼 보수적으로
        return DANGER.findall(code) if CODE_ACTION.search(code) else []
    names = {}  # 변수 -> 대입된 식들 (btn = page.get_by_text("삭제"); btn.click())

    def bind(target, value):
        for t in ast.walk(target):
            if isinstance(t, ast.Name):
                names.setdefault(t.id, []).append(value)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                bind(t, node.value)
        elif isinstance(node, (ast.For, ast.comprehension)):
            bind(node.target, node.iter)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            bind(node.optional_vars, node.context_expr)

    def texts(expr, seen):
        out = []
        for n in ast.walk(expr):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.append(n.value)
            elif isinstance(n, ast.Name) and n.id in names and n.id not in seen:
                for v in names[n.id]:
                    out += texts(v, seen | {n.id})
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "el"
                  and n.args and isinstance(n.args[0], ast.Constant)):
                out.append(_label_for(observation, n.args[0].value))  # 관찰 목록 인덱스의 라벨
        return out

    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        parts = texts(node, frozenset())  # 호출 인자 + 로케이터 체인(receiver)
        if attr in JS_CALLS:
            js = [JS_COMMENT.sub("", s) for s in parts]
            if not any(JS_ACTION.search(s) for s in js):
                continue  # 읽기만 하는 evaluate
            parts = js
        elif attr in ("delete", "post", "put", "patch", "fetch") and \
                isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "request":
            if attr == "delete":
                hits.append("delete")
        elif attr not in ACT_CALLS:
            continue
        text = " ".join(parts)
        if _risky(text):
            hits += DANGER.findall(BOX.sub(" ", text))
    return hits


def _danger(name, args, observation):
    """확인이 필요한 동작의 키 — 클릭은 라벨(인덱스는 재관찰 때 바뀐다), run_code는 위험 단어들. 안전하면 None."""
    if name == "run_code":
        hits = _code_hits(str(args.get("code", "")), observation)
        return ("run_code", frozenset(h.lower() for h in hits)) if hits else None
    if name != "click":  # 입력은 되돌릴 수 있다. 발송·등록은 버튼 클릭에서 묻는다
        return None
    label = _label_for(observation, args.get("index", ""))
    return ("click", label) if _risky(label) else None


def _confirm(name, args, observation, io):
    if not _danger(name, args, observation):  # 일반 클릭·입력·코드는 바로 실행
        return True
    ask = getattr(io, "confirm", None) or io.ask
    if name == "run_code":
        # 실제로 걸린 위험 단어를 보여준다(등장 순서, 대소문자 무시 중복 제거)
        hits = list(dict.fromkeys(h.lower() for h in _code_hits(str(args.get("code", "")), observation)))
        words = ", ".join(f"'{h}'" for h in hits[:5]) + (" 등" if len(hits) > 5 else "")
        return ask(f"아래 코드에 {words} 동작이 들어 있어요.\n"
                   f"```python\n{str(args.get('code', ''))[:500]}\n```\n실행할까요? [y/N]").strip().lower() == "y"
    label = _label_for(observation, args.get("index", ""))
    return ask(f"'{label}' 요소를 {name} 할까요? [y/N]").strip().lower() == "y"


def _fail_brief(out):
    """사용자용 짧은 실패 사유: 첫 줄의 첫 문장만, [인덱스]·모델용 안내는 뺀다."""
    line = (out.removeprefix("error:").strip().splitlines() or ["알 수 없음"])[0]
    line = re.split(r" - |[.;]\s|\s\(", re.sub(r"^\[\d+\]\s*", "", line))[0]
    return f"실패 — {_clip(line, 50)}"


# 지난 턴의 메모리 블록은 고치지 않는다: 턴 시작마다 고치면 그 뒤 대화 기록 캐시가 통째로 깨진다.
# 대신 같은 대화에서 이미 보낸 줄과 똑같은 줄은 다시 보내지 않는다(_mem_block).
MEMORY_HEADER = "[사이트 메모리]"
LOGIN = re.compile(r"로그인|login|비밀번호|password", re.I)  # ask_user로 로그인을 부탁한 요청은 기록하지 않는다


LOG_LEVELS = ("quiet", "brief", "debug")


def log_level():
    """AGENT_LOG: quiet(결과만) | brief(기본, 동작당 한 줄) | debug(도구 인자·결과·게이트 전부).
    오타를 조용히 brief로 바꾸면 디버그하려던 사람이 로그가 왜 안 나오는지 모른다 — 거부한다."""
    lv = (os.environ.get("AGENT_LOG") or "brief").strip().lower()
    if lv not in LOG_LEVELS:
        raise ValueError(f"AGENT_LOG={lv!r} — {'/'.join(LOG_LEVELS)} 중 하나여야 한다")
    return lv


class _Log:
    """진행 표시. debug 줄은 debug에서만, brief 줄은 brief에서만 나간다(debug가 이미 더 자세하다)."""

    def __init__(self, io):
        self.io, self.level, self._last = io, log_level(), None

    def debug(self, text):
        if self.level == "debug":
            self.io.say(text)

    def brief(self, text):
        # 연달아 같은 줄(연속 extract 등)은 한 번만
        if self.level == "brief" and text and text != self._last:
            self._last = text
            self.io.say(f"· {text}")


def _clip(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _short_url(url):
    return _clip(re.sub(r"^https?://(www\.)?", "", url), 60)


def _brief(name, args, observation):
    """사용자용 한 줄. 화면이 안 바뀌거나 내부 절차인 동작(scroll/wait/find/이어 읽기)은 None.
    args는 비밀값이 가려진 것을 받는다."""
    if name in ("navigate", "new_tab"):
        return f"{'이동' if name == 'navigate' else '새 탭'} {_short_url(args.get('url', ''))}"
    if name in ("click", "hover"):
        label = _label_for(observation, args.get("index", "")) or f"#{args.get('index')}"
        return f"{'클릭' if name == 'click' else '펼치기'} \"{_clip(label, 40)}\""
    if name == "type":
        return f"입력 \"{_clip(args.get('text', ''), 30)}\""
    if name == "press":
        return f"키 {args.get('key', '')}"
    if name == "attach":
        return f"파일 첨부 {PurePath(str(args.get('path', ''))).name}"
    if name == "extract" and not args.get("offset"):
        return "페이지 읽기"
    return {"back": "뒤로", "switch_tab": "탭 전환", "screenshot": "화면 확인",
            # 첫 줄 뒤의 코드는 웹 UI가 접힌 코드 블록으로 보인다
            "run_code": "스크립트 실행\n" + _clip_lines(str(args.get("code", "")), 3000)}.get(name)


def _clip_lines(text, n):
    return text if len(text) <= n else text[:n] + "\n…"


def _sent_lines(messages):
    """이 대화에서 이미 보낸 [사이트 메모리] 줄들."""
    return {line for m in messages if isinstance(m.get("content"), list) for b in m["content"]
            if b.get("type") == "text" and b["text"].startswith(MEMORY_HEADER) for line in b["text"].split("\n")}


def _mem_block(lines, sent):
    """같은 대화에서 이미 보낸 줄을 뺀 [사이트 메모리] 블록(남는 줄이 없으면 None). 보낸 줄은 sent에 더한다.
    메모 줄('  · ')은 중복을 따지지 않고 바로 위 사이트 줄을 따라간다 — 다른 사이트의 같은 메모가 빠지지 않게."""
    new, head = [], False
    for x in lines:
        if x.startswith("  · "):
            new += [x] * head
        elif x:
            head = x not in sent and x not in new
            new += [x] * head
    sent.update(new)
    return f"{MEMORY_HEADER}\n" + "\n".join(new) if new else None


def _save_memory(mem, entries, secrets, log, step, here="", op=""):
    """done.remember 항목들(사이트 이름·별칭·홈·match·메모)을 저장한다. 첫 오류 문자열을 반환(없으면 None).
    항목별로 한 번에 반영되므로 앞 항목이 저장된 뒤 뒷 항목에서 멈출 수 있다. 검증에 걸린 메모는
    memory가 그 항목만 버리고 결과에 적는다 — 로그로만 남기고 모델에게 돌려보내지 않는다.
    here(지금 주소)가 ID로 갈리는 사이트(카페)인데 모델이 그보다 넓은 주소(도메인·앞 칸만)를 주면 지금 카페 홈으로.
    다른 카페 주소(다른 ID)는 그대로 둔다."""
    home = site_home(here) if here else None
    op = _op_name(str(op or "").strip())

    def masked(v):
        if isinstance(v, dict):
            return {str(k): _mask(str(x), secrets) for k, x in v.items()}
        if isinstance(v, list):
            return [_mask(str(x), secrets) for x in v]
        return v if v is None else _mask(str(v), secrets)

    if isinstance(entries, dict):  # 사이트 하나를 배열 없이 준 경우 — 뜻이 분명하니 왕복시키지 않는다
        entries = [entries]
    if not isinstance(entries, list):
        return "error: remember는 사이트 객체의 배열이어야 한다"
    for e in entries:
        if not isinstance(e, dict):
            return "error: remember 항목은 {site, url, aliases, match, notes} 객체여야 한다"
        url = masked(e.get("url"))
        if home and url and (_norm(home).rstrip("/") + "/").startswith(_norm(url).rstrip("/") + "/"):
            log.debug(f"[step {step}]   -> [memory] 대표 URL {url} -> {home} (지금 사이트 홈)")
            url = home
        try:
            notes = masked(e.get("notes") or {})
            # 작업 이름을 키로 단 메모(열차 조회: <검색 구간>)는 그 작업의 요청 값·조회 결과다 — 작업은 tasks가 기억한다
            skip = {op, *mem.tasks(mem._find(mem._load(), str(e.get("site") or "")) or "")} - {""}
            if isinstance(notes, dict) and skip & set(notes):
                log.debug(f"[step {step}]   -> [memory] 작업 이름 메모 버림: {sorted(skip & set(notes))}")
                notes = {k: v for k, v in notes.items() if k not in skip}
            out = mem.remember(
                masked(e.get("site")), url=url,
                aliases=masked(e.get("aliases") or []), match=masked(e.get("match") or []),
                notes=notes)
        except OSError as ex:
            out = f"error: 메모리 저장 실패 ({ex})"
        log.debug(f"[step {step}]   -> [memory] {out}")
        if out.startswith("error:"):
            return out
        log.brief(out.split(" (", 1)[0])  # "기억함: 예시카페"
    return None


def _name_site(browser, io, url, log):
    """remember 없이 처음 간 사이트의 이름: 제목(홈 제목과 함께 있는 조각이 하나) → 메타 사이트 이름 → 사용자에게 한 번.
    호스트(IP·도메인)는 사용자가 부르는 이름이 아니다. 답을 안 하면(비우기·취소·비대화형) 호스트 이름으로 남긴다."""
    home = site_home(url)
    host = _default_match(home) if home else host_name(url)  # 카페는 카페 ID까지 — 호스트만으론 카페를 못 가른다
    get = getattr(browser, "site_names", None)
    title, home, meta = get(url) if get else ("", "", "")
    meta = meta.strip() if 2 <= len(meta.strip()) <= 30 and "@" not in meta else ""
    meta = "" if generic_name(meta) else meta
    name = title_name(title, home) or ""
    if generic_name(name):  # '메일' 하나로는 네이버 메일·Gmail·업무 메일이 섞인다 — 메타 이름(브랜드)을 붙이거나 묻는다
        name = "" if not meta else meta if name.lower() in meta.lower() else f"{meta} {name}"
    name = name or meta
    log.debug(f"[memory] 사이트 이름 후보: 제목 {title!r} / 홈 {home!r} / 메타 {meta!r} -> {name!r}")
    if name:
        return name
    opts = list(dict.fromkeys(_title_segs(title)))[:4]
    q = (f"처음 쓴 사이트({host})를 뭐라고 기억할까요? 다음에 그 이름으로 부르면 바로 찾아갑니다. "
         f"비워 두면 '{host}'로 기억합니다.")
    ans = str((io.ask(q, opts) if opts else io.ask(q)) or "").strip()
    log.debug(f"[memory] 사이트 이름 물음 -> {ans!r}")
    return ans if 2 <= len(ans) <= 30 and not ans.startswith(("[", "(")) else host


def _alias_hint(mem, task, site, rec):
    """요청이 이름·별칭·URL로 부르지 않은 기억 사이트에서 일하게 됐다 — 사용자가 다른 이름으로 불렀을 수 있다.
    그 이름을 별칭으로 남기라는 한 줄(요청마다 사이트당 한 번). 이름 없이 한 요청과는 코드가 구분하지 못해 모델에게 맡긴다."""
    if site in rec["hinted"] or mem.named_in(site, task):
        return []
    rec["hinted"].add(site)
    return [f"※ 요청에 '{site}'(기억한 이름·별칭)가 없다 — 사용자가 이 사이트를 다른 이름으로 불렀으면 done의 called에 "
            "그 말을 그대로 줘라. 이름 없이 요청했으면 생략."]


def _save_called(mem, rec, args, url, secrets, log):
    """done.called(사용자가 요청에서 사이트를 부른 말)를 일한 기억 사이트의 별칭으로 남긴다.
    요청 문구에 그대로 있는 말만 — 모델이 지어낸 호칭(메뉴 이름 등)은 들어가지 않는다."""
    called = _mask(str(args.get("called") or "").strip(), secrets)
    site = mem.site_at(url) or next((s for s, _ in reversed(rec["refs"])), None)
    if site and called and called.lower() in " ".join(rec["said"]).lower() and not mem.named_in(site, called):
        log.debug(f"[memory] 별칭 {called!r}: {mem.remember(site, aliases=[called])}")


def _plain(label, item=True):
    """기억할 클릭 라벨: 바뀌는 날짜·개수는 지운다(안 읽은 메일 130 개 → 안 읽은 메일). item이면 글·메일 제목 같은 긴 문장은
    {대상} — 지엽적인 글 하나를 기억하지 않는다(path는 메뉴를 거쳐 온 길이라 긴 메뉴·카페 이름도 둔다). 남는 게 없거나 on/off면 ''."""
    # 휴리스틱: 길이·띄어쓰기 휴리스틱 — 짧은 글 제목은 못 잡고, 12자 넘는 긴 메뉴 이름은 {대상}이 된다
    s = " ".join(re.sub(r"[(\[]?\d[\d,.:/-]*[)\]]?\s*(?:개|건|통|명)?", " ", label or "").split())
    if not s or s.lower() in ("on", "off"):
        return ""
    return "{대상}" if item and len(s) >= 12 and " " in s else s


def _op_name(op):
    """작업 이름에서 번호·개수·날짜를 뗀다(9번글 내용 요약 → 글 내용 요약, 9번글에 댓글 작성 → 댓글 작성) —
    글 하나가 아니라 작업을 기억한다."""
    return " ".join(re.sub(r"\d+\s*(?:번째|번|개|건|통|년|월|일)?(?:\s*(?:게시)?글(?:에|의))?", " ", op).split())


# 매번 바뀌는 추적용 쿼리 키(쿠팡 traceId 등)
TRACK = re.compile(r"(?:utm_\w+|traceid|fbclid|gclid|igshid|spm|scm|_ga|mc_[ce]id|clickid|trackingid)$", re.I)
# 쿼리 없는 결과 페이지로 보내는 검색 버튼(코레일 '열차조회')
SEARCH_BTN = re.compile(r"조회|검색|찾기|search", re.I)
# 조회를 못 했다는 요약(차단·실패·못 찾음)
FAILED = re.compile(r"access denied|차단|실패|확인하지 못|조회할 수 없|찾을 수 없|접근할 수 없|불러오지 못|열리지 않|"
                    r"(?:등록|작성|발송|전송|승인|저장)(?:하지|되지) (?:못|않)", re.I)
# 사용자가 말해도 메뉴 이름인 흔한 말 — 휴리스틱: 손으로 만든 표, 없는 메뉴 말은 {대상}이 된다
MENU_WORDS = ("목록 메뉴 전체 보기 인기글 공지 베스트 최신글 전체글 추천글 받은메일함 보낸메일함 안읽은 읽지않은 중요 스팸 "
              "휴지통 임시보관 검색")


def _clean_url(url):
    """기억할 URL에서 매번 바뀌는 쿼리를 뗀다 — 추적값(traceId·utm_*), 캐시용 시각(t=…), 날짜 값(date=2026-10-03,
    그날 기본값으로 연다), 32자 넘는 불투명 토큰(검색 조건·세션 덩어리 filter=4XAAjW… — 그 대상의 결과만 다시 연다)."""
    # 휴리스틱: 토큰 길이 휴리스틱 — 그 값이 있어야 열리는 페이지면 빈 페이지가 된다
    p = urlparse(url)
    if not p.query:
        return url
    keep = [s for s in p.query.split("&") if not TRACK.match(k := s.partition("=")[0]) and k.lower() not in TIME_KEYS
            and not re.fullmatch(r"\d{4}[-.]?\d{2}[-.]?\d{2}|[A-Za-z0-9_-]{32,}", unquote(s.partition("=")[2]))]
    return urlunparse(p._replace(query="&".join(keep)))


def _detour(steps):
    """글을 잘못 열었다 목록으로 돌아간 헛걸음(click {대상} → click 목록 → click {대상})은 뒤의 {대상}만 남긴다.
    폼에서 값을 두 번 고른 것(출발역 {대상} → 도착역 {대상})은 그대로."""
    out = []
    for s in steps:
        if s == "click {대상}" and len(out) >= 2 and out[-2] == "click {대상}" and re.search(r"목록|뒤로|이전|back|list", out[-1], re.I):
            del out[-2:]
        out.append(s)
    return out


def _learn(mem, rec, here, args, log, secrets, name_site=None):
    """done이 받아들여질 때 이번에 실제로 거친 경로를 사이트 메모리에 기록한다(모델은 이름 op만 준다).
    page로 쓴 기억 작업을 실패로 봐야 하면 False — 조작인데 진입 뒤 입력·선택·클릭이 없거나 그 이름의 기록이 거부됨.
    rec["trace"]: 성공한 동작 (도구, 라벨, 동작 전 URL, 위험 여부, 값). 값은 자리 판단에만 쓰고 저장하지 않는다."""
    tr = rec["trace"]
    if rec["login"] is not None and not any(  # 로그인을 부탁한 뒤 성공한 동작이 없으면 그 경로는 다음에 안 된다
            x[0] in ("extract", "run_code", "type", "select", "click") for x in tr[rec["login"]:]):
        return True
    urls = [x[2] for x in tr] + [here]  # urls[i + 1] = i번째 동작 뒤 URL
    typed = [v for v in rec["typed"] if len(v.strip()) >= 2]
    # 라벨에 입력값·사용자가 따옴표로 준 값이 섞이면(에디터 라벨 = 그 안의 글자) 자리 이름으로 — 개인정보를 남기지 않는다
    vals = [v.strip() for v in typed] + [q for s in rec["said"] for q in re.findall(r"['\"‘“]([^'\"’”]{2,})['\"’”]", s)]

    def safe(label, n):  # 라벨이 80자로 잘리면 입력값의 앞부분만 남으니 반대 방향(라벨 ⊂ 값)도 본다
        label = label or "?"
        hit = any(v in label or (len(label) >= 4 and label in v) for v in vals)
        return ("{대상}" if n == "click" else "입력칸") if hit else label
    found = [v for v in typed if any(_search_template(u, [v]) for u in urls)]  # 이어진 URL 쿼리에 나온 값 = 검색어
    act = next((i for i, (n, _, _, danger, v) in enumerate(tr)
                if n == "select" or danger or (n == "type" and v not in found)
                or (n == "run_code" and FORM_WRITE.search(v))), None)  # 클릭만 하는 스크립트(탭 눌러 읽기)는 아니다
    if act is None:  # 읽기: 마지막으로 읽은 곳
        entry = (rec["reads"] or [here])[-1]
        at = [x[0] for x in tr if x[2] == entry]
        got = [n for n in at if n in ("extract", "run_code")]
        how = (got[-1] if got else "관찰") + " 대기" * ("wait" in at) + " 스크롤" * ("scroll" in at)
        end = next((i for i, x in enumerate(tr) if x[2] == entry), len(tr))
        read = max((i for i, x in enumerate(tr) if x[2] == entry and x[0] in ("extract", "run_code")), default=len(tr))
        # 도착 뒤 읽기 전에 누른 것 — 주소가 안 바뀌는 탭(카페 인기글)도 재생되게
        steps = [f"click {p}" for x in tr[end:read] if x[0] == "click" and (p := _plain(safe(x[1], "click")))][-3:]
        free = True  # 진입을 아래에서 대상 클릭 전 페이지로 되돌려도 되는가(글·검색·폼으로 이미 정했으면 아니다)
        lead = end - 1 if 0 < end <= len(tr) and tr[end - 1][0] == "click" else None  # 진입으로 들어간 클릭(폼 판단용)
        # 글·메일 하나를 열어 읽었다 — 그 글로 들어간 클릭 전 목록이 진입, 그 클릭은 {대상}(Gmail #inbox/ID → #inbox).
        # 검색어가 쿼리에 든 결과 페이지는 글이 아니다
        j = next((j for j in range(end - 1, -1, -1) if tr[j][0] == "click" and not mem.item(tr[j][2])),
                 None) if mem.item(entry) and not _search_template(entry, found, rec["said"]) else None
        if j is not None:
            entry, end, steps, free = tr[j][2], j, ["click {대상}", *steps], False
        elif (srch := next((u for u in reversed(urls[:end]) if urlparse(u).netloc == urlparse(entry).netloc
                            and _search_template(u, found, rec["said"])), None)) \
                and not _search_template(entry, found, rec["said"]):
            # 검색해서 결과 하나(상품·팀 페이지)로 가 읽었다 — 그 결과 페이지는 대상 것이다. 검색 결과가 진입, 결과 고르기는 {대상}
            entry, end, steps, free = srch, urls.index(srch), ["click {대상}", *steps], False
        elif lead is not None and not urlparse(entry).query and SEARCH_BTN.search(tr[lead][1]):
            # 검색 버튼으로 연 쿼리 없는 결과 페이지(코레일 열차 목록)는 바로 열면 비어 있다 — 그 폼 페이지가 진입,
            # 폼에서 누른 것(마지막 6개)이 단계. 사용자가 고른 값(역 이름)은 아래에서 {대상}이 된다
            form = tr[lead][2]
            start = max((i for i in range(lead) if tr[i][0] == "navigate"), default=-1) + 1
            pre = [f"click {p}" for x in tr[start:lead + 1]
                   if x[0] == "click" and x[2] == form and (p := _plain(safe(x[1], "click")))]
            entry, end, steps, free = form, start, pre[-6:] + steps, False
        steps = [s for i, s in enumerate(steps) if not i or s != steps[i - 1]]  # 같은 글을 다시 누른 것은 하나로
        task = {"kind": "read", "url": _clean_url(_search_template(entry, found, rec["said"]) or entry), "how": how,
                "steps": steps}
        bump = False
    else:  # 조작: 첫 입력·선택·위험 동작 직전 URL이 진입
        entry, end, item = tr[act][2], act, None
        if mem.item(entry):  # 개별 글에서 한 조작(댓글 등) — 그 글로 들어간 클릭 전 URL이 진입이다
            item = next((j for j in range(act - 1, -1, -1) if tr[j][0] == "click" and not mem.item(tr[j][2])), None)
            if item is not None:
                entry, end = tr[item][2], item
        else:  # iframe 안에서 연 글(카페 — 주소가 안 바뀐다): 같은 주소에서 첫 입력 전에 누른 글 제목 같은 클릭이 그 글
            item = next((j for j in range(act - 1, -1, -1)
                         if tr[j][0] == "click" and tr[j][2] == entry and _plain(tr[j][1]) == "{대상}"), None)
            end = act if item is None else item
        free = item is None
        # 앞 작업이 열어 둔 글에서 시작했거나(진입을 모른다) 조작 구간에 스크립트가 있으면(단계로 못 적는다) 횟수만
        script = any(x[0] == "run_code" and CODE_WRITE.search(x[4]) for x in tr[end:])
        bump = script or (item is None and bool(mem.item(entry)))
        steps = []
        for i in range(end, len(tr)):
            n, label, _, danger, v = tr[i]
            label = safe(label, n)
            if i == item:
                steps.append("click {대상}")
            elif n == "click":
                if label := label if danger else _plain(label):
                    steps.append(f"{'⚠' if danger else ''}click {label}")
            elif n == "type":
                steps.append(f"type {label}={{{label}}}")
            elif n == "select":  # 사용자가 말한 값이면 자리로, 아니면 고른 옵션(분류=잡담 같은 필수 선택)
                steps.append(f"select {label}=" + (f"{{{label}}}" if any(v in s for s in rec["said"]) else v))
        steps = list(dict.fromkeys(steps[::-1]))[::-1]  # 같은 동작 반복은 마지막 것만(저장 실패 → 분류 선택 → 완료)
        if not steps and not script:
            return False
        last = max((i for i in range(end, len(tr)) if tr[i][0] in ("click", "type", "select")), default=len(tr) - 1)
        after = _clean_url(urls[last + 1].split("#")[0])
        if iid := mem.item(after):  # 개별 항목 ID는 뗀다(post.html?id=583941 → post.html)
            after = re.sub(rf"/{iid}/?(?=\?|$)|[^?&=]*={iid}(?:&|$)", "", after).rstrip("?&")
        check = re.sub(r"^[a-z]+://[^/]*/?", "", after) if after != entry.split("#")[0] else ""
        task = {"kind": "act", "url": _clean_url(entry), "steps": steps[:11] + steps[-1:] if len(steps) > 12 else steps,
                "check": check}
    nav = max((i for i in range(end) if tr[i][0] == "navigate"), default=-1)
    clicks = [i for i in range(nav + 1, end) if tr[i][0] == "click"] if free else []  # 진입까지 누른 것
    # 진입까지 누른 메뉴(조작만 — 다른 사이트에 길로 보여 준다. 읽기의 path는 어디에도 안 쓰여 저장하지 않는다)
    path = [(i, p[:20]) for i in range(nav + 1, end)
            if task["kind"] == "act" and tr[i][0] == "click" and (p := _plain(safe(tr[i][1], "click"), False))]
    rem = args.get("remember") or []
    rem = [rem] if isinstance(rem, dict) else rem if isinstance(rem, list) else []
    site = mem.site_at(task["url"]) or next((str(e["site"]) for e in rem if isinstance(e, dict) and e.get("site")),
                                            None)
    own = mem.tasks(site) if site else {}
    if task["kind"] == "read" and any(mem.tasks(s).get(k, {}).get("kind") == "act" for s, k in rec["refs"]):
        return False  # 조작 작업을 부르고(또는 그 진입에 와서) 입력·클릭 없이 끝났다 — 조작 게이트 실패

    def fits(t):  # 같은 종류. 같은 주소에서 누를 것만 다른 읽기(카페 탭)는 다른 작업이다
        return t["kind"] == task["kind"] and (
            task["kind"] == "act" or t.get("steps", []) == task["steps"] or not same_page(t["url"], task["url"]))
    # 이름: 이번에 부르거나 도착한 같은 계열 기억 작업(틀린 기억을 고쳐 쓴다), 진입 URL이 똑같은 기억 작업, op.
    # 조작은 op가 먼저(요청한 작업이 분명하다 — 반려가 승인하기를 덮지 않게). 셋 다 없으면 기록 안 함(클릭 라벨은 이름이 아니다)
    said0 = _drop(rec["said"][0].lower(), mem.names(site) if site else [])
    fam = _family(BOX.sub(" ", said0))
    op = _op_name(_mask(str(args.get("op") or "").strip(), secrets))
    # 모델이 해냈다고 하지 않았다 — 읽기인데 op가 없거나, 요약이 실패라고 한다(쿠팡 Access Denied·등록하지 못함).
    # 기억을 성공으로 세지 않는다
    # 휴리스틱: 요약 문구 휴리스틱 — '일부는 확인하지 못했지만'처럼 부분 성공도 기록하지 않는다
    if task["kind"] == "read" and not op or FAILED.search(str(args.get("summary") or "")):
        log.debug("[memory] 해내지 못한 작업이라 기록하지 않음")
        return True

    # 클릭 라벨이 사용자 문구와 낱말을 나누면(사람 이름·글 제목) {대상}으로. 사이트·작업 이름·조작 계열·화면 말은 겹쳐도 둔다
    # 휴리스틱: 낱말 겹침 휴리스틱 — 요청에 없는 이름·제목은 못 잡고, 요청 낱말이 든 진짜 메뉴 이름도 {대상}이 된다
    said = _grams(re.sub(r"\S+\.\S+", " ", " ".join(rec["said"])))  # URL·도메인은 사용자 문구에서 뺀다
    base = _grams(" ".join([*(mem.names(site) if site else []), host_name(task["url"]),
                            *(w for ws in FAMILIES.values() for w in ws)]))
    keep = base | _grams(" ".join([*own, MENU_WORDS]))
    core = lambda s: _grams(s) - STOP - READ_WORDS  # noqa: E731
    # 사용자가 말한 대상(팀·사람·글)을 눌러 들어간 페이지(/sports/team)는 그 대상 것이다 — 그 클릭을 한 페이지가 진입,
    # 그 클릭은 {대상}, op에 든 대상 이름(OO팀 경기 정보 조회)은 뗀다. op가 그 라벨 말고 남는 게 없으면 라벨이
    # 작업 자체(경기일정 탭)라 대상이 아니다. 대상 클릭과 진입 사이에 대기·읽기·다른 클릭이 끼어도 된다
    def strip_of(i):
        lw = _grams(tr[i][1])
        return " ".join(w for w in op.split() if not _grams(w) & lw)

    def target(i):
        lw, s = _grams(tr[i][1]), strip_of(i)
        # 긴 문장 라벨(글·경기 카드)은 마지막 클릭만 — 앞의 긴 메뉴·카페 이름 링크는 대상이 아니다
        return (bool((lw - keep) & said) or i == clicks[-1] and _plain(tr[i][1]) == "{대상}") and (s == op or bool(core(s)))
    tgt = next((i for i in reversed(clicks) if target(i)), None)
    if tgt is not None:
        page = tr[tgt][2]
        label = lambda i: _plain(safe(tr[i][1], "click"))  # noqa: E731
        tabs = [f"click {label(i)}" for i in clicks if i < tgt and tr[i][2] == page and label(i)]  # 그 페이지의 탭(야구)
        later = [f"click {label(i)}" for i in clicks if i > tgt and label(i)]  # 들어간 뒤 누른 것(경기 예매)
        op, task["url"] = strip_of(tgt), _clean_url(page)
        task["steps"] = [*tabs, "click {대상}", *later, *task["steps"]]
        path = [x for x in path if x[0] < tgt and tr[x[0]][2] != page]
    path = [x for _, x in path]
    task["path"] = [x for i, x in enumerate(path) if not i or x != path[i - 1]]  # 20자, 연속 반복은 하나

    def meant(k):  # 읽기: 거쳐 온 작업 안(목록의 글)에서 읽었거나, 그 이름의 핵심 말(흔한 읽기 말 목록·조회·문서… 뺀
        # 나머지)이 요청·op에 다 있거나, op의 핵심 말이 모두 그 이름 안에 있다('대기 문서 보기' ≈ '결재대기함 목록 조회').
        # 계열·흔한 말만 겹치면 안 된다 — '안읽은 메일'이 받은메일함을, '결재완료 문서 목록'이 '결재대기 문서 목록 조회'를 덮었다
        t = own[k]
        inside = mem.item(task["url"]) or same_page(t["url"], task["url"])
        core = _grams(k) - STOP - READ_WORDS
        covered = bool(core) and core <= _grams(said0) | _grams(op)
        return task["kind"] == "act" or inside or covered or bool(op) and _grams(op) - STOP - READ_WORDS <= _grams(k)
    called = next((k for s, k in reversed(rec["refs"] + rec["arrived"])
                   if s == site and k in own and fits(own[k]) and _family(k) == fam and meant(k)), "")
    same = next((k for k, t in own.items() if _norm(t["url"]) == _norm(task["url"]) and fits(t)), "")
    name = (op or called or same) if task["kind"] == "act" else (called or same or op)
    if not name:
        return True
    keep |= _grams(name)  # 작업 이름에 든 말(대상 이름을 뗀 뒤)은 메뉴 이름이다
    shared = lambda s: bool((_grams(s) - keep) & said)  # noqa: E731
    task["path"] = ["{대상}" if shared(x) else x for x in task["path"]]
    task["steps"] = _detour([re.sub(r"(click )(.+)", lambda m: m[1] + ("{대상}" if shared(m[2]) else m[2]), s)
                             for s in task["steps"]])
    text = unquote(task["url"] + " " + task.get("check", ""))  # 템플릿으로 못 바꾼 입력값·사용자 낱말이 남으면 기록 안 함
    if any(v in text for v in typed) or (_grams(text) - base) & said:
        log.debug("[memory] 입력값·사용자 문구가 URL에 남아 기록하지 않음")
        return True
    p = urlparse(task["url"])
    # 처음 간 사이트인데 remember가 없다 — 제목·메타·사용자 답으로 이름을 정한다(로그인 페이지는 빼고, 못 정하면 호스트)
    login = re.match(r"(nid|login|accounts|auth|sso)\.", p.netloc.lower()) or re.search(r"login|signin", p.path, re.I)
    if not site and task["kind"] == "read" and mem.item(task["url"]):  # 글 주소로 바로 가서 읽었다 — 기록할 게 없으니 빈 사이트도 안 만든다
        return True
    if not site and p.scheme in ("http", "https") and not login:
        home = site_home(task["url"]) or f"{p.scheme}://{p.netloc}/"  # 카페는 카페 홈 — 도메인만 두면 모든 카페가 걸린다
        # 이름을 못 정하거나 겹칠 때의 이름: 호스트, 카페처럼 ID로 갈리면 그 ID까지(호스트 이름이 다른 카페 것일 수 있다)
        fallback = _default_match(home) if site_home(task["url"]) else host_name(task["url"])
        # 사용자가 요청에서 주소 앞에 붙인 이름(예시카페(https://…)) — 모델이 remember를 빠뜨려도 사용자가 부른 이름으로
        named = next((m[1] for s in rec["said"] for m in re.finditer(r"([^\s()]{2,20})\s*\(\s*(https?://[^\s)]+)", s)
                      if urlparse(m[2]).netloc == p.netloc and not generic_name(m[1])), None)
        site = named or (name_site(task["url"]) if name_site else fallback)
        if Memory._find(mem._load(), site):  # 같은 이름의 다른 사이트(주소가 다르다)가 있다 — 덮어쓰지 않는다
            log.debug(f"[memory] 이름 {site!r}은 다른 사이트가 쓰고 있어 {fallback!r}로 남긴다")
            site = fallback
        out = mem.remember(site, url=home)
        if out.startswith("error:") and site != fallback:  # 이름이 다른 작업 이름과 겹치는 등
            log.debug(f"[memory] {out}")
            site = fallback
            out = mem.remember(site, url=home)
        log.debug(f"[memory] {out}")
    if not site:
        return True
    # 기억한 목록을 불러 그 안의 글(같은 경로에 쿼리만 더한 곳·개별 글)에서 읽었다 — 목록 기억은 그대로 두고 횟수만
    # 글을 열어 읽었는데({대상}) 그 이름이 같은 곳의 목록 기억이면(op가 목록 이름) 목록을 글 읽기로 덮지 않는다
    old = own.get(name, {})
    deeper = task["kind"] == "read" and old.get("kind") == "read" and (name == called and (mem.item(task["url"]) or (
        _norm(old["url"]) != _norm(task["url"]) and same_page(old["url"], task["url"]))) or (
        "click {대상}" in task["steps"] and "click {대상}" not in old.get("steps", []) and same_page(old["url"], task["url"])))
    out = mem.record(site, name, None if bump or deeper else task)
    log.debug(f"[memory] {out}" + ("" if bump or deeper else f" <- {task['url']}"))  # 기록한 진입 — 실측에서 원인을 보려고
    return not (out.startswith("error:") and any(k == name for _, k in rec["refs"]))


def _search_template(url, typed, said=()):
    """검색창에 입력한 값(또는 사용자 문구에 든 2자 이상 값)이 URL의 쿼리 값이면 그 자리를 {q}로 바꾼 검색 템플릿, 아니면 None.
    나머지 쿼리는 그대로 둔다(검색 범위·게시판 ID일 수 있다)."""
    p = urlparse(url)
    if not p.query:
        return None
    pairs = parse_qsl(p.query, keep_blank_values=True)
    want = {t.strip() for t in typed if t.strip()}
    hit = next((k for k, v in pairs if v.strip() in want or len(v.strip()) >= 2 and any(v.strip() in s for s in said)),
               None)
    if hit is None:
        return None
    query = "&".join(f"{k}={{q}}" if k == hit else urlencode([(k, v)]) for k, v in pairs)
    return urlunparse(p._replace(query=query, fragment=""))


def _obs_url(observation):
    m = re.search(r"^URL: (\S+)", observation, re.M)
    return m.group(1) if m else ""


def _mask(text, secrets):
    for s in secrets:
        if s:
            text = text.replace(s, "****")
    return text


def _mask_same_length(text, secrets):
    """Mask secrets without changing recall offsets; mask before chunking to cover boundary splits."""
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "*" * len(secret))
    return text


def _stub_observations(messages):
    """이전 관찰/도구 결과는 스텁으로 치환해 컨텍스트 누적을 막는다.
    호출 시점에 messages에 들어 있는 tool_result는 전부 과거 턴 것이므로 최신 보호는 하지 않는다."""
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if b.get("type") == "text" and b["text"].startswith("현재 페이지:"):
                    b["text"] = "[시스템: 컨텍스트 절약을 위해 이전 페이지 관찰 내용을 제거함]"
                elif b.get("type") == "tool_result":
                    body = b.get("content")
                    if isinstance(body, str):
                        b["content"] = _shrink(body)
                    elif isinstance(body, list):
                        for i, sub in enumerate(body):
                            if sub.get("type") == "image":
                                body[i] = {"type": "text",
                                           "text": "[시스템: 컨텍스트 절약을 위해 이전 스크린샷을 제거함]"}
                            elif sub.get("type") == "text":
                                body[i] = {"type": "text", "text": _shrink(sub.get("text", ""))}


def _shrink(text):
    """TOOL_RESULT_CAP 초과면 앞 TOOL_RESULT_KEEP자 + 스텁. 결과가 CAP 이하라 다시 적용해도 그대로다
    (내용이 턴마다 같아야 대화 기록 프롬프트 캐시가 맞는다)."""
    if len(text) <= TOOL_RESULT_CAP:
        return text
    # 조사 ID는 결과 끝에 붙는 경우가 많으므로 축약해도 모델이 recall할 수 있게 보존한다.
    marker = re.search(r"(?:^|\n)(조사 ID: R\d+)\s*$", text)
    suffix = f"\n{marker.group(1)}" if marker else ""
    keep = max(0, TOOL_RESULT_KEEP - len(suffix))
    return f"{text[:keep]}\n…{TOOL_RESULT_STUB}{suffix}"


def _texts(content):
    """text 블록을 중복 없이 이어붙인다 (모델이 같은 내용을 여러 블록으로 내는 경우 대비)."""
    seen = dict.fromkeys(b.get("text", "").strip() for b in content if b.get("type") == "text")
    return " ".join(t for t in seen if t)


def _trace(turn):
    """LLM 없이 이 턴의 도구 호출/결과를 기계적으로 정리한다. 없으면 None."""
    lines = []
    for i, m in enumerate(turn):
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        nxt = turn[i + 1]["content"] if i + 1 < len(turn) and isinstance(turn[i + 1].get("content"), list) else []
        outs = {b.get("tool_use_id"): b.get("content") for b in nxt if b.get("type") == "tool_result"}
        for b in m["content"]:
            if b.get("type") != "tool_use" or b.get("name") in ("screenshot", "wait", "scroll"):
                continue
            args = b.get("input") or {}
            out = outs.get(b.get("id"))
            out = out if isinstance(out, str) else ""
            # 도구마다 한 줄 — extract 원문을 그대로 붙이면 중단 결과가 페이지 덤프가 된다
            head = f"- {_brief(b['name'], args, '') or b['name']}".split("\n", 1)[0]
            lines.append(head + (f": {_clip(out, 80)}" if out and b["name"] != "extract" else ""))
    return "\n".join(lines) or None


def _summarize(messages, reason, log=None, start=0):
    """지금까지 알아낸 것을 텍스트로 요약받는다. LLM 요약이 실패하면 도구 로그 기반 요약으로 폴백.
    요약 지시문은 복사본에만 붙여 다음 턴의 히스토리를 오염시키지 않는다."""
    last = messages[-1]
    content = list(last["content"]) if isinstance(last["content"], list) else [
        {"type": "text", "text": str(last["content"])}]
    content.append({"type": "text", "text":
        f"[시스템: {reason}. 도구를 더 쓰지 말고 지금까지 알아낸 내용과 "
        "진행 상황을 사용자에게 텍스트로 요약해라.]"})
    copy = messages[:-1] + [{"role": last["role"], "content": content}]
    try:
        content = llm.chat(copy, TOOLS).get("content") or []
    except Exception as e:
        if log:
            log.debug(f"[summary] LLM 요약 실패 ({e}) - 도구 로그로 대체")
        content = []
    # 모델이 "정리합니다" 같은 머리말 + 도구 호출을 내면 머리말은 요약이 아니다 → 실행 기록으로
    uses = [b for b in content if b.get("type") == "tool_use"]
    done = next(((b.get("input") or {}).get("summary", "") for b in uses if b.get("name") == "done"), "")
    text = done or ("" if uses else _texts(content))
    if text:
        return text
    trace = _trace(messages[start:])
    return f"(LLM 요약 없음, 실행 기록)\n{trace}" if trace else None


def _chat(messages, stop):
    """llm.chat을 부르되, 응답 대기 중 중단 버튼이 눌리면 기다리지 않고 바로 llm.Cancelled를 올린다.
    요청은 별도 스레드에서 돌리고, 중단되면 연결을 끊고(abort) 그 스레드의 결과는 버린다."""
    if stop is None:
        return llm.chat(messages, TOOLS)
    # 이 호출 전용 취소 플래그 — stop은 _stopped가 비우므로, 버려진 요청이 그걸 보고 재시도하지 않게 한다
    gone = threading.Event()
    kw = {"cancel": gone.is_set} if "cancel" in inspect.signature(llm.chat).parameters else {}
    box, done = {}, threading.Event()

    def call():
        try:
            box["r"] = llm.chat(messages, TOOLS, **kw)
        except BaseException as e:
            box["e"] = e
        finally:
            done.set()

    threading.Thread(target=call, name="llm-call", daemon=True).start()
    while not done.wait(0.1):
        if stop.is_set():
            gone.set()
            llm.abort()
            raise llm.Cancelled()
    if "e" in box:
        raise box["e"]
    return box["r"]


def _stopped(stop, messages, start):
    """사용자 중단 결과. 요약용 LLM을 다시 부르지 않고(중단이 늦어진다) 실행 기록으로 바로 끝낸다."""
    stop.clear()
    trace = _trace(messages[start:])
    return "사용자가 중단함" + (f"\n지금까지 한 일:\n{trace}" if trace else "")


def run(task, browser, io, messages=None, mem=None, use_tab=True, tab_url=None):
    """Run a task. Use History to retain research across calls; plain lists only hold messages.

    use_tab=False면 사용자가 챗 창에서 현재 탭을 뺀 요청 — 처음부터 백그라운드 브라우저에서 작업한다.
    tab_url은 사용자가 챗 창 칩에서 본 탭의 URL(그 탭에서 시작)."""
    if messages is None:
        messages = History()
    if mem is None:
        mem = Memory()
    # 이번 요청의 기록 재료(_learn): 성공한 동작, 읽은 URL, 입력값·사용자 문구(자리 판단용 — 저장하지 않는다),
    # page로 쓰거나 도착한 기억 작업, 마지막 로그인 요청 때의 동작 수, done이 기록 게이트를 넘었거나 사용자가 취소했는지
    # refs = page로 부른 작업(이름·실패 판단), arrived = 진입에 도착한 작업(이름 판단만)
    rec = {"trace": [], "reads": [], "typed": [], "said": [task], "refs": [], "arrived": [], "login": None,
           "ok": False, "hinted": set()}
    try:
        return _run(task, browser, io, messages, mem, use_tab, tab_url, rec)
    finally:
        # page로 쓴 기억 작업이 있는데 done 없이 끝났거나(차단·상한·중단) 조작 게이트를 못 넘었으면 연속 실패 +1
        if not rec["ok"]:
            for site, name in dict.fromkeys(rec["refs"]):
                mem.fail(site, name)


def _run(task, browser, io, messages, mem, use_tab, tab_url, rec):
    secrets = getattr(io, "secrets", set())
    log = _Log(io)
    _stub_observations(messages)
    start = len(messages)
    # 턴마다 사용자가 지금 보는 탭에서 시작한다 — "이 탭 요약해줘"는 지난 턴의 백그라운드 탭이 아니다
    headless = False
    if not use_tab or getattr(browser, "detached", False):  # 탭 없이 시작한 대화는 계속 탭 없이
        to_bg = getattr(browser, "use_background", None)
        try:  # 대화의 첫 요청에서 뺀 탭만 대화 전체를 탭 없이로 고정한다
            headless = bool(to_bg and to_bg(first=start == 0))
        except TypeError:
            headless = bool(to_bg and to_bg())
        log.brief("탭 없이 작업" if headless
                  else "백그라운드 브라우저를 쓸 수 없어 현재 탭에서 작업합니다")
    focus = getattr(browser, "focus_user_tab", None)
    pinned = getattr(browser, "pinned", None)
    if focus and not headless:
        focus(tab_url) if tab_url else focus()
        pin = getattr(browser, "pin_user_tab", None)
        if pin:
            pin()  # 대화의 첫 요청에서 고른 탭을 새 채팅 전까지 계속 쓴다
        if getattr(browser, "pin_lost", False) or (
                pinned is not None and getattr(browser, "pinned", None) is not pinned):
            browser.pin_lost = False
            log.brief("이 대화의 탭이 닫혀 지금 보고 있는 탭에서 이어서 작업합니다")
    observation = browser.observe()
    recalled, shown = mem.recall(task, _obs_url(observation))
    sent = _sent_lines(messages)
    first = [{"type": "text", "text": task}]
    if headless:
        first.append({"type": "text", "text": BG_HEADER})
    if shown:
        log.brief(f"기억 참고: {', '.join(sorted(shown))}")
    # 시작 때 단계 줄을 받은 작업(요청과 같은 조작 계열)은 도착한 작업처럼 이름 판단에만 쓴다
    rec["arrived"] += [(k, h[0]) for k in sorted(shown) if (h := mem.hint(k, task.lower()))]
    recalled = _mem_block((recalled.split("\n") if recalled else [])
                          + [x for k in sorted(shown) for x in _alias_hint(mem, task, k, rec)], sent)
    if recalled:
        log.debug(f"[memory] {len(recalled)}자 참조")
        first.append({"type": "text", "text": recalled})
    first.append({"type": "text", "text": f"현재 페이지:\n{observation}"})
    messages.append({"role": "user", "content": first})
    recent, retried, blocked, prodded = [], False, 0, False
    cancelled = False  # 이번 턴에 사용자가 ask_user를 취소했는지
    code_fails = 0  # 이번 턴에 실패한 run_code 수 (코드가 매번 달라 반복 차단에 안 걸린다)
    # 실행한(실패 포함 — 실패해도 부작용이 났을 수 있다) 위험 동작 중 결과를 아직 확인하지 않은 것.
    # 확인 전에는 위험 동작 재실행·done을 막고, 확인 뒤 재시도는 다시 승인을 묻는다
    unverified = set()
    extracted, shot_count = set(), {}  # 페이지별 extract/캡처 상태
    typed = rec["typed"]  # 이번 턴에 type한 값 — 이어진 URL의 쿼리에 나타나면 검색어({q})다
    bg_urls = []  # 이번 턴에 백그라운드 브라우저에서 연 URL — 처음 한 번만 진행내역에 알린다

    stop = getattr(io, "stop_requested", None)
    if stop is not None and hasattr(browser, "stop"):
        browser.stop = stop  # 실행 중인 wait·run_code도 중지 버튼에 바로 멈춘다
    for step in range(1, STEP_CAP + 1):
        if stop is not None and stop.is_set():
            return _stopped(stop, messages, start)
        try:
            t_llm = time.monotonic()
            resp = _chat(messages, stop)
            log.debug(f"[step {step}] (llm {time.monotonic() - t_llm:.1f}s)")
        except llm.Cancelled:
            log.debug(f"[step {step}] 사용자 중단 — LLM 요청을 끊음")
            return _stopped(stop, messages, start)
        except Exception as e:
            summary = _summarize(messages, "LLM 오류로 중단됨", log, start) if step > 2 else None
            rec["ok"] = True  # 모델 쪽 장애다 — 기억한 작업의 실패로 세지 않는다(실패는 차단·상한·중단만)
            return f"LLM 오류: {e}" + (f"\n지금까지의 진행 요약:\n{summary}" if summary else "")
        content = resp.get("content") or []
        if not content and not retried:
            # max_tokens 등으로 빈 응답이 오면 한 번만 재시도
            retried = True
            log.debug(f"[step {step}] 빈 응답 (stop_reason={resp.get('stop_reason')}) - 재시도")
            continue
        messages.append({"role": "assistant", "content": content})

        uses = [b for b in content if b.get("type") == "tool_use"]
        if not uses:
            text = _texts(content)
            if not prodded:
                # "클릭하겠습니다"만 말하고 도구를 안 부르는 경우 — 한 번은 이어서 하게 한다
                prodded = True
                log.debug(f"[step {step}] 도구 호출 없음 (stop_reason={resp.get('stop_reason')}) - 재촉")
                messages.append({"role": "user", "content": [{"type": "text", "text":
                    "[시스템: 도구를 호출하지 않았다. 작업이 남았으면 지금 도구를 호출하고, "
                    "끝났으면 done으로 결과를 보고해라.]"}]})
                continue
            return text or f"(모델이 빈 응답으로 종료: stop_reason={resp.get('stop_reason')})"

        results = []
        mem_lines = []  # 이 스텝 끝에 붙일 [사이트 메모리] 줄 — tool_result들 뒤에 둔다
        need_obs = False
        for n, block in enumerate(uses):
            if stop is not None and stop.is_set():
                # 남은 도구는 실행하지 않는다. tool_use마다 결과가 있어야 다음 요청이 API 오류가 안 난다
                for rest in uses[n:]:
                    results.append(_result(rest["id"], "error: 사용자가 중단해 실행하지 않음", True))
                break
            name, args = block["name"], block.get("input") or {}
            if name == "recall":
                archive = messages if isinstance(messages, History) else None
                out = archive.recall(args, secrets) if archive else "error: 자료 보관은 같은 대화의 History에서만 유지됩니다"
                results.append(_result(block["id"], out, out.startswith("error:")))
                continue
            ref = None  # 기억으로 가는 navigate: (사이트, 작업 이름, 저장 URL, 이동 URL, 상세 한 줄)
            if name == "navigate" and args.get("page"):
                got = mem.resolve(args["page"], args.get("fill"))
                asked = {host_name(u) for u in URLS.findall(task)}  # 요청에 URL이 있으면 그 호스트의 기억만 부른다
                if not isinstance(got, str) and asked and host_name(got[3]) not in asked:
                    got = f"error: 요청한 사이트({', '.join(sorted(asked))})와 다른 기억 사이트다 — 요청한 URL로 가라"
                    # 같이 준 기억 URL로도 가지 않는다(요청한 호스트의 URL만 남긴다)
                    args = {k: v for k, v in args.items() if k != "url" or host_name(str(v)) in asked}
                if not isinstance(got, str):
                    ref, args = got, {**args, "url": got[3]}
                    if got[1]:
                        rec["refs"].append(got[:2])
                elif args.get("url"):  # 없는 참조를 지어냈어도 url을 같이 줬으면 그리로 간다 — 왕복 하나를 아낀다
                    log.debug(f"[step {step}]   page {args['page']!r} 무시({got}) — url로 이동")
                    got = None
            else:
                got = "error: url 또는 page가 필요하다" if name == "navigate" and not args.get("url") else None
            if isinstance(got, str):  # 틀린 페이지 참조 — 이동하지 않았으니 뒤 인덱스는 유효하지만 배치는 멈춘다
                log.debug(f"[step {step}] navigate({args}) -> {got}")
                log.brief(_fail_brief(got))
                results.append(_result(block["id"], got, True))
                for rest in uses[n + 1:]:
                    results.append(_result(rest["id"], "error: 이전 액션 실패로 건너뜀", True))
                need_obs = True
                break
            shown_args = _mask_args(name, args, observation, secrets)
            log.debug(f"[step {step}] {name}({shown_args})")

            if name == "done":
                if unverified:
                    # 위험 동작 뒤 확인 없이 끝내지 못하게 한다(한 번만 — 확인 수단이 없어도 무한히 막지 않는다)
                    unverified.clear()
                    log.debug(f"[step {step}]   -> [verify] 위험 동작 결과 미확인 — done 보류")
                    results.append(_result(block["id"], (
                        "[시스템: 아직 끝나지 않음. 승인된 위험 동작(발송·등록·삭제 등)을 실행했지만 결과를 확인하지 않았다. "
                        "extract나 find로 화면에서 결과(새 댓글·보낸 메일·완료 문구 등)를 확인한 뒤 done하고, "
                        "summary에 확인했는지/확인하지 못했는지 적어라.]"), True))
                    for rest in uses[n + 1:]:
                        results.append(_result(rest["id"], "error: done 이후 액션은 실행되지 않음", True))
                    break
                cur = getattr(browser, "current_url", None)
                err = _save_memory(mem, args.get("remember") or [], secrets, log, step,
                                   cur() if cur else _obs_url(observation), args.get("op") or "")
                if err:
                    # 잘못된 메모리는 버리지 않고 고칠 기회를 준다 (오류가 있을 때만 LLM 왕복 1회)
                    results.append(_result(block["id"], f"{err}\n고친 remember로 done을 다시 호출해라 "
                                                       "(이미 저장된 항목은 빼도 된다).", True))
                    for rest in uses[n + 1:]:
                        results.append(_result(rest["id"], "error: done 이후 액션은 실행되지 않음", True))
                    break
                if "<input:password>" in observation or re.search(r"로그인이? 필요", str(args.get("summary", ""))):
                    rec["login"] = len(rec["trace"])  # 로그인 화면에서 끝났다 — 로그인 게이트로 기록하지 않는다
                try:
                    _save_called(mem, rec, args, cur() if cur else _obs_url(observation), secrets, log)
                    rec["ok"] = _learn(mem, rec, cur() if cur else _obs_url(observation), args, log, secrets,
                                       lambda u: _name_site(browser, io, u, log))
                except OSError as ex:  # 기록 실패로 끝난 작업을 망치지 않는다
                    log.debug(f"[memory] 기록 실패 ({ex})")
                    rec["ok"] = True
                summary = args.get("summary") or _texts(content) or "(요약 없음)"
                results.append(_result(block["id"], summary))
                for rest in uses[n + 1:]:
                    results.append(_result(rest["id"], "error: done 이후 액션은 실행되지 않음", True))
                messages.append({"role": "user", "content": results})
                return summary

            if name == "ask_user":
                if cancelled:  # 취소한 질문을 다시 묻지 않는다
                    results.append(_result(block["id"], "error: 사용자가 이미 입력을 취소했다. "
                                           "다시 묻지 말고 가진 정보로 진행하거나 done으로 보고해라.", True))
                    continue
                q = args.get("question", "")
                fields = args.get("fields") or []
                fn = getattr(io, "ask_fields", None)
                if fields and fn:
                    answer = fn(q, fields)
                elif fields:
                    names = ", ".join(
                        f.get("name", "?") if isinstance(f, dict) else str(f)
                        for f in fields
                    )
                    answer = io.ask(f"{q} (항목: {names})")
                else:
                    opts = (args.get("options") or [])[:5]
                    answer = io.ask(q, opts) if opts else io.ask(q)
                cancelled = str(answer).startswith("[사용자가 입력을 취소함]")
                rec["said"].append(str(answer))
                if LOGIN.search(q) or any(isinstance(f, dict) and f.get("secret") for f in fields):
                    rec["login"] = len(rec["trace"])
                results.append(_result(block["id"], answer))
                continue

            # 확인 창에도 비밀값을 가린 코드를 보인다. 이번 턴에 이미 취소/거절했으면 다시 묻지 않고 거절한다
            ckey = _danger(name, shown_args, observation)
            if unverified and ckey:
                # 이미 실행된 위험 동작을 확인 없이 다시 실행하면 중복 발송·등록이 된다
                results.append(_result(block["id"], (
                    "error: 방금 실행한 위험 동작의 결과를 아직 확인하지 않았다. 다시 실행하지 마라 — "
                    "extract나 find로 결과를 먼저 확인해라. 실제로 반영되지 않았을 때만 다시 시도해라."), True))
                need_obs = True
                continue
            if not _confirm(name, shown_args, observation, _NO if cancelled else io):
                # 승인 창에서 취소하면 작업 전체를 바로 끝낸다 — 모델에게 돌려주면 다른 방법을 찾느라
                # 재시도·반복 차단을 거쳐 한참 뒤에야 멈춘다. 남은 tool_use에도 결과를 채워 다음 턴 API 오류를 막는다
                log.brief("취소 — 실행하지 않음")
                for rest in uses[n:]:
                    results.append(_result(rest["id"], "error: 사용자가 취소해 실행하지 않음", True))
                messages.append({"role": "user", "content": results})
                if stop is not None:
                    stop.clear()
                rec["ok"] = True  # 사용자 판단으로 멈춘 것 — 기억한 작업의 실패가 아니다
                return "사용자가 취소함"

            key = (name, repr(sorted(args.items())))
            if name == "run_code" and code_fails >= CODE_FAIL_CAP:
                log.brief(f"실패 — 스크립트 {code_fails}번 실패로 중단")
                results.append(_result(block["id"], (
                    f"error: run_code가 이번 작업에서 {code_fails}번 실패해 더 실행하지 않는다. "
                    "새 스크립트를 쓰지 말고 관찰 목록 인덱스로 type/click/find를 쓰거나, "
                    "안 되면 done으로 무엇이 실패했는지 보고해라."), True))
                continue
            recent.append(key)
            repeat = recent[-REPEAT_WINDOW:].count(key)
            if repeat >= REPEAT_MAX:
                blocked += 1
                log.debug(f"[step {step}]   -> 같은 동작 {repeat}회 반복 차단")
                results.append(_result(
                    block["id"],
                    f"error: 같은 동작을 최근 {REPEAT_WINDOW}스텝 안에 {repeat}회 반복하고 있다. "
                    "이 액션은 다시 실행되지 않는다. 대신: find로 다른 요소를 찾아 클릭, "
                    "back으로 이전 페이지, scroll(index) 후 재관찰, 또는 done으로 지금까지의 결과를 정리해라", True))
                need_obs = True
                continue

            # 실제로 실행하는 동작만, 실행 전에 보여준다 (긴 동작 중에도 무엇을 하는지 보이게)
            b = _brief(name, shown_args, observation)
            # 승인 창에서 이미 코드를 보였으면 단계 목록에는 첫 줄만 남긴다
            log.brief(b.split("\n", 1)[0] if b and ckey and name == "run_code" else b)

            if name == "screenshot":
                unverified.clear()  # 화면 확인도 결과 확인이다
                # 같은 배치에서 navigate가 먼저 돌았을 수 있어 실행 시점 URL로 키를 잡는다 (extract 기록과 같은 기준)
                cur = getattr(browser, "current_url", None)
                before = cur() if cur else _obs_url(observation)
                ukey = _page_key(cur()) if cur else _url_key(observation)
                if "(인터랙티브 요소 없음)" not in observation and ukey not in extracted:
                    # 최후 수단 게이트: extract도 안 해본 페이지에서의 첫 screenshot은 캡처하지 않고
                    # 그 자리에서 extract한 텍스트를 돌려준다 — 거부만 하면 모델이 extract를 따로
                    # 부르느라 LLM 왕복이 한 번 더 든다. 페이지를 바꾸지 않으므로 배치는 계속한다.
                    text = browser.act("extract", {})
                    extracted.add(ukey)
                    if text.strip() and not text.startswith("error:"):
                        archive_id = _retain_research(messages, "extract", before, text, secrets, log)
                        log.debug(f"[step {step}]   -> [gate] screenshot 대신 extract 결과 반환")
                        suffix = f"\n조사 ID: {archive_id}" if isinstance(archive_id, str) and archive_id.startswith("R") else ""
                        warning = archive_id if isinstance(archive_id, str) and archive_id.startswith("[") else ""
                        results.append(_result(
                            block["id"],
                            "[시스템: 이 페이지에서 아직 extract를 쓰지 않아 screenshot 대신 extract 결과를 "
                            "준다. 텍스트로 알 수 없는 화면(지도/차트/캔버스/이미지)이면 screenshot을 한 번 더 "
                            f"호출하면 캡처한다.]\n{text}{suffix}"
                            + (f"\n{warning}" if warning else "")))
                        continue
                    # extract가 비었거나 실패하면 텍스트로 볼 게 없으니 바로 캡처한다
                if shot_count.get(ukey, 0) >= SHOT_CAP:
                    log.debug(f"[step {step}]   -> [cap] screenshot 거부 - 페이지별 상한({SHOT_CAP})")
                    results.append(_result(
                        block["id"],
                        f"error: 이 페이지에서 screenshot을 이미 {SHOT_CAP}번 썼다. 같은 화면을 더 캡처해도 새 정보가 없다. "
                        "extract나 find로 텍스트를 확인하거나, 다른 페이지로 이동하거나, done으로 지금까지 "
                        "알아낸 것을 정리해라.", True))
                    continue
                jpeg = browser.screenshot()
                shot_count[ukey] = shot_count.get(ukey, 0) + 1
                show = getattr(io, "image", None)  # 챗 UI 미리보기. CliIO에는 없어 건너뛴다
                if show:
                    try:
                        show(jpeg, args.get("question") or "화면 확인")
                    except Exception:
                        pass
                if llm.vision_model():
                    try:
                        seen_full = llm.vision(jpeg, args.get("question") or
                                               "이 화면에 보이는 내용을 자세히 기술해라.")
                        seen = seen_full[:VISION_CAP]
                        log.debug(f"[step {step}]   -> [vision] {seen[:200]}")
                        archive_id = _retain_research(messages, "screenshot/vision", before, seen_full, secrets, log)
                        suffix = f"\n조사 ID: {archive_id}" if isinstance(archive_id, str) and archive_id.startswith("R") else ""
                        warning = archive_id if isinstance(archive_id, str) and archive_id.startswith("[") else ""
                        results.append(_result(block["id"], f"[비전 모델의 화면 판독 결과]\n{seen}{suffix}"
                                               + (f"\n{warning}" if warning else "")))
                        continue
                    except Exception as e:
                        log.debug(f"[step {step}]   -> vision 실패 ({e}) - 이미지 블록으로 폴백")
                results.append({
                    "type": "tool_result", "tool_use_id": block["id"],
                    "content": [{
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg",
                                   "data": base64.b64encode(jpeg).decode()},
                    }],
                })
                continue

            t_act = time.monotonic()
            cur = getattr(browser, "current_url", None)
            before = cur() if cur else _obs_url(observation)
            label = _label_for(observation, args.get("index", "")) if name in ("click", "type") else ""
            rt_ok, rt_no = [], []  # run_code 실행 중 클릭 직전 검사에서 승인/취소한 버튼 이름
            if name == "run_code" and hasattr(browser, "guard"):
                # 코드만으로는 a.btn_register가 "등록" 버튼인지 모른다 — 클릭 직전 실제 요소 이름으로 묻는다.
                # 코드 전체를 이미 승인했으면(ckey) 다시 묻지 않는다
                asker = _NO if cancelled else io

                def _guard(label, pre=bool(ckey), asker=asker):
                    if pre or label in rt_ok or not _risky(label):
                        return True
                    ask = getattr(asker, "confirm", None) or asker.ask
                    ok = ask(f"스크립트가 '{_clip(label, 40)}' 요소를 클릭하려고 해요. 실행할까요? [y/N]"
                             ).strip().lower() == "y"
                    (rt_ok if ok else rt_no).append(label)
                    return ok
                browser.guard = _guard
            try:
                # 기억한 곳이 지금 페이지와 똑같으면(# 뒤까지) 다시 로드하지 않는다. 보여 달라면(visible) 그래도 간다
                same = ref and not args.get("visible") and [_norm(u) + u.partition("#")[2] for u in (ref[3], before)]
                out = "이미 이 페이지다 — 다시 로드하지 않았다" if same and same[0] == same[1] else browser.act(name, args)
            finally:
                if name == "run_code" and hasattr(browser, "guard"):
                    browser.guard = None
            dt_act = time.monotonic() - t_act
            if rt_no:  # 스크립트 중간 승인 창에서 취소 — 위 승인 취소와 똑같이 바로 끝낸다
                log.brief("취소 — 실행하지 않음")
                results.append(_result(block["id"], out, True))
                for rest in uses[n + 1:]:
                    results.append(_result(rest["id"], "error: 사용자가 취소해 실행하지 않음", True))
                messages.append({"role": "user", "content": results})
                if stop is not None:
                    stop.clear()
                rec["ok"] = True
                return "사용자가 취소함"
            if rt_ok and not ckey:
                ckey = ("run_code", frozenset(rt_ok))  # 승인된 위험 동작 — 결과 확인 전 재실행을 막는다
            if not out.startswith("error:"):
                # 옵션({…})이 보이는 칸에 type = 고르기. 라벨·URL은 관찰 기준, 값은 자리 판단에만 쓴다
                if name == "type":
                    label, opts = _el_label(observation, args.get("index", ""))
                tool = "select" if name == "type" and opts else name
                code = str(args.get("code") or "")
                # 단순한 조작뿐인 스크립트는 그 시점 관찰 기준의 단계로 옮긴다(위험 클릭은 ⚠, 입력값은 자리 판단에만)
                steps = (_code_steps(code, observation) or []) if name == "run_code" else []
                rec["trace"] += [(n, lb, before, bool(d), v) for n, lb, d, v in steps] \
                    or [(tool, label, before, bool(ckey), str(args.get("text") or code))]
                typed += [v for n, _, _, v in steps if n == "type"]
                if name in ("navigate", "new_tab"):
                    if "in background" in out:
                        if not bg_urls and not headless:
                            log.brief("백그라운드에서 작업")
                        u = cur() if cur else str(args.get("url", ""))
                        if u not in bg_urls:
                            bg_urls.append(u)
                    if ref and ref[4]:  # 1단계: 고른 작업의 단계 한 줄(같은 대화에서 보낸 줄은 _mem_block이 뺀다)
                        mem_lines += ref[4].split("\n")
                    if ref:
                        mem_lines += _alias_hint(mem, task, ref[0], rec)
                elif name in ("extract", "run_code") and out.strip():
                    rec["reads"].append(cur() if cur else before)
                if tool == "type":  # 고른 옵션은 입력값이 아니다
                    typed.append(str(args.get("text", "")))
            if name == "extract" and not out.startswith("error:"):
                # navigate+extract를 같은 턴에 묶으면 배치 시작 관찰은 이전 페이지다 —
                # 실행 시점의 실제 URL로 기록해야 새 페이지의 screenshot 게이트가 어긋나지 않는다.
                # (current_url을 안 내주는 브라우저 대역은 기존처럼 관찰 URL로 폴백)
                extracted.add(_page_key(cur()) if cur else _url_key(observation))
            archive_id = None
            if name in ("extract", "run_code") and out.strip() and not out.startswith("error:"):
                archive_id = _retain_research(messages, name, cur() if cur else before,
                                              out, secrets, log)
            cut = 400 if name == "extract" else 100
            log.debug(f"[step {step}]   -> ({dt_act:.1f}s) {out[:cut]}" + (f' on "{label}"' if label else ""))
            if out.startswith("error:"):
                code_fails += name == "run_code"
                log.brief(_fail_brief(out))
            if ckey:
                unverified.add(ckey)
                out += ("\n[시스템: 승인된 위험 동작이 실행됐다. 다시 실행하지 말고 다음 동작으로 extract나 find를 써서 "
                        "결과가 실제로 반영됐는지 확인해라.]" if not out.startswith("error:") else
                        "\n[시스템: 승인된 위험 동작이 오류로 끝났지만 일부 반영됐을 수 있다. 다시 실행하기 전에 "
                        "extract나 find로 실제 반영 여부를 먼저 확인해라. 재시도하면 사용자에게 다시 승인을 묻는다.]")
            elif name in ("extract", "find") and not out.startswith("error:"):
                unverified.clear()
            suffix = f"\n조사 ID: {archive_id}" if isinstance(archive_id, str) and archive_id.startswith("R") else ""
            warning = archive_id if isinstance(archive_id, str) and archive_id.startswith("[") else ""
            results.append(_result(block["id"], out + suffix + (f"\n{warning}" if warning else ""), out.startswith("error:")))
            # extract/find는 페이지를 안 바꾸므로 재관찰이 필요 없다.
            # 실패한 액션은 페이지 상태를 알 수 없어 재관찰한다.
            if name not in ("extract", "find") or out.startswith("error:"):
                need_obs = True
            if out.startswith("error:"):
                # 앞 액션이 페이지를 바꿨을 수 있어 뒤 인덱스는 무효 — 배치 중단
                for rest in uses[n + 1:]:
                    results.append(_result(rest["id"], "error: 이전 액션 실패로 건너뜀", True))
                break

        # tool_result와 새 관찰을 같은 user 메시지에 넣어 role 교대를 유지한다
        _stub_observations(messages)
        if need_obs:
            observation = browser.observe()
            # 작업 중 기억한 사이트에 처음 들어오면(검색으로 찾아온 경우 등) 그 사이트 한 줄만 붙인다
            for site, line in mem.on_page(_obs_url(observation), shown):
                log.brief(f"기억 참고: {site}")
                mem_lines.append(line)
                mem_lines += _alias_hint(mem, task, site, rec)
            # page 없이 기억한 작업의 진입에 도착했어도(요청에 URL이 있던 경우 등) 그 단계 줄을 준다
            for site, task_name, line in mem.arrived(_obs_url(observation)):
                if (site, task_name) not in rec["refs"] + rec["arrived"]:
                    rec["arrived"].append((site, task_name))
                    mem_lines.append(line)
        text = _mem_block(mem_lines, sent)
        if text:
            log.debug(f"[step {step}]   -> [memory] {len(text)}자 참조")
            results.append({"type": "text", "text": text})
        results.append({"type": "text", "text": f"현재 페이지:\n{observation}"})
        messages.append({"role": "user", "content": results})
        if blocked >= BLOCK_CAP:
            summary = _summarize(messages, f"같은 동작 반복 차단이 {blocked}회 누적되어 중단됨", log, start)
            return f"반복 차단 {blocked}회 누적으로 중단. 지금까지의 진행 요약:\n{summary}" if summary \
                else f"반복 차단 {blocked}회 누적으로 중단"

    summary = _summarize(messages, f"스텝 상한({STEP_CAP})에 도달했다", log, start)
    more = "\n\n이어서 하려면 '계속'이라고 입력하세요."
    return (f"스텝 상한({STEP_CAP}) 도달. 지금까지의 진행 요약:\n{summary}" if summary
            else f"스텝 상한({STEP_CAP}) 도달") + more
