// ============================================================
//  뉴스 데이터 로더 (뉴스 페이지 · 홈의 '주요 뉴스' 카드 공용)
//
//  ▶ 지금은 assets/news-mock.js 의 샘플 데이터를 보여줍니다.
//  ▶ 백엔드가 준비되면:
//      1) 아래 API_BASE 에 서버 주소를 넣고 (예: 'https://api.example.com')
//      2) html 의 <script src="assets/news-mock.js"> 줄을 지우면 끝입니다.
//     화면은 GET {API_BASE}/api/news/home 응답을 그대로 사용합니다. (데이터 형식은 news-mock.js 맨 위 주석 참고)
// ============================================================
const NEWS_CONFIG = {
    API_BASE: ''
};

async function loadNewsHome() {
    if (NEWS_CONFIG.API_BASE) {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 10000);
        try {
            const res = await fetch(NEWS_CONFIG.API_BASE.replace(/\/$/, '') + '/api/news/home', { cache: 'no-store', signal: ctrl.signal, credentials: 'include' });
            if (!res.ok) throw new Error('서버 응답 오류 (HTTP ' + res.status + ')');
            const j = await res.json();
            const d = j && j.data ? j.data : j;
            if (!d || !Array.isArray(d.todayTop) || !Array.isArray(d.latest)) throw new Error('데이터 형식이 올바르지 않아요');
            return Object.assign({}, d, { _sample: false });
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
