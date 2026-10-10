# AGENTS.md: nongside 기여 규칙

## 프로젝트
nongside는 설치된 Edge 또는 Chrome을 Playwright persistent profile로 실행하거나 CDP 브라우저에 연결하고, LLM이 `dom.js`가 수집한 인터랙티브 요소 목록을 바탕으로 도구를 선택해 조작하는 브라우저 에이전트입니다. LLM 호출은 `llm.py`에서 Anthropic Messages API 호환 엔드포인트로 전송합니다. 필수 외부 의존성은 `playwright` 하나이며, 선택 의존성 `pywebview`를 설치하지 않으면 브라우저 앱 창을 UI로 사용합니다. 자세한 사용법은 `README.md`를 참고하세요.

## 디렉터리 맵
```
agent.py             도구 스키마와 에이전트 루프
browser.py           Playwright/CDP 브라우저 제어, dom.js 로드, 페이지 추출
dom.js               페이지 요소 목록 수집
llm.py               시스템 프롬프트와 Messages API 호출
main.py              진입점과 실행 옵션
memory.py            사이트 메모리 저장 및 주입
webui.py             채팅 UI 및 로컬 브리지
requirements.txt     필수 의존성
requirements-ui.txt  선택 UI 의존성 (주석은 ASCII만 사용)
README.md            사용자 문서
AGENTS.md            기여자 및 코딩 에이전트 규칙
LICENSE              MIT 라이선스
.gitignore           공개 저장소 제외 규칙
```

## 규칙
- Git에는 소스와 공개 문서만 추적합니다. `_local/`은 git 미추적 로컬 산출물 폴더로, 사이트 메모리 `sites.json`, 선택 플레이북 `sites.md` 등을 둘 수 있습니다.
- `dom.js`는 `browser.py`와 같은 위치에 둡니다. `browser.py`가 해당 위치에서 파일을 읽습니다.
- 핵심 모듈은 저장소 루트에 유지하고 패키지화하거나 이동하지 않습니다.
- `requirements-ui.txt`의 주석은 Windows pip 호환성을 위해 ASCII 문자만 씁니다.
- 외부 의존성을 늘리지 않습니다. 이 프로젝트는 폐쇄망 반입을 고려합니다.
- 공개 저장소에 없는 테스트 데이터나 내부 환경 설명을 추가하지 않습니다.

## 실행
```sh
python -m venv .venv
# Windows
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
# macOS/Linux
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```
`main.py`는 `--cli`, `--ui`, `--debug`, `--quiet`, `--selftest` 등의 옵션을 지원합니다. 설정은 환경변수로 지정합니다.

환경변수: `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` 또는 `ANTHROPIC_AUTH_TOKEN`, `AGENT_MODEL`, `AGENT_MAX_TOKENS`, `AGENT_VISION_MODEL`, `AGENT_CHANNEL`, `AGENT_PROFILE`, `AGENT_CDP_PORT`, `AGENT_BACKGROUND`, `AGENT_UI_BROWSER`, `AGENT_MEMORY`, `AGENT_LOG`.

## 자체 테스트
```sh
python main.py --selftest
```

커밋 전에 `git status`를 확인해 `.edge-profile*`, `_local/`, `.env`가 추적 대상으로 표시되지 않는지 확인합니다.
