import json
import base64
import queue
import secrets as _secrets
import struct
import sys
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>browser-agent</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='22' fill='%230d0d0d'/%3E%3Cpath d='M33 22V76L45 64L54 82L63 78L54 60H71Z' fill='%23fff'/%3E%3C/svg%3E">
<style>
  :root { --bg:#fff; --fg:#0d0d0d; --muted:#8f8f8f; --bubble:#f4f4f4; --line:#e5e5e5;
          --btn:#0d0d0d; --btn-fg:#fff; --ok:#10a37f; --err:#c95454; --str:#4b6a88; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#212121; --fg:#ececec; --muted:#9b9b9b; --bubble:#303030; --line:#3a3a3a;
            --btn:#ececec; --btn-fg:#0d0d0d; --ok:#19c37d; --err:#e58a8a; --str:#9db4cc; }
  }
  * { box-sizing:border-box; }
  body { margin:0; height:100vh; display:flex; flex-direction:column; background:var(--bg);
         color:var(--fg); font:13px/1.6 -apple-system, BlinkMacSystemFont, system-ui, sans-serif; }
  header { display:flex; justify-content:space-between; align-items:center; padding:12px 16px;
           font-weight:600; font-size:15px; gap:12px; background:var(--bubble);
           border-bottom:1px solid var(--line); }
  .ttl { display:flex; flex-direction:column; min-width:0; line-height:1.3; }
  #task { font-weight:400; font-size:10px; color:var(--muted); white-space:nowrap;
          overflow:hidden; text-overflow:ellipsis; }
  .hr { display:flex; align-items:center; gap:6px; flex:none; }
  header button { padding:4px 12px; font-size:11px; }
  header button svg { width:14px; height:14px; vertical-align:-2px; }
  header button.ic { width:28px; height:28px; padding:0; display:inline-flex; align-items:center; justify-content:center; }
  header button.lb svg { margin-right:4px; }
  #status { font-weight:500; font-size:10px; color:var(--muted); padding:3px 10px;
            border:1px solid var(--line); border-radius:999px; white-space:nowrap; }
  #status.busy { color:var(--ok); border-color:var(--ok); }
  #status.need { background:var(--btn); color:var(--btn-fg); border-color:var(--btn); }
  #status.busy::before { content:""; display:inline-block; width:8px; height:8px; margin-right:6px;
           border-radius:50%; background:var(--ok); animation:p 1s infinite; }
  @keyframes p { 50% { opacity:.2 } }
  #log { flex:1; overflow-y:auto; }
  .col { width:100%; max-width:768px; margin:0 auto; padding:0 24px; }
  .m { white-space:pre-wrap; word-break:break-word; margin:16px 0; }
  .user { margin:24px 0 24px auto; width:fit-content; max-width:70%; background:var(--bubble);
          padding:10px 18px; border-radius:22px; }
  .error { color:var(--err); }
  .error::before { content:"⚠ "; }
  details { color:var(--muted); font-size:12px; margin:8px 0; }
  summary { cursor:pointer; list-style:none; }
  summary::after { content:" ›"; }
  details[open] summary::after { content:" ⌄"; }
  details ol { margin:6px 0; padding-left:20px; }
  details.fail > summary { color:var(--err); }
  li.fail { color:var(--err); }
  li.fail::before { content:"⚠ "; }
  li details, .confirm details { margin:2px 0 4px; font-size:10px; }
  li pre, .confirm details pre { margin:4px 0 0; padding:8px 10px; background:var(--bubble); border:1px solid var(--line);
           border-radius:8px; overflow-x:auto; color:var(--fg);
           font:10px/1.5 ui-monospace, Menlo, Consolas, monospace; }
  /* 코드 하이라이트: 색은 최소로 — 키워드 굵게, 문자열 옅은 청회색, 주석 흐리게 */
  .hk { font-weight:600; }
  .hs { color:var(--str); }
  .hc { color:var(--muted); font-style:italic; }
  .card { border:2px solid var(--fg); background:var(--bubble); border-radius:16px;
          padding:14px 16px; margin:8px 0 16px; }
  #pending .card { max-height:45vh; overflow-y:auto; }  /* 선택지·코드가 길어도 대화 영역을 남긴다 */
  .card .ch { font-weight:600; font-size:11px; margin-bottom:4px; }
  .card .row { flex-wrap:wrap; }
  .card .row.list { flex-direction:column; align-items:stretch; }
  .card .row.list button { text-align:left; border-radius:10px; }
  .card button.cancel { color:var(--err); border-color:var(--err); }
  .m.ask, .m.confirm, .m.fields { border-left:3px solid var(--fg); padding-left:12px; }
  .card input { display:block; width:100%; margin:8px 0; padding:10px 12px; font:inherit;
                color:var(--fg); background:var(--bg); border:1px solid var(--line); border-radius:10px; }
  .card input:focus { outline:2px solid var(--muted); outline-offset:-1px; }
  .row { display:flex; gap:8px; margin-top:12px; }
  .card > .row:first-child { margin-top:0; }
  button { font:inherit; font-size:12px; padding:8px 16px; border-radius:999px; cursor:pointer;
           border:1px solid var(--line); background:var(--bg); color:var(--fg); }
  button:disabled { opacity:.4; cursor:default; }
  #yes, #send, #stop, .card .pri { background:var(--btn); color:var(--btn-fg); border-color:var(--btn); }
  .composer { margin:8px 0; display:flex; align-items:center; gap:8px; background:var(--bubble);
              border-radius:28px; padding:8px 8px 8px 20px; }
  #in { flex:1; border:0; background:none; font:inherit; color:var(--fg); min-width:0;
        resize:none; max-height:160px; padding:6px 0; }
  #in:focus { outline:none; }
  .composer:focus-within { box-shadow:0 0 0 2px var(--muted); }
  #send, #stop { width:36px; height:36px; padding:0; border-radius:50%; flex:none;
                 display:inline-flex; align-items:center; justify-content:center;
                 transition:opacity .15s, transform .1s; }
  #send[hidden], #stop[hidden] { display:none; }
  #send:not(:disabled):hover, #stop:hover { opacity:.85; }
  #send:not(:disabled):active, #stop:active { transform:scale(.94); }
  #send:focus-visible, #stop:focus-visible { outline:2px solid var(--muted); outline-offset:2px; }
  #send:disabled { opacity:.3; }
  .jumpwrap { position:relative; height:0; }
  #jump { position:absolute; left:50%; bottom:10px; transform:translateX(-50%); z-index:2;
          display:inline-flex; align-items:center; gap:6px; padding:6px 14px; font-size:11px;
          background:var(--btn); color:var(--btn-fg); border-color:var(--btn);
          box-shadow:0 2px 8px rgba(0,0,0,.25); }
  #jump[hidden] { display:none; }
  #jump svg { width:14px; height:14px; }
  #tabbar { margin:8px 0 0; }
  #tabchip, #taboff { display:flex; align-items:center; gap:8px; font-size:11px; }
  #tabchip[hidden], #taboff[hidden], #tabfav[hidden] { display:none; }
  #tabchip { width:fit-content; max-width:100%; background:var(--bubble); border:1px solid var(--line);
             border-radius:12px; padding:4px 4px 4px 10px; }
  #tabchip .tt { display:flex; flex-direction:column; min-width:0; }
  #tabttl, #taburl { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:70vw; }
  #taburl { color:var(--muted); font-size:10px; }
  #tabpin { color:var(--muted); font-size:10px; border:1px solid var(--line); border-radius:4px; padding:0 4px; white-space:nowrap; }
  #tabpin[hidden] { display:none; }
  #tabfav { width:16px; height:16px; flex:none; }
  #tabx { width:24px; height:24px; padding:0; flex:none; border:0; background:none; font-size:16px; line-height:1; }
  #tabx:hover { background:var(--line); }
  #tabx:focus-visible, #tabback:focus-visible { outline:2px solid var(--muted); outline-offset:2px; }
  #taboff { color:var(--muted); }
  #tabback { padding:2px 10px; font-size:11px; }
  .hint { text-align:center; font-size:10px; color:var(--muted); padding-bottom:12px; }
  .md { white-space:normal; }
  .user.md > :last-child { margin-bottom:0; }
  .md p, .md ul, .md ol, .md pre, .md table { margin:0 0 10px; }
  .md h3, .md h4, .md h5, .md h6 { margin:14px 0 6px; font-size:14px; }
  .md ul, .md ol { padding-left:22px; }
  .md code { background:var(--bubble); padding:1px 5px; border-radius:6px;
             font:11px ui-monospace, Menlo, Consolas, monospace; }
  .md pre { background:var(--bubble); padding:12px; border-radius:10px; overflow-x:auto; }
  .md pre code { padding:0; background:none; }
  .md table { border-collapse:collapse; display:block; overflow-x:auto; }
  .md th, .md td { border:1px solid var(--line); padding:4px 10px; text-align:left; }
  .md a { color:inherit; }
  .shot { margin:8px 0; }
  .shot img { display:block; max-width:220px; max-height:160px; border:1px solid var(--line);
              border-radius:10px; cursor:zoom-in; }
  .shot figcaption { font-size:10px; color:var(--muted); margin-top:2px; }
  #cfgdlg { border:1px solid var(--line); border-radius:16px; background:var(--bg); color:var(--fg);
            padding:16px 18px; width:min(92vw, 560px); }
  #cfgdlg::backdrop { background:rgba(0,0,0,.45); }
  #cfgdlg h2 { margin:0 0 10px; font-size:14px; }
  #cfgdlg table { border-collapse:collapse; width:100%; font-size:11px; }
  #cfgdlg th, #cfgdlg td { text-align:left; vertical-align:top; padding:6px 8px; border-bottom:1px solid var(--line); }
  #cfgdlg th { white-space:nowrap; font-weight:600; }
  #cfgdlg td.v { word-break:break-all; font-family:ui-monospace, Menlo, Consolas, monospace; }
  #cfgdlg td.s { color:var(--muted); font-size:10px; word-break:break-all; }
  #cfgdlg .note { font-size:10px; color:var(--muted); margin:10px 0 0; }
  #cfgdlg .row { justify-content:flex-end; }
