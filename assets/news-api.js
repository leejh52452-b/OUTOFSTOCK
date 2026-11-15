// ============================================================
//  뉴스 데이터 로더 (뉴스 페이지 · 홈의 '주요 뉴스' 카드 공용)
//
//  ▶ 서버(우리 백엔드)가 이 화면을 내보내면 같은 출처의 GET /api/news/home 응답을 그대로 씁니다.
//     (API_BASE 를 비워 두면 '이 페이지를 연 서버'. 다른 서버를 쓰려면 주소를 넣으세요 — 예: 'https://api.example.com')
//     서버 오류면 오류를 그대로 던져 화면의 '불러오지 못했어요'(다시 시도) 상태로 — 가상 기사로 대신 채우지 않습니다.
//  ▶ 우리 서버 없이 열었을 때(파일로 열기·Live Server·GitHub Pages — 디자인 확인용)만 assets/news-mock.js 의 샘플을
//     보여주고 '샘플' 표시를 켭니다. (우리 서버가 내보낸 화면에만 <meta name="oos-server"> 표식이 붙습니다)
//     화면은 응답의 data 객체를 그대로 사용합니다. (데이터 형식은 news-mock.js 맨 위 주석 참고)
//  ▶ 서버 응답의 sample:true(예시 데이터로 채운 DB) 도 '샘플' 표시로 이어집니다.
// ============================================================
const NEWS_CONFIG = {
    API_BASE: ''
};

function newsApiBase() {
    if (NEWS_CONFIG.API_BASE) return NEWS_CONFIG.API_BASE.replace(/\/$/, '');
    const served = /^https?:$/.test(location.protocol) && !!document.querySelector('meta[name="oos-server"]');
    return served ? '' : null;                                  // null = 우리 서버 없음(디자인 확인용)
}

async function loadNewsHome() {
    const base = newsApiBase();
    if (base !== null) {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 10000);
        try {
            const res = await fetch(base + '/api/news/home', { signal: ctrl.signal, credentials: base ? 'omit' : 'same-origin' });
            if (!res.ok) throw new Error('서버 응답 오류 (HTTP ' + res.status + ')');
            const j = await res.json();
            const d = j && j.data ? j.data : j;
            if (!d || !Array.isArray(d.todayTop) || !Array.isArray(d.latest)) throw new Error('데이터 형식이 올바르지 않아요');
            return Object.assign({}, d, { _sample: !!d.sample });
        } finally { clearTimeout(timer); }
    }
    if (typeof NEWS_MOCK === 'undefined') throw new Error('뉴스 데이터가 연결되지 않았어요');
    await new Promise((r) => setTimeout(r, 300));   // 실제 통신처럼 로딩 화면이 잠깐 보이도록
    return Object.assign({}, JSON.parse(JSON.stringify(NEWS_MOCK)), { _sample: true });
}

function timeAgo(iso) {
    const t = new Date(iso).getTime();
    if (isNaN(t)) return '';
    const min = Math.floor((Date.now() - t) / 60000);
    if (min < 1) return '방금 전';
    if (min < 60) return min + '분 전';
    const h = Math.floor(min / 60);
    if (h < 24) return h + '시간 전';
    const d = Math.floor(h / 24);
    if (d < 7) return d + '일 전';
    const dt = new Date(t);
    return (dt.getMonth() + 1) + '.' + String(dt.getDate()).padStart(2, '0');
}

function fmtViews(n) {
    n = Number(n) || 0;
    return n >= 10000 ? (n / 10000).toFixed(1).replace(/\.0$/, '') + '만' : n.toLocaleString('ko-KR');
}
