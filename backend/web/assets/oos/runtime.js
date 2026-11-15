/* ============================================================
   OUTOFSTOCK 공용 런타임 — 모든 화면이 가장 먼저 불러온다(assets/oos/runtime.js).
   겉모습은 팀 시안 그대로 두고, 서버와 이어 주는 공통 부분만 여기 모았다.

   ① 신뢰 타입(Trusted Types) 기본 정책 — 서버 CSP 의 require-trusted-types-for 'script' 와 짝.
      innerHTML 같은 위험한 대입이 모두 이 정책을 지난다. <script>·인라인 이벤트(on*=)·javascript: 주소가
      섞여 있으면 막고 서버에 보고한다(/csp-report). 화면 템플릿은 값마다 esc() 를 쓰므로 정상 HTML 은 그대로 통과.
   ② 선언형 동작 — CSP 가 script-src-attr 'none' 이라 onclick 속성 같은 인라인 핸들러는 실행되지 않는다.
      data-on="함수(인자)" · data-on-submit="…" · data-on-self="…"(그 요소 자체를 눌렀을 때만 — 모달 바깥 배경) 로 적고,
      화면이 OOS.actions({...}) 로 등록한 함수만 부른다. eval·new Function 없는 작은 해석기라 XSS 로 속성이
      끼어들어도 부를 수 있는 건 등록된 화면 동작뿐이다.
   ③ api() — HttpOnly 쿠키 세션 · 변경 요청에 X-CSRF-Token · 접근 토큰 만료 시 갱신(동시에 여러 요청이 만료돼도 1번)
      · 추가 인증(비밀번호 재확인)·2단계 인증 코드 창 · 로그인 상태가 바뀐 뒤 도착한 늦은 응답은 버림.
   ④ 로그인 상태(브라우저에는 아이디 힌트만) · 헤더(잔액·마이페이지·로그인/로그아웃) · 실시간 알림(SSE)
      · 다른 탭에서 로그인/로그아웃하면 이 탭도 따라감 · 로그인 전 기록(학습 표시·오늘 퀴즈)을 계정으로 옮김.
   우리 서버가 내보낸 화면에만 표식(<meta name="oos-server">)이 붙는다 — 파일로 열거나(file://) Live Server·GitHub Pages
   같은 다른 서버로 열면 서버 기능만 조용히 빠지고 화면은 시안의 예시 값 그대로 보인다(디자인 작업용).
   ============================================================ */