</style>
<header>
  <div class="ttl">browser-agent<span id="task"></span></div>
  <div class="hr">
    <span id="status" aria-live="polite">대기</span>
    <button id="bell" aria-label="알림 소리" aria-pressed="true" title="알림 소리 켜기/끄기">🔔</button>
    <button id="cfg" class="ic" aria-label="현재 설정 보기" title="현재 설정 보기"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg></button>
    <button id="new" class="lb" aria-label="새 채팅 시작" title="새 채팅 시작"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.4 2.6a1 1 0 0 1 3 3l-9 9a2 2 0 0 1-.85.5l-2.87.84a.5.5 0 0 1-.62-.62l.84-2.87a2 2 0 0 1 .5-.85z"/></svg>새 채팅</button>
  </div>
</header>
<dialog id="cfgdlg" aria-labelledby="cfgttl">
  <h2 id="cfgttl">현재 설정</h2>
  <table><thead><tr><th scope="col">항목</th><th scope="col">값</th><th scope="col">출처</th></tr></thead>
  <tbody id="cfgrows"></tbody></table>
  <p class="note">읽기 전용입니다. 바꾸려면 .env·환경변수·~/.claude/settings.json을 고치고 다시 실행하세요.</p>
  <div class="row"><button id="cfgclose" class="pri">닫기</button></div>
