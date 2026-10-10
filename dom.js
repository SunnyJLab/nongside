// page.evaluate(DOM_JS, view)로 실행되는 함수. window.__agentEls에 DOM 참조를 저장하고 요약 배열을 반환한다.
// view(선택): iframe이면 {offY: 프레임의 최상위 화면 y, vh: 최상위 화면 높이} — 화면 거리를 최상위 화면 기준으로도 잰다.
(view) => {
  view = view || {};
  const MAX = 300;
  const SEL =
    'a[href], button, input, select, textarea, summary, [role], [onclick], [tabindex], label[for], [contenteditable]';
  // 네이티브로 조작 가능한 요소. 이것들은 role/tabindex와 무관하게 남긴다.
  const NATIVE =
    'a[href], button, input, select, textarea, summary, [onclick], label[for], [contenteditable]';
  // 구조·랜드마크 role은 클릭 대상이 아니라 컨테이너다. 하위 항목이 따로 수집되므로
  // 남기면 300개 슬롯과 토큰만 잡아먹고 innerText 레이아웃 비용도 크다.
  const STRUCT = new Set((
    'main region navigation banner contentinfo complementary search form article section ' +
    'document application presentation none group list listitem directory feed figure img ' +
    'heading paragraph note generic separator table rowgroup toolbar menu menubar tablist tabpanel ' +
    'tree treegrid grid radiogroup status log marquee timer alert math definition term'
  ).split(' '));

  // 숨은(display:none)·픽셀(1px) iframe은 스캔하지 않는다 — 광고/추적 프레임.
  if (window !== window.top && (innerWidth <= 1 || innerHeight <= 1)) {
    window.__agentEls = [];
    return { els: [], nFile: 0, tiny: true };
  }

  // role/tabindex만으로 잡힌 요소 중 실제 조작 대상이 아닌 것을 걸러낸다.
  const interactive = (el) => {
    if (el.matches(NATIVE)) return true;
    const r = el.getAttribute('role');
    if (r) {
      if (!STRUCT.has(r)) return true;
      return el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1';
    }
    // [tabindex]만 있는 경우: -1은 포커스 관리용이라 조작 대상 아님
    return el.getAttribute('tabindex') !== '-1';
  };

  // 스캔 중 예외로 부분 참조가 남지 않게, 다 모은 뒤 한 번에 교체한다.
  const els = [];
  const out = [];

  const clean = (s, n) =>
    String(s || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, n);

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none") return false;
    if (el.getAttribute("aria-hidden") === "true") return false;
    return true;
  };

  // 라벨 우선순위: innerText -> aria-label -> placeholder -> value -> title -> alt
  const label = (el) => {
    const t = clean(el.innerText, 80);
    if (t) return t;
    for (const c of [
      el.getAttribute("aria-label"),
      // X 같은 폼은 placeholder 없이 <label>/aria-labelledby로만 이름을 준다
      el.labels && el.labels[0] && el.labels[0].innerText,
      (el.getAttribute("aria-labelledby") || "").split(/\s+/)
        .map((id) => (document.getElementById(id) || {}).innerText || "").join(" "),
      el.getAttribute("placeholder"),
      el.type === "password" ? "(password)"
        : (el.getAttribute("autocomplete") || "").includes("one-time-code") ? (el.value ? "(otp)" : "")
        : el.value,
      el.getAttribute("title"),
      el.getAttribute("alt"),
      el.getAttribute("autocomplete"),
      el.getAttribute("name"),
    ]) {
      const v = clean(c, 80);
      if (v) return v;
    }
    return "";
  };

  const role = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === "A") return "link";
    if (tag === "BUTTON") return "button";
    if (tag === "INPUT") return el.type || "input";
    if (tag === "SELECT") return "combobox";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "SUMMARY") return "summary";
    if (el.isContentEditable) return "textbox";
    return "";
  };

  // A1: 펼침 가능 요소 힌트 (첫 매치 하나만)
  // 숨은메뉴 판정: querySelector 최대 2회(컨테이너) + 내부 1회(a/button)
  const getHint = (el) => {
    // 1. aria-expanded="false" -> 접힘
    if (el.getAttribute("aria-expanded") === "false") return "접힘";
    // 2. aria-haspopup 있고 값이 "false"가 아님 -> 팝업
    const hp = el.getAttribute("aria-haspopup");
    if (hp !== null && hp !== "false") return "팝업";
    // 3. SUMMARY + 부모 details에 open 없음 -> 접힘
    if (
      el.tagName === "SUMMARY" &&
      el.parentElement &&
      el.parentElement.tagName === "DETAILS" &&
      !el.parentElement.hasAttribute("open")
    )
      return "접힘";
    // 4. 숨은 메뉴: 자신 또는 부모(한 단계, 태그 무관)의 직접 자식에
    //    렌더 안 된 ul/ol/[role=menu] + 내부 a/button >= 1
    const isHiddenMenu = (c) => {
      if (!c) return false;
      // visible() 과 같은 기준: rects 없음 OR visibility:hidden OR opacity:0
      const rects = c.getClientRects();
      if (rects.length > 0) {
        const cs = getComputedStyle(c);
        if (cs.visibility !== "hidden" && cs.opacity !== "0") return false;
      }
      return !!c.querySelector("a, button");
    };
    // querySelector 1: 자신의 직접 자식
    const c1 = el.querySelector(":scope > ul, :scope > ol, :scope > [role='menu']");
    if (isHiddenMenu(c1)) return "숨은메뉴";
    // querySelector 2: 부모의 직접 자식 (보정1: 부모 태그 무관, 한 단계만)
    const par = el.parentElement;
    if (par) {
      const c2 = par.querySelector(":scope > ul, :scope > ol, :scope > [role='menu']");
      if (isHiddenMenu(c2)) return "숨은메뉴";
    }
    return "";
  };

  // A2: select 옵션 문자열 {*현재|옵션|...|+N}
  // 최대 12개, 각 20자 컷, 중괄호 내 200자 캡
  const selectOpts = (el) => {
    if (el.tagName !== "SELECT") return "";
    const opts = Array.from(el.options);
    if (!opts.length) return "";
    const parts = [];
    let extra = 0;
    let total = 0;
    for (let j = 0; j < opts.length; j++) {
      const o = opts[j];
      const entry = (o.selected ? "*" : "") + clean(o.text || o.value, 20);
      if (parts.length < 12 && total + entry.length + 1 <= 200) {
        parts.push(entry);
        total += entry.length + 1;
      } else {
        extra++;
      }
    }
    return "{" + parts.join("|") + (extra ? "|+" + extra : "") + "}";
  };

  // open shadow root만 관통한다. iframe은 browser.py가 page.frames를 순회하며
  // 각 프레임에서 이 스크립트를 개별 evaluate하므로 여기서 내려가면 중복 수집된다.
  // 1단계: 가벼운 판정(보임+조작 가능)만 하고 뷰포트 거리를 기록한다.
  const RAW_MAX = 4000;
  const vh = innerHeight;
  // 화면까지의 세로 거리(겹치면 0). iframe은 최상위 화면 기준으로도 재서 큰 쪽 — 내용 높이만큼 늘어난 iframe(네이버 카페
  // 본문)은 자기 화면 기준으로는 모든 글이 0이라 스크롤해도 문서 앞쪽 글만 뽑혔다. 안쪽 스크롤 iframe은 자기 기준이 잡는다
  const offY = view.offY || 0, topVh = view.vh || vh;
  const gap = (top, bottom, h) => (bottom < 0 ? -bottom : top > h ? top - h : 0);
  const dist = (r) => Math.max(gap(r.top, r.bottom, vh), gap(r.top + offY, r.bottom + offY, topVh));
  const cands = [];
  const scan = (root) => {
    if (!root || cands.length >= RAW_MAX) return;
    for (const el of root.querySelectorAll(SEL)) {
      if (cands.length >= RAW_MAX) break;
      if (el.shadowRoot) scan(el.shadowRoot);
      if (!interactive(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      if (!visible(el)) continue;
      cands.push({ el, d: dist(r), n: cands.length });
    }
  };

  // 열린 모달(aria-modal / dialog[open])이 있으면 그 안만 수집한다. 뒤 배경 요소는 오버레이에
  // 가려 클릭이 안 되는데 목록에 남아 있으면 모델이 계속 그걸 누르다 반복 차단에 걸린다.
  let modal = null;
  for (const m of document.querySelectorAll("[aria-modal='true'], dialog[open]")) {
    if (!visible(m)) continue;
    // 투명(자신 또는 조상 opacity:0)하거나 뷰포트 밖으로 치워둔 숨은 모달은 무시한다 —
    // visible()은 opacity·위치를 안 봐서 크기만 있으면 모달로 잡혀 본문이 전부 사라진다.
    let faded = false;
    for (let a = m; a && a.tagName !== "HTML"; a = a.parentElement) {
      if (getComputedStyle(a).opacity === "0") { faded = true; break; }
    }
    if (faded) continue;
    const r = m.getBoundingClientRect();
    if (r.right <= 0 || r.bottom <= 0 || r.left >= innerWidth || r.top >= innerHeight) continue;
    modal = m; // 나중(위)에 열린 것 우선
  }
  const root = modal || document;
  scan(root);
  // designMode 에디터(iframe 본문)는 SEL에 안 걸리므로 body를 직접 넣는다
  const bd = document.body;
  if (!modal && document.designMode === "on" && bd && !cands.some((c) => c.el === bd))
    cands.push({ el: bd, d: 0, n: cands.length });
  // 가짜 입력면 에디터(SmartEditor ONE 등): 실제 contenteditable은 화면 밖 캡처 div이고
  // 보이는 건 placeholder 문단뿐이라 목록에 안 잡힌다 → 에디터 안 placeholder를 입력면으로 넣는다.
  // 휴리스틱: class/data-placeholder + editor 조상 휴리스틱. 다른 에디터가 새면 조건 보강.
  const surf = new Set();
  for (const el of root.querySelectorAll('[class*="placeholder"]:not(input):not(textarea), [data-placeholder]')) {
    if (surf.size >= 5) break;
    if (el.isContentEditable || !el.closest('[class*="editor"], [id*="editor"], [class*="se-"]')) continue;
    if (!clean(el.textContent, 1) || !visible(el)) continue;
    const r = el.getBoundingClientRect();
    surf.add(el);
    cands.push({ el, d: dist(r), n: cands.length });
  }

  // A4: 자동완성 후보. 포커스된 입력에 값이 있을 때만(자동완성은 입력 후에 뜬다),
  // 보이는 후보 컨테이너 안의 평범한 항목(li/td/div.item)을 SEL 밖에서 추가 수집한다.
  // A4는 선택보다 먼저 모아 자리를 확보한다 (긴 페이지에서 후보가 잘리지 않게).
  const candItems = [];
  const ae = document.activeElement;
  if (
    ae &&
    (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA") &&
    clean(ae.value, 1)
  ) {
    const conts = [];
    const seen = new Set();
    const pushCont = (c) => {
      // 입력을 감싼 래퍼는 제외: 자기 내부 항목을 후보로 쏟아내는 걸 막는다
      if (c && !seen.has(c) && !c.contains(ae) && visible(c) && conts.length < 3) {
        seen.add(c);
        conts.push(c);
      }
    };
    for (const attr of ["aria-controls", "aria-owns"]) {
      const id = ae.getAttribute(attr);
      if (id) pushCont(document.getElementById(id));
    }
    if (!conts.length) {
      for (const c of document.querySelectorAll(
        "[role=listbox], [class*=auto], [class*=sugg], [class*=drop], [id*=auto], [id*=sugg]"
      )) {
        pushCont(c);
        if (conts.length >= 3) break;
      }
    }
    for (const cont of conts) {
      if (candItems.length >= 30) break;
      for (const item of cont.querySelectorAll('li, td, div[class*="item"]')) {
        if (candItems.length >= 30) break;
        if (!visible(item)) continue;
        // 이중 수집 방지: 항목 자신이나 후손이 이미 SEL로 수집됐으면 건너뛴다
        // (li>a 구조에서 같은 후보가 두 줄로 나오는 것 방지)
        if (item.matches(SEL) && interactive(item)) continue;
        if (item.querySelector("a[href], button, [role=option], input")) continue;
        candItems.push(item);
      }
    }
  }

  // 2단계: 뷰포트에 가까운 순으로 자리를 채우고 출력은 문서 순서를 유지한다.
  // 문서 순서로만 자르면 긴 페이지에서 GNB/푸터가 화면에 보이는 본문을 밀어낸다.
  // label/hint 같은 비싼 계산은 고른 요소에만 한다.
  const room = MAX - candItems.length;
  const picked = cands.length <= room ? cands
    : cands.slice().sort((a, b) => a.d - b.d || a.n - b.n).slice(0, room)
        .sort((a, b) => a.n - b.n);
  for (const { el, d } of picked) {
    const i = els.length;
    els.push(el);
    out.push({
      i,
      d,
      tag: el.tagName.toLowerCase(),
      role: surf.has(el) ? "textbox" : role(el),
      type: el.type || "",
      text: label(el),
      hint: surf.has(el) ? "에디터" : getHint(el),    // A1
      opts: selectOpts(el), // A2
    });
  }
  for (const item of candItems) {
    const i = els.length;
    els.push(item);
    out.push({
      i,
      d: 0,
      tag: item.tagName.toLowerCase(),
      role: role(item),
      type: "",
      text: label(item),
      hint: "후보",
      opts: "",
    });
  }

  window.__agentEls = els;
  // 파일 입력 개수·화면 높이(iframe 거리 계산용)도 같은 evaluate에서 세서 observe의 왕복을 줄인다.
  return { els: out, nFile: document.querySelectorAll("input[type=file]").length, tiny: false,
           vh: innerHeight,
           modal: modal ? clean(modal.getAttribute("aria-label") || modal.innerText, 40) : "" };
}