(function () {
    'use strict';
    if (window.OOS) return;

    const ONLINE = /^https?:$/.test(location.protocol) && !!document.querySelector('meta[name="oos-server"]');

    // ── 보고(/csp-report) ─────────────────────────────────────
    function cspReport(directive, sample) {
        if (!ONLINE || !navigator.sendBeacon) return;
        try {
            const body = JSON.stringify({ 'csp-report': { 'document-uri': location.pathname, 'violated-directive': directive,
                'blocked-uri': 'trusted-types', 'script-sample': String(sample).slice(0, 80) } });
            navigator.sendBeacon('/csp-report', new Blob([body], { type: 'application/csp-report' }));
        } catch (e) { /* 보고 실패는 화면과 무관 */ }
    }

    // ── ① 신뢰 타입 기본 정책 ─────────────────────────────────
    (function () {
        if (!window.trustedTypes || !window.trustedTypes.createPolicy) return;
        const BAD = /<script[\s>/]|<[^>]*\son[a-z]+\s*=|\s(?:href|src|action|formaction|xlink:href)\s*=\s*["']?\s*javascript:/i;
        try {
            window.trustedTypes.createPolicy('default', {
                createHTML(s) {
                    const m = BAD.exec(s);
                    if (m) { cspReport('trusted-types:html', m[0]); throw new TypeError('차단된 HTML(스크립트·이벤트 핸들러)'); }
                    return s;
                },
                createScriptURL(u) {
                    const x = new URL(u, location.href);
                    if (x.origin === location.origin && x.pathname.indexOf('/assets/') === 0) return u;
                    cspReport('trusted-types:script-url', x.origin);
                    throw new TypeError('허용되지 않은 스크립트 주소');
                },
                createScript() { cspReport('trusted-types:script', 'string-code'); throw new TypeError('문자열 코드 실행 금지'); }
            });
        } catch (e) { /* 이미 만들어짐 */ }
    })();

    // ── 작은 도구 ─────────────────────────────────────────────
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const won = (n) => Math.round(Number(n) || 0).toLocaleString('ko-KR');
    const idemKey = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('');
    /** 외부 링크는 http(s) 만 — 그 밖(javascript: 등)은 '#'. */
    const safeUrl = (u, fallback) => (/^https?:\/\//i.test(String(u || '')) ? String(u) : (fallback || '#'));
    function readJSON(key) { try { return JSON.parse(localStorage.getItem(key)); } catch (e) { return null; } }
    function writeJSON(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) { /* 저장 실패해도 진행 */ } }
    function removeKeys(keys) { keys.forEach((k) => { try { localStorage.removeItem(k); } catch (e) { /* 무시 */ } }); }

    // 시안 전용 가짜 값(잔액·연속 출석·출석일·지갑 활동 기록)과 옛 화면의 저장분 — 실제 값은 서버가 준다. 남아 있으면 지운다
    removeKeys(['outofstock_balance', 'outofstock_streak', 'outofstock_last_attendance', 'outofstock_activity_log',
        'oos_token', 'oos_guest', 'oos_profile', 'oos_learned_terms']);
    // 로그인하지 않았을 때 브라우저에만 두는 기록 — 로그아웃하면 지운다(공용 PC 에 이전 사람의 성향·학습 표시가 남지 않게)
    const GUEST_KEYS = ['outofstock_risk_profile', 'outofstock_learned_terms', 'outofstock_daily_quiz', 'outofstock_practice_records'];

    // 같은 동작 중복 실행 방지(더블클릭·연타) — 진행 중이면 무시
    const busy = new Set();
    async function once(key, fn) {
        if (busy.has(key)) return undefined;
        busy.add(key);
        try { return await fn(); } finally { busy.delete(key); }
    }

    // ── 간단한 이벤트 ─────────────────────────────────────────
    const listeners = Object.create(null);
    function on(name, fn) { (listeners[name] = listeners[name] || []).push(fn); }
    function emit(name, data) {
        (listeners[name] || []).slice().forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } });
    }

    // ── ② 선언형 동작(data-on) ────────────────────────────────
    // 문법: 문장;문장 / 함수(인자, …) / event.preventDefault() · event.stopPropagation()
    // 인자: '문자열' "문자열" 숫자 null true false this event this.dataset.키 {키: 값} 조건 ? 값 : 값
    const ACTIONS = Object.create(null);
    function actions(map) { Object.keys(map).forEach((k) => { if (typeof map[k] === 'function') ACTIONS[k] = map[k]; }); }
    const parsed = new Map();
    function parse(src) {
        if (parsed.has(src)) return parsed.get(src);
        const s = src; let i = 0;
        const fail = (m) => { throw new SyntaxError(`data-on 해석 실패(${m}) @${i}: ${s}`); };
        const ws = () => { while (i < s.length && /\s/.test(s[i])) i++; };
        const peek = (c) => { ws(); return s.startsWith(c, i); };
        const eat = (c) => { if (!peek(c)) fail(`'${c}' 필요`); i += c.length; };
        const ident = () => { ws(); const m = /^[A-Za-z_$][\w$]*/.exec(s.slice(i)); if (!m) fail('이름'); i += m[0].length; return m[0]; };
        const list = (close, item) => { const out = []; if (peek(close)) return out; for (;;) { out.push(item()); if (!peek(',')) break; eat(','); } return out; };
        function primary() {
            ws(); const c = s[i];
            if (c === "'" || c === '"') { i++; let v = ''; while (i < s.length && s[i] !== c) { if (s[i] === '\\') i++; v += s[i++]; } eat(c); return { t: 'lit', v }; }
            if (c === '-' || /\d/.test(c || '')) { const m = /^-?\d+(\.\d+)?/.exec(s.slice(i)); if (!m) fail('숫자'); i += m[0].length; return { t: 'lit', v: Number(m[0]) }; }
            if (c === '{') { eat('{'); const props = list('}', () => { const k = ident(); eat(':'); return [k, value()]; }); eat('}'); return { t: 'obj', props }; }
            const id = ident();
            if (id === 'null' || id === 'true' || id === 'false') return { t: 'lit', v: id === 'null' ? null : id === 'true' };
            if (id === 'event') return { t: 'event' };
            if (id === 'this') { if (!peek('.')) return { t: 'this' }; eat('.'); if (ident() !== 'dataset') fail('this.dataset 만'); eat('.'); return { t: 'data', k: ident() }; }
            return fail(`값 '${id}'`);
        }
        function value() { const v = primary(); if (!peek('?')) return v; eat('?'); const a = value(); eat(':'); return { t: 'cond', c: v, a, b: value() }; }
        function stmt() {
            const id = ident();
            if (id === 'event') { eat('.'); const m = ident(); if (m !== 'preventDefault' && m !== 'stopPropagation') fail('event.' + m); eat('('); eat(')'); return { t: 'ev', m }; }
            eat('('); const args = list(')', value); eat(')'); return { t: 'call', f: id, args };
        }
        const prog = []; ws();
        while (i < s.length) { prog.push(stmt()); ws(); if (i < s.length) { eat(';'); ws(); } }
        parsed.set(src, prog);
        return prog;
    }
    function val(v, el, ev) {
        switch (v.t) {
            case 'lit': return v.v;
            case 'event': return ev;
            case 'this': return el;
            case 'data': return el.dataset[v.k];
            case 'obj': return Object.fromEntries(v.props.map(([k, x]) => [k, val(x, el, ev)]));
            case 'cond': return val(v.c, el, ev) ? val(v.a, el, ev) : val(v.b, el, ev);
            default: return undefined;
        }
    }
    function run(src, el, ev) {
        let prog;
        try { prog = parse(src); } catch (e) { console.error(e.message); return; }
        for (const st of prog) {
            if (st.t === 'ev') { ev[st.m](); continue; }
            const fn = ACTIONS[st.f];
            if (typeof fn !== 'function') { console.error('data-on: 등록되지 않은 동작 ' + st.f); return; }
            try {
                const r = fn(...st.args.map((a) => val(a, el, ev)));
                if (r && typeof r.catch === 'function') r.catch((err) => console.error(err));
            } catch (err) { console.error(err); }
        }
    }
    const ON_SEL = '[data-on],[data-on-self]';
    // 포착 단계에서 받아 안쪽 요소부터 바깥으로(예전 인라인 핸들러의 거품 순서) — stopPropagation 이면 거기서 멈춘다
    document.addEventListener('click', (e) => {
        let el = e.target instanceof Element ? e.target.closest(ON_SEL) : null;
        while (el) {
            if (el.disabled) return;
            const src = el.getAttribute('data-on');
            if (src) run(src, el, e);
            const self = el.getAttribute('data-on-self');
            if (self && e.target === el) run(self, el, e);
            if (e.cancelBubble) return;
            el = el.parentElement && el.parentElement.closest(ON_SEL);
        }
    }, true);
    document.addEventListener('submit', (e) => {
        const f = e.target instanceof Element && e.target.closest('[data-on-submit]');
        if (f) run(f.getAttribute('data-on-submit'), f, e);
    }, true);
    // 키보드 접근 — 클릭 동작만 붙은 div·span·href 없는 a 도 Tab 으로 가고 Enter/Space 로 누를 수 있게(KWCAG 2.1.1)
    const KBD_SEL = 'div[data-on],span[data-on],li[data-on],img[data-on],b[data-on],a[data-on]:not([href])';
    function kbd(root) {
        const els = root.matches && root.matches(KBD_SEL) ? [root] : [];
        if (root.querySelectorAll) els.push(...root.querySelectorAll(KBD_SEL));
        els.forEach((el) => { if (el.hasAttribute('tabindex')) return; el.tabIndex = 0; if (!el.hasAttribute('role')) el.setAttribute('role', 'button'); });
    }
    document.addEventListener('keydown', (e) => {
        const el = e.target;
        if (!e.defaultPrevented && (e.key === 'Enter' || e.key === ' ') && el instanceof Element && el.matches(KBD_SEL)) { e.preventDefault(); el.click(); }
    });

    // ── ③ 서버 호출 ───────────────────────────────────────────
    const CSRF_COOKIES = ['__Host-oos_csrf', 'oos_csrf'];       // HTTPS 면 __Host- 접두어(브라우저가 Secure·경로를 강제)
    function csrfToken() {
        for (const c of document.cookie.split(/;\s*/)) {
            const k = c.indexOf('='), n = c.slice(0, k);
            if (k > 0 && CSRF_COOKIES.includes(n)) return decodeURIComponent(c.slice(k + 1));
        }
        return '';
    }
    const WEB = { 'X-Client': 'web' };                          // 웹: 토큰을 본문이 아니라 HttpOnly 쿠키로 받는다

    // ── 기기 결속 키(DPoP — 서버 app/security/dpop.py) ──────────────────────────
    // 이 브라우저에서만 쓰는 ECDSA P-256 키 — 개인키는 extractable=false 라 스크립트로도 꺼낼 수 없다(IndexedDB 에 객체로만).
    // 로그인·갱신·변경 요청(POST·DELETE)·관리 요청에 이 키로 서명한 1회용 증명(DPoP 헤더)을 붙인다 →
    // 쿠키를 훔쳐 다른 기기에서 써도 세션을 늘리거나 돈을 옮길 수 없다. 키를 못 만드는 환경이면 증명 없이(예전처럼) 동작.
    let devKey = null, clockSkew = 0;                            // clockSkew: 서버 시각 − 내 시각(ms, 응답 Date 헤더로)
    const b64u = (buf) => btoa(String.fromCharCode.apply(null, new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    const b64uText = (t) => b64u(new TextEncoder().encode(t));
    function keyStore(mode, fn) {
        return new Promise((resolve, reject) => {
            const open = indexedDB.open('oos-device', 1);
            open.onupgradeneeded = () => open.result.createObjectStore('keys');
            open.onerror = () => reject(open.error);
            open.onsuccess = () => {
                const tx = open.result.transaction('keys', mode), req = fn(tx.objectStore('keys'));
                tx.oncomplete = () => { open.result.close(); resolve(req.result); };
                tx.onerror = () => { open.result.close(); reject(tx.error); };
            };
        });
    }
    function deviceKey() {
        if (!devKey) {
            devKey = (async () => {
                if (!ONLINE || !window.indexedDB || !window.crypto || !crypto.subtle) return null;
                let kp = await keyStore('readonly', (st) => st.get('dpop'));
                if (!kp || !kp.privateKey) {
                    kp = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']);
                    await keyStore('readwrite', (st) => st.put(kp, 'dpop'));
                }
                const pub = await crypto.subtle.exportKey('jwk', kp.publicKey);   // 공개키는 항상 내보낼 수 있다
                return { priv: kp.privateKey, jwk: { kty: 'EC', crv: 'P-256', x: pub.x, y: pub.y } };
            })().catch(() => null);
        }
        return devKey;
    }
    async function dpopProof(method, path) {
        const k = await deviceKey();
        if (!k) return null;
        const h = b64uText(JSON.stringify({ typ: 'dpop+jwt', alg: 'ES256', jwk: k.jwk }));
        const p = b64uText(JSON.stringify({ htm: method, htu: location.origin + path.split('?')[0].split('#')[0],
            iat: Math.floor((Date.now() + clockSkew) / 1000), jti: idemKey() }));
        try {
            const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, k.priv, new TextEncoder().encode(h + '.' + p));
            return h + '.' + p + '.' + b64u(sig);
        } catch (e) { return null; }
    }
    const needsProof = (method, path) => method !== 'GET' && method !== 'HEAD' || /^\/admin\//.test(path);
    function noteServerTime(r) {
        const d = Date.parse(r.headers.get('Date') || '');
        if (d) clockSkew = d - Date.now();
    }
    let user = null;                                             // 로그인 아이디(힌트) — 실제 인증은 HttpOnly 쿠키, 판단은 서버가 매 요청
    if (ONLINE) { try { user = localStorage.getItem('oos_user') || null; } catch (e) { user = null; } }
    let gen = 0;                                                 // 로그인·로그아웃·세션 만료마다 +1 — 그 전에 보낸 요청의 늦은 응답은 버린다
    const never = () => new Promise(() => {});
    let refreshing = null;
    // 접근 토큰 만료 → 갱신 토큰으로 새로 받기(서버가 갱신 토큰도 회전). 동시에 여러 요청이 만료돼도 갱신은 한 번만 —
    // 회전된 갱신 토큰을 두 번 쓰면 서버가 탈취로 보고 세션을 끊는다
    function refreshSession() {
        if (!refreshing) {
            refreshing = (async () => {
                const headers = Object.assign({ 'X-CSRF-Token': csrfToken() }, WEB);
                const proof = await dpopProof('POST', '/auth/refresh');
                if (proof) headers.DPoP = proof;
                const r = await fetch('/auth/refresh', { method: 'POST', credentials: 'same-origin', headers });
                noteServerTime(r);
                return r.ok;
            })().catch(() => false)
                .finally(() => setTimeout(() => { refreshing = null; }, 0));
        }
        return refreshing;
    }
    // 사용자가 알아야 하는 세션 종료 이유 — 유휴·만료·다른 곳에서 종료·기기 불일치·탈취 의심·계정 잠금
    const SESSION_END_NOTICE = new Set(['session_idle', 'session_expired', 'session_revoked', 'device_mismatch', 'refresh_reuse', 'account_locked',
        'dpop_mismatch', 'dpop_required']);
    function sessionEnded(code, msg) {
        clearSession();
        if (SESSION_END_NOTICE.has(code)) setTimeout(() => alert(msg), 60);
    }
    function httpError(message, status, code) { const e = new Error(message); e.status = status; e.code = code || ''; return e; }
    /**
     * 서버 호출(같은 출처). opts: { method, body(객체면 JSON), headers, timeout(ms) }
     * 실패하면 Error(message=서버 안내 문구, status, code) — 401 중 토큰 만료·추가 인증은 여기서 처리하고 다시 보낸다.
     */
    async function api(path, opts = {}, _try = 0) {
        if (!ONLINE) throw httpError('서버에 연결되지 않은 화면이에요(파일로 열림)', 0, 'offline');
        const g = gen, sent = user;
        const method = String(opts.method || 'GET').toUpperCase();
        const headers = Object.assign({}, opts.headers || {});
        let body = opts.body;
        if (body !== undefined && typeof body !== 'string') { body = JSON.stringify(body); headers['Content-Type'] = 'application/json'; }
        if (method !== 'GET' && method !== 'HEAD') { const c = csrfToken(); if (c) headers['X-CSRF-Token'] = c; }
        if (needsProof(method, path)) { const proof = await dpopProof(method, path); if (proof) headers.DPoP = proof; }
        const ctrl = new AbortController(), timer = setTimeout(() => ctrl.abort(), opts.timeout || 20000);
        let r;
        try { r = await fetch(path, { method, headers, body, credentials: 'same-origin', signal: ctrl.signal }); noteServerTime(r); }
        catch (e) {
            if (g !== gen) return never();
            throw httpError(e && e.name === 'AbortError' ? '서버 응답이 늦어요 — 잠시 뒤 다시 시도해 주세요' : '서버에 연결할 수 없어요 — 잠시 뒤 다시 시도해 주세요', 0, 'network');
        } finally { clearTimeout(timer); }
        if (g !== gen) return never();                           // 로그아웃·계정 전환 뒤 도착한 이전 계정의 응답은 버림
        if (!r.ok) {
            let m = '요청을 처리하지 못했어요', code = r.headers.get('X-Auth-Code') || '';
            try {
                const j = await r.json();
                m = (typeof j.detail === 'string' ? j.detail : (j.detail && j.detail[0] && j.detail[0].msg)) || m;
                code = j.code || code;
            } catch (e) { /* 본문 없음 */ }
            if (r.status === 429) { const ra = r.headers.get('Retry-After'); if (ra && !/분|초/.test(m)) m += ` (${ra}초 뒤 다시)`; }
            // 401 — ① 접근 토큰 만료: 갱신 후 재시도 ② 추가 인증 필요: 비밀번호 확인 창 → 재시도 ③ 그 밖(세션 끝): 정리
            if (r.status === 401 && sent && sent === user && !/^\/auth\/(login|register|logout|refresh|step-up)\b/.test(path)) {
                if (code === 'token_expired') {
                    if (_try < 2 && await refreshSession() && g === gen) return api(path, opts, _try + 1);
                    sessionEnded('session_expired', '로그인 유지 시간이 지나 다시 로그인해 주세요');
                    return never();
                }
                if (code === 'step_up_required') {
                    if (_try < 2 && await stepUp(m) && g === gen) return api(path, opts, _try + 1);
                    throw httpError('비밀번호 확인을 마치지 않아 요청을 멈췄어요', 401, code);
                }
                if (code === 'dpop_invalid' || code === 'dpop_replay') {     // 시계 차이 등 — 서버 시각으로 맞춘 새 증명으로 한 번 더
                    if (_try < 1 && g === gen) return api(path, opts, _try + 1);
                    throw httpError(m, 401, code);
                }
                sessionEnded(code, m);
                return never();
            }
            throw httpError(m, r.status, code);
        }
        if (r.status === 204) return null;
        let j = null;
        try { j = await r.json(); } catch (e) { j = null; }
        if (g !== gen) return never();
        return j;
    }

    // ── 대화 상자(비밀번호 재확인 · 2단계 인증 · 화면에서 쓰는 간단한 입력 창) ──────────
    const CSS = `
.oos-dlg-overlay{position:fixed;inset:0;background:rgba(15,23,42,.55);z-index:5000;display:flex;align-items:center;justify-content:center;padding:20px}
.oos-dlg{position:relative;width:100%;max-width:380px;max-height:90vh;overflow:auto;background:var(--card-bg,#fff);color:var(--text-primary,#0f172a);border-radius:16px;padding:22px 20px;box-shadow:0 20px 60px rgba(15,23,42,.3);font-family:inherit;box-sizing:border-box;text-align:left}
.oos-dlg-title{font-size:15.5px;font-weight:800;margin:0 24px 4px 0}
.oos-dlg-sub{font-size:12px;color:var(--text-secondary,#475569);line-height:1.55;margin-bottom:14px;white-space:pre-line}
.oos-dlg label{display:block;font-size:12px;font-weight:700;color:var(--text-secondary,#475569);margin-bottom:10px}
.oos-dlg label[hidden]{display:none}
.oos-dlg input,.oos-dlg select{display:block;width:100%;margin-top:5px;padding:10px 12px;border:1px solid var(--card-border,#e2e8f0);border-radius:8px;font-size:13px;font-family:inherit;background:var(--item-bg,#f8fafc);color:inherit;outline:none;box-sizing:border-box}
.oos-dlg input:focus,.oos-dlg select:focus{border-color:var(--accent-blue,#3730a3);background:#fff}
.oos-dlg-hint{font-size:11.5px;font-weight:600;color:var(--text-muted,#64748b);margin-top:4px}
.oos-dlg-msg{min-height:18px;font-size:12px;color:#b91c1c;margin:2px 0 10px;line-height:1.5}
.oos-dlg-actions{display:flex;gap:8px}
.oos-dlg-actions button{flex:1;padding:10px 12px;border-radius:8px;font-size:13px;font-weight:700;font-family:inherit;cursor:pointer;border:1px solid var(--card-border,#e2e8f0);background:#fff;color:var(--text-secondary,#475569)}
.oos-dlg-actions .oos-dlg-ok{background:var(--accent-blue,#3730a3);border-color:var(--accent-blue,#3730a3);color:#fff}
.oos-dlg-actions .oos-dlg-ok.danger{background:#b91c1c;border-color:#b91c1c}
.oos-dlg-actions button:disabled{opacity:.6;cursor:not-allowed}
.oos-toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,8px);z-index:6000;max-width:min(92vw,420px);padding:11px 16px;border-radius:10px;background:#1e1b4b;color:#fff;font-size:12.5px;font-weight:700;line-height:1.5;box-shadow:0 10px 30px rgba(15,23,42,.3);opacity:0;visibility:hidden;transition:opacity .2s,transform .2s,visibility .2s;pointer-events:none}
.oos-toast.show{opacity:1;visibility:visible;transform:translate(-50%,0);pointer-events:auto}
.oos-toast.err{background:#991b1b}`;
    function ensureCss() {
        if (document.getElementById('oos-runtime-css')) return;
        const st = document.createElement('style');
        st.id = 'oos-runtime-css';
        st.textContent = CSS;
        (document.head || document.documentElement).appendChild(st);
    }
    function el(tag, props, kids) {
        const n = document.createElement(tag);
        Object.entries(props || {}).forEach(([k, v]) => {
            if (v == null || v === false) return;
            if (k === 'text') n.textContent = v;
            else if (k === 'className') n.className = v;
            else if (k === 'hidden') n.hidden = true;
            else n.setAttribute(k, v === true ? '' : v);
        });
        (kids || []).forEach((c) => c && n.appendChild(c));
        return n;
    }
    let dlgSeq = 0;
    /**
     * 간단한 입력 창 — 값은 텍스트로만 넣는다(innerHTML 없음).
     * fields: [{ name, label, type, placeholder, autocomplete, inputmode, maxlength, value, hint, hidden, options:[{value,label}] }]
     * submit(values, ui) 가 값을 돌려주면 그 값으로 닫고, 예외를 던지면 메시지를 보여 주고 창은 그대로.
     * 취소·Esc·바깥 클릭은 null.
     */
    function dialog(cfg) {
        ensureCss();
        return new Promise((resolve) => {
            const id = 'oosDlg' + (++dlgSeq);
            const inputs = {};
            const rows = (cfg.fields || []).map((f) => {
                let input;
                if (f.options) {
                    input = el('select', { name: f.name, id: id + '-' + f.name });
                    f.options.forEach((o) => input.appendChild(el('option', { value: o.value, text: o.label })));
                    if (f.value != null) input.value = f.value;
                } else {
                    input = el('input', { name: f.name, id: id + '-' + f.name, type: f.type || 'text', placeholder: f.placeholder,
                        autocomplete: f.autocomplete || 'off', inputmode: f.inputmode, maxlength: f.maxlength, value: f.value });
                }
                inputs[f.name] = input;
                const lab = el('label', { for: id + '-' + f.name, hidden: f.hidden }, [document.createTextNode(f.label)]);
                lab.appendChild(input);
                if (f.hint) lab.appendChild(el('div', { className: 'oos-dlg-hint', text: f.hint }));
                return lab;
            });
            const msg = el('div', { className: 'oos-dlg-msg', role: 'alert' });
            const ok = el('button', { type: 'submit', className: 'oos-dlg-ok' + (cfg.danger ? ' danger' : ''), text: cfg.ok || '확인' });
            const cancel = el('button', { type: 'button', className: 'oos-dlg-cancel', text: cfg.cancel || '취소' });
            const form = el('form', { className: 'oos-dlg', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': id + '-t', novalidate: true }, [
                el('div', { className: 'oos-dlg-title', id: id + '-t', text: cfg.title || '' }),
                cfg.sub ? el('div', { className: 'oos-dlg-sub', text: cfg.sub }) : null,
                ...rows, msg, el('div', { className: 'oos-dlg-actions' }, [cancel, ok])
            ]);
            const overlay = el('div', { className: 'oos-dlg-overlay' }, [form]);
            const back = document.activeElement;
            const prevOverflow = document.body.style.overflow;
            let done = false;
            function close(v) {
                if (done) return; done = true;
                overlay.remove();
                document.removeEventListener('keydown', onKey, true);
                if (!document.querySelector('.oos-dlg-overlay')) document.body.style.overflow = prevOverflow;
                if (back && document.contains(back) && back.focus) back.focus();
                resolve(v);
            }
            function onKey(e) {
                if (e.key === 'Escape' && overlay === [...document.querySelectorAll('.oos-dlg-overlay')].pop()) { e.stopPropagation(); e.preventDefault(); close(null); }
            }
            const ui = {
                inputs, form,
                error(t) { msg.textContent = t || ''; },
                show(name, yes) { const lab = inputs[name] && inputs[name].closest('label'); if (lab) lab.hidden = !yes; if (yes && inputs[name]) inputs[name].focus(); },
                close
            };
            cancel.addEventListener('click', () => close(null));
            overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
            form.addEventListener('submit', async (e) => {
                e.preventDefault();
                const values = {};
                Object.entries(inputs).forEach(([k, n]) => { values[k] = n.value; });
                if (!cfg.submit) { close(values); return; }
                ok.disabled = true; msg.textContent = '';
                try {
                    const v = await cfg.submit(values, ui);
                    if (v !== undefined) close(v);
                } catch (err) { msg.textContent = (err && err.message) || '처리하지 못했어요'; }
                finally { ok.disabled = false; }
            });
            document.addEventListener('keydown', onKey, true);
            document.body.appendChild(overlay);
            document.body.style.overflow = 'hidden';
            const first = form.querySelector('input:not([type=hidden]),select');
            setTimeout(() => (first || ok).focus(), 0);
        });
    }
    function confirmBox(title, sub, okText, danger) {
        return dialog({ title, sub, ok: okText || '확인', danger, fields: [] }).then((v) => v !== null);
    }
    let toastTimer = null;
    function notice(text, isErr) {
        ensureCss();
        let t = document.querySelector('.oos-toast');
        if (!t) { t = el('div', { className: 'oos-toast', role: 'status', 'aria-live': 'polite' }); document.body.appendChild(t); }
        t.textContent = text;
        t.classList.toggle('err', !!isErr);
        requestAnimationFrame(() => t.classList.add('show'));
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
    }

    // 탈퇴·큰 금액 송금·평소와 다른 송금은 서버가 '최근 5분 안 비밀번호 재확인'을 요구한다(401 step_up_required)
    // → 이 창 → 원래 요청을 다시 보낸다. 같은 순간 여러 요청이 요구하면 창 하나를 같이 기다린다.
    let stepUpWait = null;
    function stepUp(message) {
        if (stepUpWait) return stepUpWait;
        stepUpWait = dialog({
            title: '비밀번호 확인', sub: message || '안전을 위해 비밀번호를 한 번 더 확인할게요',
            fields: [{ name: 'password', label: '비밀번호', type: 'password', autocomplete: 'current-password', maxlength: 128 },
                { name: 'otp', label: '2단계 인증 코드', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 20, placeholder: '6자리 또는 복구 코드', hidden: true }],
            async submit(v, ui) {
                if (!v.password) throw new Error('비밀번호를 입력해 주세요');
                const otp = (v.otp || '').trim();
                try { await api('/auth/step-up', { method: 'POST', body: otp ? { password: v.password, otp } : { password: v.password } }); }
                catch (e) {
                    if (e.code === 'otp_invalid' && !otp) { ui.show('otp', true); throw new Error('2단계 인증 코드를 입력해 주세요'); }
                    throw e;
                }
                return true;
            }
        }).then((v) => !!v).finally(() => { stepUpWait = null; });
        return stepUpWait;
    }
    function askOtp() {
        return dialog({
            title: '2단계 인증', sub: 'OTP 앱의 6자리 코드(또는 복구 코드)를 입력해 주세요.',
            fields: [{ name: 'otp', label: '인증 코드', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 20, placeholder: '123456' }],
            submit(v) { const c = (v.otp || '').trim(); if (!c) throw new Error('코드를 입력해 주세요'); return c; }
        });
    }

    // ── ④ 로그인 상태 · 헤더 · 실시간 알림 ─────────────────────
    let balance = null;
    function setUser(name) {
        gen++;
        user = name || null;
        try { if (user) localStorage.setItem('oos_user', user); else localStorage.removeItem('oos_user'); } catch (e) { /* 무시 */ }
        busy.clear();
        balance = null;
        if (user) sseStart(); else sseStop();
        bindHeader();
        emit('session', user);
    }
    // 로그아웃·세션 만료 공통 정리 — 다음 사람에게 이전 계정의 상태가 남지 않게
    function clearSession() {
        removeKeys(GUEST_KEYS);
        setUser(null);
    }
    // 로그인 전(브라우저)에 해 둔 학습 표시·오늘 퀴즈를 계정으로 옮기고 브라우저 사본은 지운다(옮기는 데 성공한 것만)
    async function syncGuest() {
        const terms = readJSON('outofstock_learned_terms');
        if (Array.isArray(terms) && terms.length) {
            try { await api('/learn/progress/sync', { method: 'POST', body: { terms: terms.filter((t) => typeof t === 'string').slice(0, 500) } }); removeKeys(['outofstock_learned_terms']); }
            catch (e) { /* 다음 로그인 때 다시 */ }
        }
        const q = readJSON('outofstock_daily_quiz');
        if (q && q.done && Array.isArray(q.answers) && q.answers.length && q.answers.every(Number.isInteger)) {
            try {
                const r = await api('/learn/quiz', { method: 'POST', body: { date: q.date, answers: q.answers } });
                removeKeys(['outofstock_daily_quiz']);
                if (r && r.reward > 0) notice(`로그인 전에 푼 퀴즈 보상 +${won(r.reward)}원이 지갑에 들어왔어요`);
            }
            catch (e) { if (e.status === 400 || e.status === 409 || e.status === 422) removeKeys(['outofstock_daily_quiz']); }
        }
    }
    async function login(username, password, remember) {
        const body = { username, password, remember: !!remember };
        let r;
        try { r = await api('/auth/login', { method: 'POST', headers: WEB, body }); }
        catch (e) {
            if (e.code !== 'otp_required') throw e;                // 2단계 인증을 켠 계정 — 코드를 받아 한 번 더
            const otp = await askOtp();
            if (!otp) return null;
            r = await api('/auth/login', { method: 'POST', headers: WEB, body: Object.assign(body, { otp }) });
        }
        setUser(r.username || username);
        await syncGuest();
        refreshBalance();
        return r;
    }
    async function register(username, password) {
        await api('/auth/register', { method: 'POST', headers: WEB, body: { username, password } });
        return login(username, password, false);
    }
    async function logout(next) {
        sseStop();                                                   // 먼저 닫는다 — 서버의 세션 종료 알림이 '종료됐어요' 안내로 되돌아오지 않게
        try { await api('/auth/logout', { method: 'POST' }); } catch (e) { /* 서버가 이미 끝낸 세션이어도 화면은 정리 */ }
        clearSession();
        if (next !== false) location.href = next || 'index.html';
    }
    function goLogin() {
        const here = location.pathname.replace(/^.*\//, '');
        if (here === '' || here === 'index.html') { emit('login-request'); return; }
        location.href = 'index.html#login';
    }

    // 헤더(모든 화면 공통 마크업): .header-balance #headerBalanceAmt · .header-btn-mypage · .header-btn-logout
    const ICON_IN = '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"></path><polyline points="10 17 15 12 10 7"></polyline><line x1="15" y1="12" x2="3" y2="12"></line>';
    let iconOut = null;
    function bindHeader() {
        const bal = document.querySelector('.header-balance'), amt = document.getElementById('headerBalanceAmt');
        const btn = document.querySelector('.header-btn-logout'), my = document.querySelector('.header-btn-mypage');
        if (my) my.setAttribute('aria-label', '마이페이지');          // 좁은 화면은 글자를 숨기고 아이콘만 — 링크 이름은 남긴다
        if (bal) {
            bal.style.display = user ? '' : 'none';
            bal.style.visibility = user && balance != null ? '' : 'hidden';   // 잔액을 받기 전엔 시안의 예시 값을 보여 주지 않는다
        }
        if (amt && balance != null) amt.textContent = won(balance);
        if (btn) {
            const svg = btn.querySelector('svg'), label = btn.querySelector('.header-btn-label');
            if (svg && iconOut == null) iconOut = svg.innerHTML;
            if (svg) svg.innerHTML = user ? iconOut : ICON_IN;
            if (label) label.textContent = user ? '로그아웃' : '로그인';
            btn.setAttribute('aria-label', user ? '로그아웃' : '로그인');
        }
    }
    function setBalance(n) {
        if (n == null || !isFinite(n)) return;
        balance = Number(n);
        bindHeader();
        emit('wallet', balance);
    }
    async function refreshBalance() {
        if (!user || !ONLINE) return null;
        try { const d = await api('/wallet'); setBalance(d.balance); return d; } catch (e) { return null; }
    }
    async function badges() {
        if (!user || !ONLINE) return null;
        try { const d = await api('/me/badges'); emit('badges', d); return d; } catch (e) { return null; }
    }

    // 실시간 알림(SSE) — 잔액·채팅·친구 요청·세션 종료를 서버가 바뀐 순간 밀어 준다(폴링 대신).
    // 알림은 '힌트' — 놓쳐도 화면은 다음 조회 때 맞춰진다. 끊기면 지수 백오프로 다시 연결(가벼운 요청으로 토큰 갱신 유도 뒤).
    let es = null, sseRetry = 0, sseTimer = null;
    const sseData = (e) => { try { return JSON.parse(e.data || '{}'); } catch (err) { return {}; } };
    function sseStart() {
        if (!ONLINE || !user || es || !window.EventSource) return;
        clearTimeout(sseTimer);
        es = new EventSource('/events');
        es.addEventListener('hello', () => { sseRetry = 0; });
        es.addEventListener('wallet', (e) => { const d = sseData(e); if (d.balance != null) setBalance(d.balance); });
        es.addEventListener('chat', (e) => { emit('chat', sseData(e)); badges(); });
        es.addEventListener('friends', (e) => { emit('friends', sseData(e)); badges(); });
        es.addEventListener('session', () => { badges(); });   // 내 다른 기기일 수도 — 이 세션이 끝났으면 확인 요청이 401 → 정리
        es.addEventListener('reconnect', () => { sseStop(); sseSoon(500); });
        es.onerror = () => {
            if (es && es.readyState === EventSource.CLOSED) { sseStop(); sseSoon(Math.min(60000, 2000 * 2 ** sseRetry++)); }
        };
    }
    function sseStop() { clearTimeout(sseTimer); if (es) { es.close(); es = null; } }
    function sseSoon(ms) {
        clearTimeout(sseTimer);
        sseTimer = setTimeout(async () => { if (!user) return; await badges(); sseStart(); }, ms);
    }

    // 다른 탭에서 로그인·로그아웃 → 이 탭도 같은 상태로(예전 계정 화면이 남지 않게)
    window.addEventListener('storage', (e) => {
        if (e.key !== 'oos_user') return;
        const next = e.newValue || null;
        if (next === user) return;
        gen++; user = next; balance = null; busy.clear();
        if (user) { sseStart(); refreshBalance(); } else sseStop();
        bindHeader();
        emit('session', user);
    });

    actions({ headerLogout: () => (user ? logout() : goLogin()) });

    // 헤더 검색창 — 종목명·코드·초성 자동완성(/search/suggest, 국내·해외 함께). 고르면 실시간 종목 화면으로.
    // 우리 서버 없이 열면(디자인 모드) 아무것도 하지 않는다. 늦게 온 응답은 버린다.
    function headerSearch() {
        const input = document.querySelector('header .search-box input');
        if (!input || !ONLINE || input.dataset.oosSearch) return;
        input.dataset.oosSearch = '1';
        const box = input.closest('.search-box');
        const st = document.createElement('style');
        st.textContent = '.oos-sugg{position:absolute;top:calc(100% + 4px);left:0;right:0;z-index:5000;background:#fff;border:1px solid #cbd5e1;border-radius:10px;box-shadow:0 10px 24px rgba(15,23,42,.12);overflow:hidden;max-height:360px;overflow-y:auto}'
            + '.oos-sugg button{display:flex;align-items:center;gap:8px;width:100%;padding:9px 12px;border:none;background:none;font:inherit;font-size:13px;color:#0f172a;cursor:pointer;text-align:left}'
            + '.oos-sugg button:hover,.oos-sugg button.on{background:#eef2ff}'
            + '.oos-sugg .n{font-weight:700;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
            + '.oos-sugg .c{color:#475569;font-size:11.5px}.oos-sugg .p{font-size:12px;color:#475569}.oos-sugg .up{color:#c81e1e}.oos-sugg .down{color:#1d4ed8}'
            + '.oos-sugg .t{font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:5px;background:#eef2ff;color:#3730a3}'
            + '.oos-sugg .e{padding:12px;font-size:12.5px;color:#475569}';
        document.head.appendChild(st);
        const list = document.createElement('div');
        list.className = 'oos-sugg'; list.hidden = true; list.id = 'oosSugg'; list.setAttribute('role', 'listbox');
        box.appendChild(list);
        input.setAttribute('role', 'combobox'); input.setAttribute('aria-controls', 'oosSugg');
        input.setAttribute('aria-expanded', 'false'); input.setAttribute('autocomplete', 'off');
        let seq = 0, timer = null, items = [], cur = -1;
        const close = () => { list.hidden = true; cur = -1; input.setAttribute('aria-expanded', 'false'); };
        const go = (it) => { location.href = `stocks.html?mkt=${it.type === 'us' ? 'us' : 'kr'}&code=${encodeURIComponent(it.ticker)}`; };
        const price = (it) => (it.price == null ? '' : it.type === 'us' ? '$' + Number(it.price).toFixed(2) : won(it.price) + '원');
        const draw = () => {
            list.innerHTML = items.length ? items.map((it, i) => {
                const cr = Number(it.change_rate) || 0;
                return `<button type="button" role="option" data-i="${i}" class="${i === cur ? 'on' : ''}"><span class="t">${it.type === 'us' ? '해외' : '국내'}</span>`
                    + `<span class="n">${esc(it.name)} <span class="c">${esc(it.ticker)}</span></span>`
                    + `<span class="p">${price(it)} <span class="${cr > 0 ? 'up' : cr < 0 ? 'down' : ''}">${it.change_rate == null ? '' : (cr > 0 ? '+' : '') + cr.toFixed(2) + '%'}</span></span></button>`;
            }).join('') : '<div class="e">찾는 종목이 없어요</div>';
            list.hidden = false; input.setAttribute('aria-expanded', 'true');
        };
        async function fetchNow(q) {
            const my = ++seq;
            let d;
            try { d = await api('/search/suggest?kind=all&limit=8&q=' + encodeURIComponent(q)); } catch (e) { d = { items: [] }; }
            if (my !== seq || input.value.trim() !== q) return null;
            items = d.items || []; cur = items.length ? 0 : -1; draw();
            return items;
        }
        input.addEventListener('input', () => {
            clearTimeout(timer);
            const q = input.value.trim();
            items = []; seq++; close();                               // 이전 검색어의 결과로 엔터가 가지 않게
            if (!q) return;
            timer = setTimeout(() => fetchNow(q), 150);
        });
        input.addEventListener('keydown', async (e) => {
            if (e.isComposing || e.keyCode === 229) return;          // 한글 조합 중 엔터·화살표는 조합이 끝난 뒤
            if (e.key === 'Escape') { close(); return; }
            if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !list.hidden && items.length) {
                e.preventDefault();
                cur = (cur + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; draw();
            } else if (e.key === 'Enter') {
                e.preventDefault();
                const q = input.value.trim(); if (!q) return;
                if (!list.hidden && items[cur]) { go(items[cur]); return; }
                clearTimeout(timer);                                   // 디바운스를 기다리지 않고 바로 찾아 1순위로
                const r = await fetchNow(q);
                if (r && r.length) go(r[0]);
            }
        });
        list.addEventListener('mousedown', (e) => {                   // blur 보다 먼저
            const b = e.target.closest('button[data-i]'); if (!b) return;
            e.preventDefault(); go(items[Number(b.dataset.i)]);
        });
        input.addEventListener('blur', () => setTimeout(close, 120));
        input.addEventListener('focus', () => { if (input.value.trim() && items.length) draw(); });
    }

    function start() {
        kbd(document.body);
        new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach((n) => { if (n.nodeType === 1) kbd(n); })))
            .observe(document.body, { childList: true, subtree: true });
        bindHeader();
        headerSearch();
        if (user) { refreshBalance(); sseStart(); }
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();

    window.OOS = Object.freeze({
        online: ONLINE,
        get user() { return user; },
        loggedIn: () => !!user,
        get balance() { return balance; },
        /** 서버 시각(ms) — 응답 Date 헤더로 맞춘 시계 차이를 더한 값(모의투자 시즌 시계처럼 서버 시각 기준 화면용) */
        serverNow: () => Date.now() + clockSkew,
        api, actions, on, emit, once, dialog, confirm: confirmBox, notice, stepUp,
        login, register, logout, goLogin, setBalance, refreshBalance, badges,
        esc, won, idemKey, safeUrl, readJSON, writeJSON, removeKeys
    });
})();