</dialog>
<div id="log" aria-label="대화 로그"><div class="col" id="msgs"></div></div>
<div class="col">
  <div class="jumpwrap"><button id="jump" hidden aria-live="polite"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14"/><path d="M19 12l-7 7-7-7"/></svg><span id="jumptxt">아래에 새 메시지</span></button></div>
  <div id="pending"></div>
  <div id="tabbar">
    <div id="tabchip" role="group" aria-label="작업할 탭" hidden>
      <img id="tabfav" alt="" hidden>
      <span class="tt"><span id="tabttl"></span><span id="taburl"></span></span>
      <span id="tabpin" hidden>이 대화의 탭</span>
      <button id="tabx" aria-label="현재 탭 사용 안 함 (보이지 않는 백그라운드에서 작업)" title="현재 탭 사용 안 함 — 보이지 않는 백그라운드 브라우저에서 작업">×</button>
    </div>
    <div id="taboff" hidden><span>탭 없이 작업</span><button id="tabback">현재 탭 사용</button></div>
  </div>
  <div class="composer">
    <textarea id="in" rows="1" placeholder="무엇을 해 드릴까요?" aria-label="작업 또는 답변 입력 (Alt+Enter 줄바꿈)" autofocus></textarea>
    <button id="send" aria-label="전송" title="전송 (Enter)" disabled><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg></button>
    <button id="stop" aria-label="작업 중단" title="작업 중단" hidden><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><rect width="12" height="12" rx="2.5" fill="currentColor"/></svg></button>
  </div>
  <div class="hint">위험한 동작은 실행 전에 확인을 받습니다.</div>
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get("t") || "";
// BRIDGE: pywebview 창 — HTTP 대신 window.pywebview.api로 파이썬을 직접 부른다(포트 없음)
const BRIDGE = false;
const IMAGE_PAGE = __IMAGE_PAGE__;   // 스크린샷 새 창(HTTP 모드) 페이지 — 파이썬 IMAGE_PAGE를 넣는다
let since = 0, pending = null, busy = false, steps = null, epoch = null, task = "", first = true, rev = -1;
let sound = true, actx = null;
let tab = null, tabOff = false;   // 사용자 탭 칩 {title,url,icon} / 이번 요청에서 x로 뺐는지
try { sound = localStorage.getItem("snd") !== "0"; } catch (e) {}
const log = document.getElementById("log"), msgs = document.getElementById("msgs"),
      inp = document.getElementById("in"),
      sendBtn = document.getElementById("send"), stopBtn = document.getElementById("stop"),
      statusEl = document.getElementById("status"), pendEl = document.getElementById("pending"),
      taskEl = document.getElementById("task"), bell = document.getElementById("bell");

// 알림: 결과·질문·확인이 새로 오면 짧은 비프음 + 창이 뒤에 있으면 제목 표시
function ding() {
  if (document.hidden) document.title = "● 확인 필요 — browser-agent";
  if (!sound) return;
  try {
    actx = actx || new AudioContext();
    const o = actx.createOscillator(), g = actx.createGain(), t0 = actx.currentTime;
    o.frequency.value = 880;
    g.gain.setValueAtTime(0.15, t0);
    g.gain.exponentialRampToValueAtTime(0.001, t0 + 0.35);
    o.connect(g).connect(actx.destination);
    o.start(); o.stop(t0 + 0.35);
  } catch (e) {}
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) document.title = "browser-agent"; });
function renderBell() {
  bell.textContent = sound ? "🔔" : "🔕";
  bell.setAttribute("aria-pressed", String(sound));
}
bell.onclick = () => {
  sound = !sound;
  try { localStorage.setItem("snd", sound ? "1" : "0"); } catch (e) {}
  renderBell(); if (sound) ding();
};
document.getElementById("new").onclick = () => { inp.value = ""; post("/reset", {}); };

// 결과용 최소 마크다운: 제목·목록·표·코드·굵게·링크. 먼저 escape해서 HTML 주입을 막는다.
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const inl = (s) => esc(s)
  .replace(/`([^`]+)`/g, "<code>$1</code>")
  .replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>")
  .replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
const LI = /^\\s*([-*]|\\d+\\.)\\s+/, ROW = /^\\|.*\\|\\s*$/;
function md(src) {
  const L = src.split("\\n"), out = [];
  let i = 0, m;
  while (i < L.length) {
    const l = L[i];
    if (/^```/.test(l)) {
      const b = []; i++;
      while (i < L.length && !/^```/.test(L[i])) b.push(L[i++]);
      i++; out.push("<pre><code>" + esc(b.join("\\n")) + "</code></pre>");
    } else if ((m = l.match(/^(#{1,4})\\s+(.*)/))) {
      const n = m[1].length + 2; out.push(`<h${n}>${inl(m[2])}</h${n}>`); i++;
    } else if (LI.test(l)) {
      const t = /^\\s*\\d/.test(l) ? "ol" : "ul", it = [];
      while (i < L.length && LI.test(L[i])) it.push("<li>" + inl(L[i++].replace(LI, "")) + "</li>");
      out.push(`<${t}>${it.join("")}</${t}>`);
    } else if (ROW.test(l) && /^\\|[\\s:|-]+\\|\\s*$/.test(L[i + 1] || "")) {
      const row = (r, t) => "<tr>" + r.trim().slice(1, -1).split("|")
        .map((c) => `<${t}>${inl(c.trim())}</${t}>`).join("") + "</tr>";
      const rows = [row(L[i], "th")]; i += 2;
      while (i < L.length && ROW.test(L[i])) rows.push(row(L[i++], "td"));
      out.push("<table>" + rows.join("") + "</table>");
    } else if (!l.trim()) {
      i++;
    } else {
      const p = [inl(L[i++])];
      while (i < L.length && L[i].trim() && !/^(```|#{1,4}\\s|\\|)/.test(L[i]) && !LI.test(L[i])) p.push(inl(L[i++]));
      out.push("<p>" + p.join("<br>") + "</p>");
    }
  }
  return out.join("");
}

// 최소 파이썬 하이라이트(주석·문자열·키워드). 토큰 사이 텍스트도 escape해서 HTML 주입을 막는다
const HL = /(#.*)|("{3}[\\s\\S]*?"{3}|'{3}[\\s\\S]*?'{3}|"(?:\\\\.|[^"\\\\\\n])*"|'(?:\\\\.|[^'\\\\\\n])*')|\\b(def|return|if|elif|else|for|while|in|not|and|or|is|None|True|False|try|except|finally|with|as|lambda|pass|break|continue|raise|await|async|class|import|from)\\b/g;
function hl(el) {
  const s = el.textContent;
  let out = "", last = 0, m;
  HL.lastIndex = 0;
  while ((m = HL.exec(s))) {
    out += esc(s.slice(last, m.index)) + '<span class="' + (m[1] ? "hc" : m[2] ? "hs" : "hk") + '">' + esc(m[0]) + "</span>";
    last = HL.lastIndex;
  }
  el.innerHTML = out + esc(s.slice(last));
}

// 단계 묶음 제목: "1분 14초 작업함" — 첫 단계부터 묶음이 닫힌(다음 메시지) 시각까지
function stepsTitle(end, failed = false) {
  const sec = Math.max(0, Math.round(end - steps.t0)), m = Math.floor(sec / 60);
  // 묶음 뒤에 온 최종 메시지가 오류일 때만 실패로 표시한다. 중간 단계 실패는 제목에 안 적는다
  steps.classList.toggle("fail", failed);
  steps.firstChild.textContent = (m ? m + "분 " : "") + (sec % 60) + "초 작업함" + (failed ? " · 실패" : "");
}
// 작업 중에는 열린 묶음의 초를 1초마다 올린다 (서버와 같은 PC라 시계 차이는 무시)
setInterval(() => { if (steps && busy && !pending) stepsTitle(Date.now() / 1000); }, 1000);

// 스크롤: 맨 아래 근처를 보고 있으면 새 메시지를 따라 내려가고, 위를 읽는 중이면 위치를 두고 버튼으로 알린다
const jumpBtn = document.getElementById("jump"), jumpTxt = document.getElementById("jumptxt");
function nearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 80; }
function toBottom() { log.scrollTop = log.scrollHeight; atBottom = true; jumpBtn.hidden = true; }
jumpBtn.onclick = toBottom;
// atBottom: 마지막으로 본 위치가 맨 아래였는지. 확인 카드가 뜨거나 입력창이 커져 대화 영역이 줄어도
// scrollTop은 그대로라 마지막 메시지가 가려진다 — 크기가 바뀌면 맨 아래를 다시 맞춘다
let atBottom = true;
// 위로 굴렸을 때만 "읽는 중"으로 본다. 카드가 떠 대화 영역만 줄어든 경우(scrollTop 그대로)는 맨 아래 유지
let lastTop = 0;
log.addEventListener("scroll", () => {
  if (log.scrollTop < lastTop - 2) atBottom = nearBottom();
  else if (nearBottom()) atBottom = true;
  lastTop = log.scrollTop;
  if (atBottom) jumpBtn.hidden = true;
});
new ResizeObserver(() => { if (atBottom) log.scrollTop = log.scrollHeight; }).observe(log);

function add(kind, text, t = Date.now() / 1000, ev = {}) {
  const stick = first || kind === "user" || nearBottom();
  if (kind === "step") {           // 연속된 단계는 접힌 한 줄로 묶는다
    if (!steps) {
      const d = document.createElement("details");
      d.append(document.createElement("summary"), document.createElement("ol"));
      msgs.appendChild(d);
      steps = d; steps.t0 = t;
    }
    const li = document.createElement("li");
    const [head, ...body] = text.replace(/^· /, "").split("\\n");
    li.textContent = head;
    if (body.length) {             // 스크립트 코드: 작게, 기본 접힘
      const c = document.createElement("details"), s = document.createElement("summary"),
            p = document.createElement("pre"), code = document.createElement("code");
      s.textContent = "코드 보기"; code.textContent = body.join("\\n");
      hl(code);
      p.appendChild(code); c.append(s, p); li.appendChild(c);
    }
    if (head.startsWith("실패")) li.className = "fail";
    steps.lastChild.appendChild(li);
    stepsTitle(t);
  } else if (kind === "image") {   // 스크린샷 미리보기: 작은 썸네일, 클릭/Enter로 크게
    if (steps) stepsTitle(t);
    steps = null;
    if (!String(ev.src || "").startsWith("data:image/")) {   // 오래돼 원본을 버린 스크린샷은 캡션만
      const c = document.createElement("div");
      c.className = "shot";
      c.textContent = "📷 " + (text || "화면 확인") + " (이전 스크린샷 — 원본 없음)";
      msgs.appendChild(c);
      return;
    }
    const f = document.createElement("figure"), im = document.createElement("img"),
          c = document.createElement("figcaption");
    f.className = "shot";
    im.src = ev.src; im.alt = "스크린샷: " + (text || "화면 확인"); im.tabIndex = 0;
    im.title = "클릭하면 새 창에서 크게 보기";
    const open = () => openImage(ev.src, text || "화면 확인");
    im.onclick = open;
    im.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
    c.textContent = "📷 " + (text || "화면 확인");
    f.append(im, c);
    msgs.appendChild(f);
  } else {
    if (steps) stepsTitle(t, kind === "error");
    steps = null;
    const d = document.createElement("div");
    d.className = "m " + kind;
    const body = text.replace(/^\\[(result|error)\\] ?/, "").replace(/\\s*\\[y\\/N\\]$/, "");
    if (kind === "result" || kind === "confirm" || kind === "user") {
      d.classList.add("md"); d.innerHTML = md(body);
      d.querySelectorAll("pre code").forEach(hl);
      if (kind === "confirm") d.querySelectorAll("pre").forEach((p) => {  // 승인 창 코드도 기본 접힘
        const c = document.createElement("details"), s = document.createElement("summary");
        s.textContent = "코드 보기 (" + p.textContent.split("\\n").length + "줄)";
        p.replaceWith(c); c.append(s, p);
      });
    } else d.textContent = body;
    msgs.appendChild(d);
  }
  if (stick) toBottom();
  else if (kind !== "step") {   // 단계 줄은 조용히 쌓고, 답변·질문·확인이 오면 버튼을 띄운다
    jumpTxt.textContent = ["ask", "confirm", "fields"].includes(kind) ? "아래에 확인 요청"
                          : kind === "result" ? "아래에 새 답변" : "아래에 새 메시지";
    jumpBtn.hidden = false;
  }
}

// 스크린샷을 별도 창으로 연다. 브리지면 파이썬이 pywebview 창을 새로 만들고, HTTP 모드면 팝업 창
function openImage(src, caption) {
  if (BRIDGE) return window.pywebview.api.open_image(src, caption);
  const w = window.open("", "_blank", "width=1100,height=760");
  if (!w) return;
  w.document.write(IMAGE_PAGE);
  w.document.close();
  w.document.title = "스크린샷 — " + caption;
  w.document.getElementById("img").src = src;
}

async function post(path, body) {
  if (BRIDGE) return window.pywebview.api.call(path, body);
  await fetch(path, { method: "POST",
    headers: { "content-type": "application/json", "x-token": TOKEN },
    body: JSON.stringify(body) });
}

function renderPending() {
  pendEl.textContent = "";
  inp.placeholder = pending && pending.kind === "ask" ? "답변 입력" : "무엇을 해 드릴까요?";
  if (!pending) return;
  const d = document.createElement("div");
  d.className = "card"; d.setAttribute("role", "group");
  const h = document.createElement("div");
  h.className = "ch";
  h.textContent = pending.kind === "confirm" ? "실행 확인이 필요합니다" : "답변이 필요합니다";
  d.appendChild(h);
  const row = document.createElement("div");
  row.className = "row";
  const cancel = document.createElement("button");
  cancel.className = "cancel"; cancel.textContent = "취소"; cancel.setAttribute("aria-label", "입력 취소");
  cancel.onclick = () => { add("user", "(취소)"); post("/answer", { cancel: true }); };
  if (pending.kind === "confirm") {
    d.setAttribute("aria-label", "실행 확인");
    const yes = document.createElement("button"), no = document.createElement("button");
    yes.id = "yes"; yes.textContent = "실행"; yes.setAttribute("aria-label", "실행 확인");
    no.id = "no"; no.className = "cancel"; no.textContent = "취소"; no.setAttribute("aria-label", "취소");
    yes.onclick = () => { add("user", "실행"); post("/answer", { text: "y" }); };
    no.onclick  = () => { add("user", "취소"); post("/answer", { text: "n" }); };
    row.append(yes, no);
  } else if (pending.kind === "ask") {
    d.setAttribute("aria-label", "답변 요청");
    row.classList.add("list");   // 선택지는 항상 세로 목록
    for (const o of (pending.options || []).slice(0, 5)) {   // 선택지는 최대 5개
      const b = document.createElement("button");
      b.textContent = o;
      b.onclick = () => { add("user", o); post("/answer", { text: o }); };
      row.append(b);
    }
    const other = document.createElement("button");
    other.textContent = (pending.options || []).length ? "기타 (직접 입력)" : "직접 입력";
    other.onclick = () => { inp.placeholder = "직접 입력 후 Enter"; inp.focus(); };
    row.append(other, cancel);
  } else if (pending.kind === "fields") {
    d.setAttribute("aria-label", "입력 요청");
    const inputs = [];
    for (const f of pending.fields || []) {
      const i = document.createElement("input");
      i.type = f.secret ? "password" : "text";
      i.placeholder = f.name;
      i.setAttribute("aria-label", f.name);
      d.appendChild(i);
      inputs.push([f.name, f.secret, i]);
    }
    const b = document.createElement("button");
    b.className = "pri"; b.textContent = "전송"; b.setAttribute("aria-label", "답변 전송");
    b.onclick = () => {
      const lines = [], echo = [];
      for (const [name, secret, i] of inputs) {
        lines.push(name + ": " + i.value);
        echo.push(name + ": " + (secret && i.value ? "****" : i.value));
      }
      add("user", echo.join("\\n"));
      post("/answer", { text: lines.join("\\n") });
    };
    row.append(b, cancel);
  }
  d.appendChild(row);
  pendEl.appendChild(d);
}

function renderStatus() {
  statusEl.textContent = pending ? "입력 필요" : busy ? "작업 중" : "대기";
  statusEl.className = pending ? "need" : busy ? "busy" : "";
  taskEl.textContent = task;
  taskEl.title = task;
  const waiting = busy && !pending;
  stopBtn.hidden = !waiting;
  sendBtn.hidden = waiting;
  inp.disabled = waiting;
  syncSend();
}

// 탭 칩: 칩이 보이면 그 탭에서, x로 닫으면 보이지 않는 백그라운드 브라우저에서 작업한다
const tabChip = document.getElementById("tabchip"), tabOffEl = document.getElementById("taboff"),
      tabFav = document.getElementById("tabfav"), tabTtl = document.getElementById("tabttl"),
      tabUrl = document.getElementById("taburl");
function renderTab() {
  const off = !!(tab && tab.off);   // 탭 없이 시작한 대화: 새 채팅 전까지 고정
  tabChip.hidden = !tab || tabOff || off;
  tabOffEl.hidden = !(tab && (tabOff || off));
  document.getElementById("tabback").hidden = off;
  if (!tab || off) return;
  tabTtl.textContent = tab.title || tab.url;
  tabUrl.textContent = tab.url;
  tabChip.title = (tab.title ? tab.title + " — " : "") + tab.url
                  + (tab.pinned ? " (이 대화는 이 탭에서 이어집니다. 다른 탭은 새 채팅에서 고르세요)" : "");
  document.getElementById("tabpin").hidden = !tab.pinned;
  const icon = tab.icon || "";
  if (/^(https?:|data:image[/])/i.test(icon)) {
    if (tabFav.getAttribute("src") !== icon) tabFav.src = icon;
    tabFav.hidden = false;
  } else tabFav.hidden = true;
}
tabFav.onerror = () => { tabFav.hidden = true; };
document.getElementById("tabx").onclick = () => { tabOff = true; renderTab(); document.getElementById("tabback").focus(); };
document.getElementById("tabback").onclick = () => { tabOff = false; renderTab(); inp.focus(); };

async function poll() {
  let delay = BRIDGE ? 0 : 700;
  try {
    let j = null;
    if (BRIDGE) {           // 롱폴: 새 이벤트·상태 변화가 생기면 파이썬이 바로 돌려준다(최대 20초 대기)
      j = await window.pywebview.api.poll(since, first ? 0 : 20, rev);
    } else {
      const r = await fetch("/poll?since=" + since, { headers: { "x-token": TOKEN } });
      if (r.ok) j = await r.json();
    }
    if (j) {
      if (epoch !== null && j.epoch !== epoch) {   // 새 채팅: 화면을 비우고 처음부터 다시 받는다
        epoch = j.epoch; since = 0; steps = null; msgs.textContent = ""; tabOff = false; renderTab();
        setTimeout(poll, 0);
        return;
      }
      epoch = j.epoch;
      since = j.next;
      if (j.rev !== undefined) rev = j.rev;
      const changed = JSON.stringify(pending) !== JSON.stringify(j.pending) || busy !== j.busy
                      || task !== j.task;
      pending = j.pending; busy = j.busy; task = j.task;
      const nt = j.tab || null;
      if (JSON.stringify(nt) !== JSON.stringify(tab)) {
        // 서버가 대화 전체를 탭 없이로 고정했거나, 사용자가 다른 탭으로 옮겼다(새 탭은 다시 쓸 수 있게)
        if (nt && (nt.off || !tab || nt.url !== tab.url)) tabOff = false;
        tab = nt; renderTab();
      }
      let alert = false;
      for (const e of j.events) {
        add(e.kind, e.text, e.t, e);
        if (["result", "ask", "confirm", "fields"].includes(e.kind)) alert = true;
      }
      if (alert && !first) ding();   // 첫 poll은 새로고침 복원이라 울리지 않는다
      first = false;
      if (changed) { renderPending(); renderStatus(); if (atBottom) toBottom(); }
    }
  } catch (e) { delay = 700; }
  setTimeout(poll, delay);
}

async function send() {
  const t = inp.value.trim();
  if (!t) return;
  try { actx = actx || new AudioContext(); } catch (e) {}  // 사용자 조작 시점에 오디오 허용
  inp.value = "";
  grow();
  if (pending && pending.kind === "ask") {
    add("user", t);
    await post("/answer", { text: t });
  } else if (!busy) {
    add("user", t);
    const useTab = !tabOff;
    // 고정 탭이 있는 대화에서 x는 이번 요청에만 적용된다. 첫 요청이면 서버가 대화 전체를 탭 없이로 고정한다
    if (tab && tab.pinned) { tabOff = false; renderTab(); }
    await post("/task", { task: t, use_tab: useTab, tab_url: useTab && tab ? tab.url : "" });
  }
}

sendBtn.onclick = send;
stopBtn.onclick = () => post("/stop", {});
// Enter 전송, Alt/Shift+Enter 줄바꿈 (textarea 기본 동작은 Shift+Enter만 줄바꿈이라 Alt는 직접 넣는다)
function grow() { inp.style.height = "auto"; inp.style.height = inp.scrollHeight + "px"; syncSend(); }
// 입력이 비었거나 작업 중(답변 대기 아님)이면 전송 버튼을 흐리게 막는다
function syncSend() { sendBtn.disabled = !inp.value.trim() || (busy && !pending); }
inp.oninput = grow;
inp.onkeydown = (e) => {
  if (e.key !== "Enter" || e.isComposing) return;
  if (e.altKey) { e.preventDefault(); inp.setRangeText("\\n", inp.selectionStart, inp.selectionEnd, "end"); grow(); }
  else if (!e.shiftKey) { e.preventDefault(); send(); }
};
renderBell(); renderPending(); renderStatus();

// 설정 보기: 열 때마다 파이썬에서 현재 값을 다시 받는다(값은 textContent로만 넣는다)
const cfgDlg = document.getElementById("cfgdlg"), cfgRows = document.getElementById("cfgrows");
async function loadSettings() {
  let rows = [];
  try {
    if (BRIDGE) rows = (await window.pywebview.api.settings()).rows || [];
    else {
      const r = await fetch("/settings", { headers: { "x-token": TOKEN } });
      if (r.ok) rows = (await r.json()).rows || [];
    }
  } catch (e) {}
  cfgRows.textContent = "";
  if (!rows.length) rows = [{ name: "-", value: "설정을 읽지 못했습니다", source: "" }];
  for (const x of rows) {
    const tr = document.createElement("tr"), th = document.createElement("th"),
          v = document.createElement("td"), s = document.createElement("td");
    th.scope = "row"; th.textContent = x.name; v.className = "v"; v.textContent = x.value;
    s.className = "s"; s.textContent = x.source;
    tr.append(th, v, s); cfgRows.appendChild(tr);
  }
}
document.getElementById("cfg").onclick = async () => { await loadSettings(); cfgDlg.showModal(); };
document.getElementById("cfgclose").onclick = () => cfgDlg.close();
if (!BRIDGE) poll();
else if (window.pywebview && window.pywebview.api) poll();
else window.addEventListener("pywebviewready", poll, { once: true });
</script>
"""

# 스크린샷 새 창 페이지. 기본은 창에 맞춤, 클릭하면 원본 크기(스크롤) <-> 맞춤 전환. Esc로 닫기(팝업만)
IMAGE_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>스크린샷</title>
<style>
  html, body { margin:0; height:100%; background:#111; }
  body { display:flex; align-items:center; justify-content:center; overflow:auto; }
  img { max-width:100vw; max-height:100vh; object-fit:contain; cursor:zoom-in; }
  body.full { display:block; }
  body.full img { max-width:none; max-height:none; cursor:zoom-out; }
</style>
<img id="img" alt="스크린샷" tabindex="0">
<script>
  const im = document.getElementById("img");
  const toggle = () => document.body.classList.toggle("full");
  im.onclick = toggle;
  document.onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    else if (e.key === "Escape") window.close();
  };
</script>
"""
# HTML 안 <script>에 문자열로 넣으므로 "</"를 끊어 둔다
HTML = HTML.replace("__IMAGE_PAGE__", json.dumps(IMAGE_PAGE).replace("</", "<\\/"), 1)

# pywebview 창용: 같은 페이지를 브리지 모드로 (html= 로 직접 로드, HTTP 서버 없음)
HTML_BRIDGE = HTML.replace("const BRIDGE = false;", "const BRIDGE = true;", 1)
assert HTML_BRIDGE != HTML

# 사용자가 ask/fields 입력을 취소했을 때 에이전트에 돌려줄 답
ANSWER_CANCEL = ("[사용자가 입력을 취소함] 이 정보 없이 진행할 수 있으면 계속하고, "
                 "아니면 done으로 상황을 보고해라.")
# 이 시간 동안 poll이 없으면 UI 창이 닫힌 것으로 본다. 숨은 창의 타이머는 크롬 집중 스로틀링으로
# 1분에 한 번까지 늦어질 수 있어 그보다 길게 잡는다
UI_GONE_SEC = 150
# 원본을 보관하는 최근 스크린샷 수. 오래된 것은 캡션만 남긴다(새로고침 시 재전송량·메모리 제한)
MAX_IMAGES = 10


class WebIO:
    """agent의 say/ask/confirm을 웹 채팅으로 연결한다. CLI의 print/input 대응."""

    def __init__(self):
        self._cond = threading.Condition()
        self._rev = 0                 # busy/task/pending이 바뀔 때마다 증가 — 롱폴이 깨어난다
        self._busy = False
        self._task = ""
        self.events = []
        self.tasks = queue.Queue()
        self.secrets = set()          # secret 필드 값 — 결과 로그에서 가리기 위함
        self.stop_requested = threading.Event()
        self._answer = None
        self._pending = None          # {"kind", "text", "fields"} — 새로고침 복원용
        self.last_poll = time.time()  # UI 창이 살아 있는지 — 창을 닫으면 poll이 끊긴다
        self.epoch = 0                # 새 채팅마다 증가 — UI가 화면을 비운다
        self.new_session = threading.Event()  # 메인 루프가 대화 기록을 비우라는 신호
        self._drop = False            # 새 채팅 후 중단 중인 이전 작업의 출력은 버린다
        # 질문·확인 대기 시작(kind) / 끝(None)에 불린다 — 창 복원·작업표시줄 깜빡임 등
        self.on_attention = None
        # 설정 화면용 [{name, value, source}]를 돌려주는 함수 — main이 연결한다
        self.settings = lambda: []
        # 작업을 실행할 수 없는 이유(브라우저 시작 실패 등). 설정되면 /task를 받지 않고 오류로 알린다
        self.unavailable = None
        self._tab = None  # 사용자가 보는 탭 {title, url, icon} — 챗 창 탭 칩

    def _bump(self):
        with self._cond:
            self._rev += 1
            self._cond.notify_all()

    @property
    def busy(self):
        """작업 실행 중 — UI가 중단 버튼을 보여준다."""
        return self._busy

    @busy.setter
    def busy(self, v):
        self._busy = bool(v)
        self._bump()

    @property
    def task(self):
        """현재(마지막) 작업 — 헤더에 표시."""
        return self._task

    @task.setter
    def task(self, v):
        self._task = str(v)
        self._bump()

    @property
    def tab(self):
        return self._tab

    @tab.setter
    def tab(self, v):
        if v != self._tab:  # 바뀔 때만 롱폴을 깨운다
            self._tab = v
            self._bump()

    @property
    def pending_kind(self):
        p = self._pending
        return p["kind"] if p else None

    def _attention(self, kind):
        if self.on_attention:
            try:
                self.on_attention(kind)
            except Exception:
                pass

    def say(self, text):
        t = str(text)
        kind = ("result" if t.startswith("[result]")
                else "error" if t.startswith("[error]")
                else "step" if t.startswith("· ")
                else "msg")
        with self._cond:
            if self._drop:
                return
            self.events.append({"kind": kind, "text": t, "t": time.time()})
            self._cond.notify_all()

    def image(self, jpeg, caption=""):
        """스크린샷 미리보기(JPEG bytes). 채팅에 썸네일로 보인다. CLI에는 이 메서드가 없다."""
        src = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        with self._cond:
            if self._drop:
                return
            self.events.append({"kind": "image", "text": str(caption), "src": src, "t": time.time()})
            imgs = [e for e in self.events if e["kind"] == "image" and e.get("src")]
            for e in imgs[:-MAX_IMAGES]:
                e.pop("src", None)
            self._cond.notify_all()

    def _wait_answer(self, kind, question, fields=None, options=None):
        with self._cond:
            self._answer = None
            self._pending = {"kind": kind, "text": str(question),
                             "fields": fields or [], "options": options or []}
            ev = {"kind": kind, "text": str(question), "t": time.time()}
            if fields:
                ev["fields"] = fields  # secret 값은 아직 없다 — name/secret 플래그뿐
            self.events.append(ev)
            self._rev += 1
            self._cond.notify_all()
        self._attention(kind)  # 락 밖에서 — 창 조작은 GUI 스레드로 넘어간다
        with self._cond:
            while self._answer is None:
                self._cond.wait(timeout=1.0)
                if self._answer is not None:
                    break
                if time.time() - self.last_poll > UI_GONE_SEC:
                    # 창이 닫혀 답할 사람이 없다 — 거절/취소로 풀고 작업을 중단시켜 메인 루프가 종료를 감지하게 한다
                    self.stop_requested.set()
                    self._answer = "n" if kind == "confirm" else ANSWER_CANCEL
            ans, self._pending = self._answer, None
            self._rev += 1
            self._cond.notify_all()
        self._attention(None)
        return ans

    def ask(self, question, options=None):
        return self._wait_answer("ask", question, options=[str(o) for o in (options or [])[:5]])

    def ask_fields(self, question, fields):
        """응답은 CliIO.ask_fields와 같은 "name: value" 줄 형식."""
        fields = list(fields)
        ans = self._wait_answer("fields", question, fields)
        secret_names = {f.get("name") for f in fields
                        if isinstance(f, dict) and f.get("secret")}
        for line in ans.splitlines():
            name, _, v = line.partition(":")
            if name.strip() in secret_names and v.strip():
                self.secrets.add(v.strip())
        return ans

    def confirm(self, question):
        return self._wait_answer("confirm", question)

    def answer(self, text):
        with self._cond:
            self._answer = str(text)
            self._cond.notify_all()

    def stop(self):
        self.stop_requested.set()
        with self._cond:
            if self._pending is not None and self._answer is None:
                self._answer = "n" if self._pending["kind"] == "confirm" else ""
            self._cond.notify_all()

    def reset(self):
        """새 채팅: 화면·대기 작업을 비우고, 실행 중이면 중단시킨다. 대화 기록은 메인 루프가 비운다."""
        with self._cond:
            self.new_session.set()
            self._drop = self.busy
            self.events, self.task = [], ""
            self.epoch += 1
            self._rev += 1
            while not self.tasks.empty():
                self.tasks.get_nowait()
        if self.busy:
            self.stop()

    def poll(self, since, wait=0, rev=None):
        """since 이후 이벤트와 현재 상태. wait>0이면 새 이벤트나 상태 변화(rev와 다름)가 생길 때까지
        최대 wait초 기다린다(브리지 롱폴)."""
        with self._cond:
            self.last_poll = time.time()
            if wait and rev is not None:
                self._cond.wait_for(lambda: len(self.events) > since or self._rev != rev,
                                    timeout=wait)
                self.last_poll = time.time()
            evs = self.events[since:]
            return {"events": evs, "next": since + len(evs),
                    "pending": self._pending, "busy": self.busy,
                    "task": self.task, "epoch": self.epoch, "rev": self._rev, "tab": self._tab}

    def handle(self, path, data):
        """UI 요청 처리 — HTTP와 브리지가 공유한다. 모르는 경로면 False."""
        if path == "/task":
            t = str(data.get("task", "")).strip()
            if t and self.unavailable:
                self.say(f"[error] {self.unavailable}")
            elif t:
                # use_tab=False: 사용자가 탭 칩을 x로 닫음 → 백그라운드에서 작업
                # epoch: 새 채팅 전에 들어온 작업을 메인 루프가 버릴 수 있게 함께 넣는다
                with self._cond:
                    self.tasks.put((t, data.get("use_tab") is not False, str(data.get("tab_url") or ""),
                                    self.epoch))
        elif path == "/answer":
            self.answer(ANSWER_CANCEL if data.get("cancel") else str(data.get("text", "")))
        elif path == "/reset":
            self.reset()
        elif path == "/stop":
            self.stop()
        else:
            return False
        return True


def _settings(io):
    """설정 화면 응답 {"rows": [{name, value, source}]}. 값 조회 실패는 한 줄 오류로 돌려준다."""
    try:
        rows = io.settings() or []
    except Exception as e:
        rows = [{"name": "오류", "value": str(e), "source": ""}]
    return {"rows": [{k: str(r.get(k, "")) for k in ("name", "value", "source")} for r in rows]}


# 앱 아이콘: 검은 둥근 사각형 + 흰 커서(HTML의 favicon SVG와 같은 모양). 파일을 따로 두지 않고 그려서 임시 폴더에 쓴다
_ARROW = [(33, 22), (33, 76), (45, 64), (54, 82), (63, 78), (54, 60), (71, 60)]


def _inside(x, y, poly):
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def _icon_rgba(n):
    """n×n RGBA 바이트(위→아래 행). 4×4 슈퍼샘플로 가장자리를 부드럽게 한다."""
    out = bytearray()
    k = 4
    for py in range(n):
        for px in range(n):
            bg = fg = 0
            for sy in range(k):
                for sx in range(k):
                    x, y = (px + (sx + .5) / k) * 100 / n, (py + (sy + .5) / k) * 100 / n
                    cx, cy = min(max(x, 22), 78), min(max(y, 22), 78)  # 모서리 반지름 22
                    if (x - cx) ** 2 + (y - cy) ** 2 <= 22 * 22:
                        bg += 1
                        fg += _inside(x, y, _ARROW)
            a = bg / (k * k)
            v = round(255 * fg / bg) if bg else 0
            out += bytes((v, v, v, round(255 * a)))
    return bytes(out)


def _png(n):
    px = _icon_rgba(n)
    raw = b"".join(b"\0" + px[r * n * 4:(r + 1) * n * 4] for r in range(n))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _ico(sizes=(16, 24, 32, 48, 64)):
    """BMP(32bpp) 항목만 쓴 .ico — WinForms Icon이 PNG 압축 항목을 못 읽는 버전이 있다."""
    imgs = []
    for n in sizes:
        px = _icon_rgba(n)
        rows = [px[r * n * 4:(r + 1) * n * 4] for r in range(n)][::-1]  # BMP는 아래→위
        bgra = b"".join(bytes(b for i in range(0, len(row), 4)
                              for b in (row[i + 2], row[i + 1], row[i], row[i + 3])) for row in rows)
        mask = b"\0" * (((n + 31) // 32) * 4 * n)
        hdr = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, len(bgra) + len(mask), 0, 0, 0, 0)
        imgs.append((n, hdr + bgra + mask))
    head = struct.pack("<HHH", 0, 1, len(imgs))
    off, dirs, body = 6 + 16 * len(imgs), b"", b""
    for n, data in imgs:
        dirs += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), off + len(body))
        body += data
    return head + dirs + body


def icon_path():
    """pywebview start(icon=)용 아이콘 파일 경로(Windows .ico, 그 외 .png). 실패하면 None."""
    ext = ".ico" if sys.platform == "win32" else ".png"
    p = Path(tempfile.gettempdir()) / f"browser-agent-icon-v1{ext}"
    try:
        if not p.is_file():
            p.write_bytes(_ico() if ext == ".ico" else _png(128))
        return str(p)
    except OSError:
        return None


class BridgeApi:
    """pywebview js_api — 페이지에서 window.pywebview.api.poll/call로 부른다.
    밑줄 속성은 pywebview가 JS에 노출하지 않으므로 WebIO 내부는 보이지 않는다."""

    def __init__(self, io):
        self._io = io

    def poll(self, since=0, wait=0, rev=-1):
        return self._io.poll(max(0, int(since)), min(max(0.0, float(wait)), 25.0), int(rev))

    def call(self, path, data=None):
        return {"ok": self._io.handle(str(path), data if isinstance(data, dict) else {})}

    def settings(self):
        return _settings(self._io)

    def open_image(self, src, caption=""):
        """스크린샷을 별도 pywebview 창으로 연다. 창이 뜬 뒤 evaluate_js로 이미지를 넣는다
        (html= 은 WebView2 NavigateToString 2MB 제한이 있어 data URL을 직접 넣지 않는다)."""
        src = str(src)
        if not src.startswith("data:image/"):
            return {"ok": False}
        import webview

        w = webview.create_window("스크린샷 — " + str(caption)[:80], html=IMAGE_PAGE,
                                  width=1100, height=760, min_size=(320, 240))

        def loaded():
            w.evaluate_js("document.getElementById('img').src = " + json.dumps(src))

        w.events.loaded += loaded
        return {"ok": True}


class _Handler(BaseHTTPRequestHandler):
    io = None
    token = ""

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json", code=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok(self):
        return _secrets.compare_digest(self.headers.get("x-token") or "", self.token)

    def do_OPTIONS(self):
        self.send_error(403)  # CORS 허용 헤더를 절대 내주지 않는다

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            if parse_qs(u.query).get("t", [""])[0] != self.token:
                self.send_error(403)
            else:
                self._send(HTML, "text/html; charset=utf-8")
        elif u.path == "/poll" and self._ok():
            q = parse_qs(u.query)
            self._send(json.dumps(self.io.poll(int(q.get("since", [0])[0]))))
        elif u.path == "/poll":
            self.send_error(403)
        elif u.path == "/settings" and self._ok():
            self._send(json.dumps(_settings(self.io)))
        elif u.path == "/settings":
            self.send_error(403)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._ok():
            self.send_error(403)
            return
        try:
            n = int(self.headers.get("content-length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        if isinstance(data, dict) and self.io.handle(self.path, data):
            self._send("{}")
        else:
            self.send_error(404)


def start_server(io, port=0):
    """데몬 스레드로 서버를 띄우고 토큰 포함 UI URL을 반환한다."""
    _Handler.io = io
    _Handler.token = _secrets.token_urlsafe(16)
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/?t={_Handler.token}"
