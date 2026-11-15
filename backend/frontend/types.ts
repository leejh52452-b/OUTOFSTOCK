// OUTOFSTOCK API — 응답 타입 + 그대로 쓸 수 있는 요청 래퍼 (2026-10-22 판, FRONTEND_API_GUIDE.md 와 함께)
// 백엔드 실제 응답 기준. 필드 추가가 필요하면 주원에게.
// 사용 예:  const r = await api<MarketStocks>("/market/stocks?sort=value&limit=10");

// 같은 출처가 기본 — 개발 서버는 Vite 프록시(가이드 1.1)로 API 경로를 VM 에 넘기고 여기는 비워 둔다.
// 다른 출처에서 Bearer 로 부를 때만(가이드 1.2) 전체 주소: "http://<VM_IP>" (nginx 없이 띄웠으면 ":8000")
export const BASE_URL = "";

// ── 공통 유틸 ──────────────────────────────────────────────
/** 422(입력 형식 오류)만 detail 이 배열. 나머지는 문자열. 정책 거부(인증·권한·CSRF)는 code 가 붙는다. */
export interface ValidationIssue { type: string; loc: (string | number)[]; msg: string; ctx?: Record<string, unknown>; }
export type AuthCode =
  | "token_expired" | "step_up_required" | "login_required" | "token_invalid"
  | "session_idle" | "session_expired" | "session_revoked" | "device_mismatch" | "refresh_reuse"
  | "invalid_credentials" | "otp_required" | "otp_invalid" | "step_up_failed" | "account_locked"
  | "csrf_failed" | "fds_blocked" | "forbidden" | "mfa_required" | "ip_blocked" | "device_untrusted";
export interface ApiError { status: number; code: AuthCode | string; detail: string | ValidationIssue[]; message: string; }

/** 화면에 보여 줄 한 줄 메시지. */
export function errorText(detail: string | ValidationIssue[] | undefined): string {
  if (typeof detail === "string") return detail;
  return detail?.[0]?.msg ?? "요청 실패";
}

/** 새 멱등키 — 돈·매매 POST 에 클릭마다 하나(같은 키로 다시 보내면 서버가 한 번만 처리). */
export function idemKey(): string {
  return crypto.randomUUID ? crypto.randomUUID()
    : Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
}

/** CSRF 토큰(쿠키 oos_csrf, HTTPS 면 __Host-oos_csrf) — POST·DELETE 의 X-CSRF-Token 헤더로. */
export function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)(?:__Host-)?oos_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

/** 앱이 한 번 설정하는 훅 — 401 을 화면 동작으로 바꾼다. */
export const authHooks = {
  /** 401 step_up_required: 비밀번호 확인 창 → stepUp(password, otp) 까지 마쳤으면 true(원래 요청을 다시 보낸다). */
  stepUp: async (_message: string): Promise<boolean> => false,
  /** 세션이 끝남(유휴·만료·다른 곳에서 종료·탈취 의심 등) → 로그인 화면 + 메시지. */
  sessionEnded: (_code: string, _message: string): void => {},
};

/** Bearer 모드(가이드 1.2)에서만 채운다 — 메모리에만 둘 것(localStorage 금지). 쿠키 모드면 null 그대로. */
export const bearer: { access: string | null; refresh: string | null } = { access: null, refresh: null };

const AUTH_PATHS = /^\/auth\/(login|register|logout|refresh|step-up)\b/;
let refreshing: Promise<boolean> | null = null;

/** 접근 토큰 갱신(갱신 토큰 회전). 동시에 여러 요청이 401 을 받아도 갱신은 한 번만. */
export function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    const init: RequestInit = bearer.refresh
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: bearer.refresh }) }
      : { method: "POST", credentials: "same-origin", headers: { "X-Client": "web", "X-CSRF-Token": csrfToken() } };
    refreshing = fetch(`${BASE_URL}/auth/refresh`, init)
      .then(async r => {
        if (!r.ok) return false;
        if (bearer.refresh) { const j: RefreshResult = await r.json(); bearer.access = j.token ?? null; bearer.refresh = j.refresh_token ?? null; }
        return true;
      })
      .catch(() => false)
      .finally(() => { setTimeout(() => { refreshing = null; }, 0); });
  }
  return refreshing;
}

/** 모든 API 호출. 실패 시 ApiError throw. 401 token_expired → 갱신 후 재시도, step_up_required → 확인 창 후 재시도. */
export async function api<T>(
  path: string,
  opts: { method?: string; body?: unknown; idempotencyKey?: string; headers?: Record<string, string> } = {},
  _try = 0,
): Promise<T> {
  const method = (opts.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(opts.headers ?? {}) };
  if (bearer.access) headers["Authorization"] = `Bearer ${bearer.access}`;
  else if (method !== "GET" && method !== "HEAD") { const c = csrfToken(); if (c) headers["X-CSRF-Token"] = c; }
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;    // 재시도에도 같은 키(처리 전이므로)
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method, headers, credentials: "same-origin",
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch {
    throw { status: 0, code: "", detail: "서버에 연결할 수 없어요", message: "서버에 연결할 수 없어요 — 잠시 뒤 다시 시도해 주세요" } as ApiError;
  }
  const data = await res.json().catch(() => ({ detail: "요청 실패" }));
  if (res.ok) return data as T;
  const code: string = data.code ?? res.headers.get("X-Auth-Code") ?? "";
  const err: ApiError = { status: res.status, code, detail: data.detail ?? "요청 실패", message: errorText(data.detail) };
  if (res.status === 401 && !AUTH_PATHS.test(path)) {
    if (code === "step_up_required") {
      if (_try < 2 && await authHooks.stepUp(err.message)) return api<T>(path, opts, _try + 1);
      throw err;                                            // 확인을 취소했다 — 세션은 그대로
    }
    if (code === "token_expired" && _try < 2 && await refreshSession()) return api<T>(path, opts, _try + 1);
    authHooks.sessionEnded(code, err.message);
  }
  throw err;
}

// ── 인증 ───────────────────────────────────────────────────
/** 웹 로그인(쿠키). OTP 를 켠 계정은 먼저 401 otp_required → otp 를 넣어 다시.
 *  remember=false(기본): 세션 쿠키 — 브라우저를 닫으면 끝(공용 PC). true: '로그인 유지' — 12시간 쿠키(서버 만료 규칙은 같다). */
export function login(username: string, password: string, otp?: string, remember = false): Promise<LoginCookieResult> {
  return api<LoginCookieResult>("/auth/login", { method: "POST", body: { username, password, otp, remember }, headers: { "X-Client": "web" } });
}
/** Bearer 로그인(가이드 1.2) — 토큰을 bearer 에 넣는다. */
export async function loginBearer(username: string, password: string, otp?: string): Promise<LoginBearerResult> {
  const r = await api<LoginBearerResult>("/auth/login", { method: "POST", body: { username, password, otp } });
  bearer.access = r.token; bearer.refresh = r.refresh_token;
  return r;
}
export function stepUp(password: string, otp?: string): Promise<StepUpResult> {
  return api<StepUpResult>("/auth/step-up", { method: "POST", body: { password, otp } });
}
export async function logout(): Promise<{ ok: true; ended: boolean }> {
  try { return await api("/auth/logout", { method: "POST" }); } finally { bearer.access = bearer.refresh = null; }
}

export interface LoginCookieResult { username: string; mode: "cookie"; expires_in: number; mfa: boolean; }
export interface LoginBearerResult {
  username: string; token: string; refresh_token: string; token_type: "Bearer"; expires_in: number; mfa: boolean;
}
export interface RefreshResult { ok: true; mode?: "cookie"; token?: string; refresh_token?: string; token_type?: "Bearer"; expires_in: number; }
export interface StepUpResult { ok: true; valid_for: number; mfa: boolean; }
export interface SessionItem {
  session_id: string; client: "web" | "api"; device: string; ip_net: string | null;
  created_at: string; last_seen_at: string; expires_at: string; mfa: boolean; current: boolean;
}
export interface Sessions { items: SessionItem[]; }
export interface MfaStatus { enabled: boolean; }
export interface MfaSetup { secret: string; otpauth_uri: string; period: number; digits: number; }
export interface MfaConfirm { enabled: true; recovery_codes: string[]; }

// ── 실시간 알림(SSE) — new EventSource("/events") ──────────
export interface SseEvents {
  hello: { heartbeat: number };
  wallet: { balance: number };
  chat: { from: string; id: number };
  friends: { type: "request"; from: string } | { type: "accepted"; by: string };
  session: { reason: string; sid: string };
  reconnect: { reason: "lifetime" };
}

// ── 공통 모델 ──────────────────────────────────────────────
/** 등락률·수익률은 % (이미 ×100). 양수=빨강(상승), 음수=파랑(하락). */
export interface IndexItem {
  code: string; name: string; value: number; change: number; change_rate: number;
  /** kis(실시간)·yahoo·krx(전 영업일)·fdr·gen(표본) — null 이면 출처 기록 없는 예전 값 */
  source?: "kis" | "yahoo" | "krx" | "fdr" | "kept" | "gen" | null;
  as_of?: string | null;            // 그 값이 속한 거래일
  ts?: string | null;               // 받은 시각
  status?: "live" | "delayed" | "close" | "sample" | "unknown";
  stale?: boolean;                  // 기준일이 오래됨(수집 중단)
  history?: number[];               // 미니차트(최근 종가, KOSPI·KOSDAQ 만)
}
/** 장 상태 — 국내: pre|open|closed|weekend|holiday, 미국: +after */
export type MarketPhase = "pre" | "open" | "after" | "closed" | "weekend" | "holiday";
export interface MarketStock {
  rank: number; ticker: string; name: string; market: string; sector: string;
  price: number; change: number; change_rate: number; volume: number; value: number; marcap: number; live: boolean;
  /** live(장중 실시간) · close(종가) · sim(시뮬레이션) */
  status?: string; session_date?: string | null;
}
export interface NewsItem { title: string; url: string; source: string; published_at: string; published_ts?: string | null; }
export interface TaggedNews extends NewsItem { type: "호재" | "악재" | "중립"; }
/** GET /stocks/{ticker}/news?limit=&offset= — url 은 http(s) 원문만(없으면 ""). summary = 최근 7일 이 종목 기사 수. */
export interface StockNews {
  ticker: string;
  items: TaggedNews[];
  summary: { days: number; since: string | null; pos: number; neg: number; neu: number; total: number };
}
/** 재무 비율 1행(최신 회계연도). op_margin·net_margin 은 %(매출 대비), debt_ratio 는 %(부채/자본). fs_basis: CFS 연결 · OFS 별도 · null(표본) */
export interface PeerRow {
  ticker: string; name: string; sector: string | null; fiscal_year: number | null; revenue: number | null;
  op_margin: number | null; net_margin: number | null; debt_ratio: number | null; fs_basis: "CFS" | "OFS" | null;
  /** peers 항목에만: 재무 유사도 순위(1~3)·정규화 거리 */
  rank?: number; distance?: number;
}
/** GET /stocks/{ticker}/peers — 비교 기업. 재무 데이터가 없으면 { available:false, reason } */
export interface StockPeers {
  ticker: string; available: boolean; reason?: string;
  /** dart(실재무) · sample(표본 시세 모드의 데모 재무 — '표본' 배지) */
  source?: "dart" | "sample";
  base?: PeerRow; peers?: PeerRow[]; method?: string; note?: string;
}
/** 추천 상품(OUTOFSTOCK_추천상품_선정.docx). price 는 DB 에 시세가 있는 개별 주식만(ETF 는 null). */
export interface Product {
  code: string; name: string; manager: string; grade: string; risk: "저위험" | "중위험" | "고위험";
  kind: string; type: "ETF" | "주식"; desc: string;
  source: { text: string; dart_url: string | null };
  price: number | null; change_rate: number | null; status: string | null;
}
export interface Evidence { title: string; type: "호재" | "악재"; url?: string; source?: string; }
/** 뉴스 홈 aiPicks 의 한 종목 */
export interface Pick {
  ticker: string; name: string; sentiment: number; pos_ratio: number; news_count: number; evidence: Evidence[];
}
export interface TodayPicks { recommend: Pick | null; avoid: Pick | null; }

// ── 홈 ─────────────────────────────────────────────────────
export interface Indices { items: IndexItem[]; source?: string; phase?: MarketPhase; index_source?: string | null; }
export interface MarketStocks { sort: string; live: boolean; as_of: string | null; items: MarketStock[]; }
export interface RecoProducts {
  profile: "안정형" | "중립형" | "공격형"; grade: string; rule: string; subtitle: string; items: Product[];
  excluded: string; notice: string; checked_at: string;
}
export type RecoFactor = "momentum" | "technical" | "value" | "quality" | "sentiment" | "flow" | "lowrisk";
export interface Top5Item {
  rank: number; ticker: string; name: string; market: string; sector?: string;
  tag: string; tags: string[]; brief?: string;
  /** 0~100 — 같은 날 적격 종목 사이의 종합점수 백분위 */
  score: number; grade?: "S" | "A" | "B" | "C" | "D"; composite?: number;
  reasons?: string[]; cautions?: string[];
  factors?: Record<RecoFactor, number>;      // 표준화 점수(z, 0=평균)
  contrib?: Record<RecoFactor, number>;      // 가중치 × z (합 = composite)
  metrics?: Record<string, number | string | null>;
  risk?: { level: "낮음" | "보통" | "높음" | "매우 높음" | "알 수 없음"; volatility: number | null; mdd: number | null };
  entry_date?: string; entry_price?: number;
  price: number; change_rate: number | null; status: string | null;
}
export interface RecoModelInfo {
  version: string; regime: "bull" | "neutral" | "bear"; regime_label: string; breadth: number | null;
  weights: Record<RecoFactor, number>; factors_used: RecoFactor[]; universe: number; eligible: number;
  excluded: Record<string, number>; profile: string | null; profile_rule: string | null;
  constraints?: { max_corr: number; max_per_sector: number }; diversified_out?: Record<string, number>;
}
export interface Top5 {
  as_of: string | null; items: Top5Item[]; note: string; source: string;
  profile?: "안정형" | "중립형" | "공격형" | null; model?: RecoModelInfo | null;
}
export interface RecoBacktest {
  available: boolean; reason?: string; model_version?: string;
  params: { days: number; horizon: number; step: number; limit: number; profile: string | null; cost_bps: number };
  /** 비율은 소수(0.021 = 2.1%) */
  summary?: { periods: number; avg_top: number; avg_bench: number; avg_excess: number; win_rate: number;
              hit_rate: number | null; ic_mean: number | null; icir: number | null;
              cum_top: number; cum_bench: number; max_drawdown: number };
  verdict?: string; caveats?: string[];
  curve?: { date: string; top: number; bench: number }[];
  periods?: { date: string; exit: string; top: number; bench: number; excess: number; ic: number | null;
              hits: number; n: number; regime: "bull" | "neutral" | "bear"; picks: string[] }[];
}
/** GET /reco/model/monitor — 모형 모니터링(입력 안정성 PSI · IC 추이) */
export type MonitorStatus = "안정" | "주의" | "재검토" | "데이터 부족";
export interface ModelMonitor {
  available: boolean; reason?: string; model_version?: string;
  ref_date?: string; cur_date?: string; status?: "안정" | "주의" | "재검토";
  /** 실제 쓴 비교 기준(20/40/60/120/250 거래일 중 요청 값에 가장 가까운 값) */
  ref_days?: number;
  psi?: { var: string; label: string; kind: "market" | "valuation" | "financial"; kind_label: string;
          psi: number | null; status: MonitorStatus; n_ref: number; n_cur: number;
          ref_median: number | null; cur_median: number | null }[];
  thresholds?: { warn: number; alert: number; rule: string };
  population?: { ref: number; cur: number }; regime?: { ref: string; cur: string };
  ic?: { series: { date: string; ic: number | null }[]; recent_mean: number | null; overall_mean: number | null;
         status: "양호" | "저하" | "데이터 부족" };
  actions?: string[]; note?: string;
  /** PSI 가 가장 큰 변수(위 psi 배열의 한 항목) */
  worst?: { var: string; label: string; psi: number | null; status: MonitorStatus } | null;
}
/** GET /reco/model/challengers — Champion–Challenger 비교 */
export interface ModelComparison {
  available: boolean; reason?: string; model_version?: string;
  params?: { days: number; horizon: number; step: number; limit: number; cost_bps: number };
  models?: { key: "champion" | "equal" | "momentum" | "lowrisk"; label: string; weights: Record<string, number> | null;
             periods: number; avg_excess: number | null; win_rate: number | null; ic_mean: number | null;
             cum_excess: number | null;
             vs_champion?: { n: number; mean_diff: number | null; t_stat: number | null;
                             champion_better_share: number | null; verdict: string } }[];
  method?: string; caveats?: string[];
}
export interface Top5ReviewItem {
  rank: number; ticker: string; name: string; tag: string; entry_date: string; entry_price: number;
  price: number; price_date: string | null; return_pct: number;
}
export interface Top5Review {
  available: boolean; days: number; as_of?: string; items?: Top5ReviewItem[]; top?: Top5ReviewItem;
  avg_return?: number; wins?: number; count?: number; note: string;
  /** 같은 기간 유니버스 평균(벤치마크)·초과수익(%p) — 그날 실제 공개 기록(스냅샷)이면 from_snapshot */
  bench_return?: number | null; excess_return?: number | null; bench_count?: number;
  from_snapshot?: boolean; model_version?: string;
}

// ── 리스크 · 재무(종목 상세 심화) ───────────────────────────
/** /stocks/{t}/soundness 의 merton — 시장 기반 부도거리·부도확률(Merton/KMV) */
export interface MertonCredit {
  ok: boolean; reason?: string; dd?: number; pd?: number; pd_band?: "매우 낮음" | "낮음" | "보통" | "높음" | "매우 높음";
  asset_value?: number; asset_vol?: number; converged?: boolean;
  inputs?: { equity: number; equity_vol: number; default_point: number; default_point_rule: string;
             risk_free: number; horizon_years: number; days: number; as_of: string };
  note?: string;
}
/** GET /stocks/{t}/var-backtest — 롤링 아웃오브샘플 VaR 백테스트 + 바젤 신호등 */
export interface VarBacktest {
  ticker: string; ok: boolean; reason?: string; confidence?: number;
  method?: "rolling_oos" | "in_sample"; window?: number; period?: { from: string; to: string };
  observations?: number; var?: number; breaches?: number; expected_breaches?: number; breach_rate?: number | null;
  kupiec_lr?: number | null; kupiec_pvalue?: number | null; reject_model_at_95?: boolean;
  christoffersen?: { lr_ind: number; p_ind: number; reject_ind: boolean; clustered_breaches: number;
                     lr_cc: number; p_cc: number; reject_cc: boolean };
  traffic_light?: { zone: "green" | "yellow" | "red"; label: string; green_max: number; yellow_max: number; cum_prob: number };
  recent_breaches?: string[]; verdict?: string;
}
/** GET /invest/risk (로그인) — 내 포트폴리오 리스크 · 스트레스 테스트. 손실(loss)은 음수(원) */
export interface InvestRisk {
  ok: boolean; reason?: string; stats_ok?: boolean; total_value?: number; positions?: number; tickers?: number;
  days?: number; period?: { from: string; to: string };
  var_95?: number; var_95_krw?: number; es_975?: number; es_975_krw?: number; annual_volatility?: number | null;
  beta?: number; beta_complete?: boolean; beta_source?: "kospi" | "assumed";
  scenarios?: { name: string; market_shock: number; loss: number; loss_pct: number }[];
  historical?: { name: string; loss: number; loss_pct: number }[];
  concentration?: { hhi: number; effective_n: number; max_weight: number; limit: number; limit_breach: boolean };
  items?: { ticker: string; name: string; value: number; weight: number; beta: number | null }[];
  assumptions?: string[];
}

// ── 학습 ───────────────────────────────────────────────────
/** GET /learn/dictionary — terms.js 의 TERMS 항목과 같은 필드 + notes/detail/box/src. */
export interface Term {
  name: string; en: string; level: "basic" | "mid"; cat: string;
  analogy: string; explain: string; alias: string[]; related: string[];
  practice: "golden" | "rsi" | "volume" | "ma" | "sideways" | "divergence" | "any" | null;
  box: "비유" | "정의";                                  // '정의'면 비유 칸 제목을 '한 줄 정의'로
  notes: { name: string; define: string; howto: string; caution: string }[];   // 팀 「주식 기초 용어 사전」 docx
  detail: { kind: "indicator" | "technique"; title: string; desc: string; rows: [string, string][] } | null;
  src: "team" | "doc" | "ours";
}
export interface LearnDictionary {
  version: string; count: number; categories: string[]; levels: Record<string, string>;
  practice: Record<string, { label: string; scenario: string | null }>; sources: string[]; items: Term[];
}
/** GET /learn/today — 홈 '1분 용어 학습'·학습하기 '오늘의 용어'(같은 날엔 같은 용어). */
export interface LearnToday {
  kind: string; name: string; term: string; en: string; desc: string; analogy: string;
  box: "비유" | "정의"; level: "basic" | "mid"; cat: string; tab: "terms" | "ind" | "tech";
}
export interface LearnProgress { done: string[]; count: number; total: number; }
/** Daily Stock Quiz — 문제는 화면(assets/daily-quiz.js)이 날짜로 만들고, 서버가 같은 공식으로 다시 만들어 채점한다.
 *  answers = 화면에 보인 순서의 보기 번호(0~3) 5개. 하루 1번 — 다시 내면 처음 결과(duplicate: true). 날짜는 오늘(KST) ±1일만.
 *  보상: reward = 서버가 지갑에 넣은 금액(점수 × reward_per_correct, 한국 날짜 하루 1번 — 이미 받은 날은 0 + reward_skipped). */
export interface QuizState {
  date: string; done: boolean; answers: number[] | null; score: number | null; total: number;
  total_days: number; duplicate?: boolean;
  reward: number; reward_per_correct: number; reward_skipped?: string; /** 지급한 제출에만 — 지급 뒤 잔액 */ balance?: number;
}
export function quizState(date?: string): Promise<QuizState> {
  return api<QuizState>("/learn/quiz" + (date ? `?date=${encodeURIComponent(date)}` : ""));
}
export function submitQuiz(date: string, answers: number[]): Promise<QuizState> {
  return api<QuizState>("/learn/quiz", { method: "POST", body: { date, answers } });
}

// ── 실습 ───────────────────────────────────────────────────
export interface Candle { trade_date: string; open: number; high: number; low: number; close: number; volume: number; }
/** 지표 오버레이: series 와 같은 길이, 워밍업 구간은 null. */
export type Overlays = Record<string, (number | null)[]>;
export interface Progress { current: number; total: number; pct: number; }
export interface PracticeAccount {
  cash: number; holding_qty: number; holding_avg: number; holding_value: number;
  pnl: number; pnl_pct: number; total_asset: number; return_rate: number; start_cash: number; buyable: number;
}
export interface PracticeState {
  session_id: number; scenario_id: string; name: string; stock: string; ticker: string; difficulty: string;
  objective: string; indicators: string[]; status: "active" | "done";
  cursor_date: string; start_date: string; end_date: string;
  progress: Progress; has_next: boolean; price: number; day_change: number;
  series: Candle[]; overlays: Overlays; account: PracticeAccount;
  final_return: number | null; best_return: number | null;
}
export interface ScenarioItem {
  scenario_id: string; name: string; stock: string; ticker: string; difficulty: "초급" | "중급" | "고급";
  start_date: string; end_date: string; objective: string; indicators: string[]; pattern: string;
  status: "none" | "active" | "done"; progress: Progress | null;
  best_return: number | null; attempts: number; locked: boolean; session_id: number | null;
}
export interface PracticeScenarios {
  stats: { done: number; total: number; by_difficulty: Record<string, number> };
  active_scenario: string | null; items: ScenarioItem[];
}
/** /practice/home 은 전체 상태가 아니라 '요약'(series·account 없음) — 이어 하려면 GET /practice/{session_id}. */
export interface PracticeHomeActive {
  session_id: number; scenario_id: string; name: string; cursor_date: string;
  progress: Progress; total_asset: number; return_rate: number;
}
export interface PracticeHome { active: PracticeHomeActive | null; }
/** 웹 실습 기록(팀 practice.html — 한 판씩). 수익률은 화면 계산값이라 서버는 범위만 확인(−100% ~ +1000%),
 *  가상머니와 잇지 않는다. 시간당 120회(PRACTICE_RECORD_MAX_PER_HOUR). real 모드만 entry_date(YYYY-MM-DD). */
export type PracticeTech = "golden" | "rsi" | "pullback" | "support";
export interface PracticeTechSummary {
  tries: number; best: number | null; real_tries: number; real_best: number | null;
  /** 매매까지 마친 판 수(마이페이지 '완료') */ traded: number;
}
export type PracticeSummary = Partial<Record<PracticeTech, PracticeTechSummary>>;
export interface PracticeRecordItem {
  id: number; tech: PracticeTech; mode: "practice" | "real"; traded: boolean; ret: number | null;
  real_entry: string | null; created_at: string;
}
export interface PracticeRecords { summary: PracticeSummary; items: PracticeRecordItem[]; total: number; has_more: boolean; }
export function practiceRecords(limit = 20, offset = 0): Promise<PracticeRecords> {
  return api<PracticeRecords>(`/practice/records?limit=${limit}&offset=${offset}`);
}
export function addPracticeRecord(r: {
  tech: PracticeTech; mode?: "practice" | "real"; traded: boolean; ret?: number | null; entry_date?: string;
}): Promise<{ ok: true; summary: PracticeSummary }> {
  return api("/practice/records", { method: "POST", body: r });
}

// ── 종목(국내·해외) ────────────────────────────────────────
/** GET /search/suggest?q=&limit=8&kind=all|kr|us&prefer=kr|us — 자동완성(기록 안 함).
 *  kind=all: 국내·해외 함께(prefer 쪽 먼저, 다른 시장 3칸 보장). match: exact|prefix|code|typing|chosung|alias|contains */
export interface SuggestItem {
  type: "kr" | "us"; ticker: string; name: string; market: string; sector: string; match: string;
  price: number | null; change_rate: number | null; live: boolean;
}
export interface Suggest { query: string; items: SuggestItem[]; took_ms: number; }
export interface PopularStocks { items: { type: "kr" | "us"; ticker: string; name: string; cnt: number }[]; basis: "search" | "mixed" | "value"; days: number; }
export interface StockDetail {
  ticker: string; name: string; market: string; sector: string;
  price: number; change: number; change_rate: number; live: boolean;
  /** 시세 상태(live 장중 틱 · close 종가 등) · 그 가격이 속한 영업일 · 국내 장 단계 · 적재 출처(krx 실데이터 / gen 표본) */
  status?: string; session_date?: string | null; phase?: MarketPhase; source?: string;
  fundamentals: {
    open: number; high: number; low: number; volume: number; value: number; market_cap: number;
    /** 적자 PER·자본잠식 PBR·재무 없음은 null('-' 표시). */
    per: number | null; pbr: number | null; dividend_yield: number | null;
    /** 52주 = 최근 봉 기준 365일·마지막 연속 구간(실습용 과거 구간 제외) */
    high_52w: number; low_52w: number;
    prev_close?: number | null;
    /** dart = 실시세 × DART 재무 · sample = 표본(데모) 값 → '표본' 표시 · null = 재무 없음 */
    fundamentals_source?: "dart" | "sample" | null;
  };
}
export interface OhlcSeries { ticker: string; period: "1M" | "3M" | "6M" | "1Y"; series: Candle[]; overlays: Overlays; }
/** GET /overseas/list?sort=value|volume|marcap|rise|fall|price&limit= */
export interface OverseasStock {
  rank: number; symbol: string; ticker: string; name: string; name_ko: string | null; exchange: string; sector: string | null;
  currency: "USD"; price: number; change: number; change_rate: number; volume: number; value: number;
  market_cap: number | null; marcap: number | null; price_krw: number | null; live: boolean; status: string; session_date: string | null;
}
export interface OverseasList {
  sort: string; live: boolean; as_of: string | null; source: string; usd_krw: number | null;
  status: { live: boolean; sim: boolean; phase: MarketPhase; source: string; session_date: string | null };
  items: OverseasStock[];
}
/** GET /overseas/indices — S&P 500(SPX)·나스닥·다우존스·원/달러 */
export interface OverseasIndices {
  items: { code: "SPX" | "NASDAQ" | "DOW" | "USDKRW" | string; name: string; value: number; change: number; change_rate: number; ts: string | null }[];
  phase: MarketPhase; source: string;
}
/** GET /overseas/{symbol}/detail (없으면 404) */
export interface OverseasDetail {
  symbol: string; ticker: string; name: string; name_ko: string | null; exchange: string; sector: string | null; currency: "USD";
  price: number; change: number; change_rate: number; live: boolean; status: string; session_date: string | null;
  phase: MarketPhase; source: string; price_krw: number | null;
  fundamentals: {
    open: number; high: number; low: number; prev_close: number | null; volume: number; value: number;
    market_cap: number | null; market_cap_krw: number | null; per: number | null; pbr: number | null;
    high_52w: number | null; low_52w: number | null;
    revenue?: number | null; net_income?: number | null; assets?: number | null; equity?: number | null;
    fiscal_year?: number | null; period_end?: string | null;
  };
  financials_source: string | null; forex: { rate: number; source: string; ts: string | null };
}

// ── 투자 성향 분석(설문) ────────────────────────────────────
/** OUTOFSTOCK_투자성향설문.docx — 영역별 2문항 무작위 10문항, 보기 0~3점, 총점 0~30. */
export interface SurveyQuestion {
  id: string; no: number; domain: "A" | "B" | "C" | "D" | "E"; domain_name: string; q: string;
  options: { text: string; score: 0 | 1 | 2 | 3 }[];
}
export interface SurveyQuestions {
  questions: SurveyQuestion[]; count: number; max_score: number; per_domain: number;
  domains: { id: string; name: string; measure: string }[];
  grades: { type: string; grade: string; range: string; summary: string }[];
}
/** POST /survey/submit body — 권장 {answers:[{id,answer}]} / 호환 {answers:number[10]} */
export type SurveySubmit = { answers: { id: string; answer: number }[] } | { answers: number[] };
export interface SurveyResult {
  type: "안정형" | "중립형" | "공격형"; key: "stable" | "neutral" | "aggressive"; risk: string; grade: string;
  score: number; max_score: number; summary: string; description: string;
  breakdown: { domain: string; name: string; score: number; max: number }[] | null;
  products: Product[]; product_rule: string; notice: string;
}

// ── 마이페이지 · 관심종목 ──────────────────────────────────
/** 시세가 아직 없는 종목은 price·change_rate 가 null. 없는 종목코드 POST 는 404. */
export interface WatchItem { ticker: string; name: string; market: string; price: number | null; change_rate: number | null; live: boolean; }
export interface Watchlist { count: number; items: WatchItem[]; }
export type LogKind = "wallet" | "practice" | "learning" | "access" | "survey" | "watchlist";
export type WalletFilter = "all" | "reward" | "charge" | "transfer" | "invest" | "shop";   // shop = 양봉이 꾸미기
export interface LedgerRow {
  ledger_id: number; reason: string; reason_ko: string; amount: number; balance_after: number;
  ref: string; memo: string; created_at: string;
}
/** 마이페이지 — 각 기록은 최근 preview(5)건, 전체 건수는 counts. */
export interface MeOverview {
  user: string;
  profile: { username: string; created_at: string | null };
  survey: { type: string; score: number; description: string; created_at: string } | null;
  practice: { done: number; active: number; total: number;
              sessions: { session_id: number; scenario_id: string; name: string; difficulty?: string; status: string;
                          final_return: number | null; best_return: number | null; attempts: number }[] };
  learning: { count: number; items: { id: number; kind: string; item_key: string; item_name: string; viewed_at: string }[] };
  /** detail = 기기 계열(브라우저·OS)만 */
  access: { count: number; items: { id: number; event: string; detail: string; at: string }[] };
  watchlist: { count: number; items: WatchItem[] };
  wallet: Wallet;
  attendance: { streak: number; total_days: number; checked_in_today: boolean; daily_reward: number };
  invest: InvestPositions["summary"];
  /** 양봉이 꾸미기 요약(프로필 그림) — owned 는 개수 */
  avatar: { equipped: AvatarOutfit; worth: number; class: AvatarClass; owned: number };
  counts: Record<LogKind, number>; preview: number;
}
/** GET /me/logs/{kind}?limit=&offset=[&filter=] — '더보기' 전용 페이지(지갑만 filter) */
export interface LogPage<T = Record<string, unknown>> {
  kind: LogKind; title: string; total: number; limit: number; offset: number; has_more: boolean;
  items: T[]; filter?: WalletFilter; filters?: { key: WalletFilter; label: string }[];
}
export interface Badges { chat_unread: number; friend_requests: number; }

// ── 친구(요청 → 수락) · 채팅 ────────────────────────────────
export type Relation = "friend" | "sent" | "received" | "none" | "me";
export interface UserRow { username: string; created_at: string; relation: Relation; is_friend: boolean; }
export interface FriendRequests {
  received: { id: number; username: string; created_at: string }[];
  sent: { id: number; username: string; created_at: string }[];
}
export interface FriendAction { ok: boolean; friend: string; status: "pending" | "friends" | "declined" | "canceled" | "none"; }
export interface Friends { items: { username: string; since: string }[]; }
/** GET /friends/{username}/profile — 공개 부분집합만 */
export interface FriendProfile {
  username: string; created_at: string; survey_type: string | null; practice_done: number;
  invest: { open_count: number; pnl_pct: number }; relation: Relation; is_friend: boolean; is_me: boolean;
}
export interface ChatRooms {
  items: { peer: string; last_id: number; last_body: string; last_at: string; last_mine: boolean; unread: number; is_friend: boolean }[];
  unread_total: number;
}
export interface ChatMessage { id: number; sender: string; receiver: string; body: string; created_at: string; read: boolean; mine: boolean; }
/** GET /chat/{peer}/messages?after_id=&before_id=&limit= — body 는 화면에 넣을 때 항상 이스케이프 */
export interface ChatThread { peer: string; items: ChatMessage[]; peer_last_read_id: number; can_send: boolean; has_more: boolean; }

// ── 뉴스 홈(팀 news.html · news-api.js 계약) — GET /api/news/home → { status, message, data: NewsHome } ──
/** views 는 항상 null(포털·언론사 비공개) → coverage(보도량)를 쓰세요. */
export interface NewsArticle {
  id: string; title: string; summary: string; source: string; publishedAt: string; url: string;
  views: null; dateOnly: boolean; sentiment: "호재" | "악재" | null;
  stocks: { ticker: string; name: string }[];
  coverage?: { articles: number; outlets: number };
  related?: { id: string; title: string; source: string; publishedAt: string; url: string; dateOnly: boolean }[];
}
export interface RankedArticle extends NewsArticle { rank: number; rankChange: number; isNew: boolean; }
export interface NewsTheme {
  rank: number; name: string; summary: string; articleCount: number; changeRate: number | null;
  keywords: string[]; trend: number[]; stocks: { ticker: string; name: string; changeRate: number | null }[];
  sentiment: { pos: number; neg: number }; memberCount: number;
}
export interface NewsHome {
  updatedAt: string; todayLabel: string; todayTop: NewsArticle[]; themes: NewsTheme[];
  rankings: { week: RankedArticle[]; month: RankedArticle[]; weekLabel: string; monthLabel: string; basis: string };
  latest: NewsArticle[]; aiPicks: TodayPicks;
  metric: { views: null; basis: "coverage"; note: string }; sample: boolean;
}

// ── 양봉이 꾸미기(avatar.html) — 그림·이름·가격은 web/assets/avatar-items.js, 서버는 그 사본의 가격으로 차감 ──
export type AvatarSlot = "hat" | "top" | "bottom" | "shoes" | "glasses" | "pet";
/** 부위 → 아이템 id(예: { hat: "hat-2" }) — 안 입은 부위는 키가 없다 */
export type AvatarOutfit = Partial<Record<AvatarSlot, string>>;
export interface AvatarClass { name: "흙수저" | "동수저" | "은수저" | "금수저" | "다이아수저"; level: 1 | 2 | 3 | 4 | 5; }
export interface AvatarItem { id: string; slot: AvatarSlot; tier: 1 | 2 | 3 | 4 | 5; name: string; price: number; desc: string; }
export interface AvatarCatalog {
  slots: { id: AvatarSlot; name: string }[]; tiers: { tier: number; name: string; color: string; bg: string }[];
  classes: { min: number; name: string; color: string }[]; items: AvatarItem[];
}
export interface AvatarState { owned: string[]; equipped: AvatarOutfit; worth: number; class: AvatarClass; balance?: number; bought?: string; }
/** 서버가 저장 전에 가린 중요정보 종류(채팅·송금 응답) — rrn: 주민·외국인등록번호, card: 카드번호 */
export type MaskedKind = "rrn" | "card";
/** 503 의 code — overloaded: 혼잡(공개 조회부터 잠시 거절) · service_paused: 그 기능만 비상 차단 */
export type UnavailableCode = "overloaded" | "service_paused";
/** 기기 결속 키(DPoP) 관련 401 code — 2.8 */
export type DpopCode = "dpop_required" | "dpop_mismatch" | "dpop_invalid" | "dpop_replay";
export function avatarCatalog(): Promise<AvatarCatalog> { return api<AvatarCatalog>("/avatar/catalog"); }
export function avatarMe(): Promise<AvatarState> { return api<AvatarState>("/avatar/me"); }
/** price = 화면이 사용자에게 보여 준 가격(확인용) — 서버 가격과 다르면 409(아무것도 안 바뀜). 이미 가짐·잔액 부족은 400. */
export function avatarBuy(item_id: string, price: number, equip = true): Promise<AvatarState> {
  return api<AvatarState>("/avatar/buy", { method: "POST", body: { item_id, price, equip } });
}
/** 바꿀 부위만 — null 은 벗기. 하나라도 틀리면 전부 그대로(400). */
export function avatarEquip(items: Partial<Record<AvatarSlot, string | null>>): Promise<AvatarState> {
  return api<AvatarState>("/avatar/equip", { method: "POST", body: { items } });
}

// ── 가상머니 지갑(로그인 필요) ────────────────────────────────
export interface Wallet { username: string; balance: number; ledger: LedgerRow[]; }
export interface Attendance {
  today: string; checked_in_today: boolean; streak: number; total_days: number; daily_reward: number;
  recent: { attend_date: string; reward: number; streak: number }[];
  /** POST /attendance/check-in 응답에만 */
  claimed?: boolean; reward_given?: number; balance?: number;
}
export interface TransferResult {
  ok: true; amount: number; from: string; to: string; balance: number; memo: string;
  /** 같은 Idempotency-Key 로 다시 보낸 요청 → true(송금은 처음 1번만) */
  duplicate: boolean;
}
export interface InvestPosition {
  position_id: number; ticker: string; name: string; market: string; quantity: number;
  buy_price: number; buy_date: string; cost: number; current_price: number | null; value: number;
  pnl: number; pnl_pct: number; realized_pnl: number; sold_qty: number; days_held: number;
  pending_adjustment: boolean; live: boolean;
}
export interface InvestPositions {
  summary: { open_count: number; closed_count: number; cost: number; value: number; pnl: number; pnl_pct: number; realized_pnl: number };
  open: InvestPosition[];
  /** 청산 내역 — quantity = 판 수량 합, sell_price = 평균 매도가, realized_pct = 실현손익 ÷ 매도분 원가(%) */
  closed: { position_id: number; ticker: string; name: string; quantity: number; buy_price: number; buy_date: string;
            sell_price: number; sell_date: string; realized_pnl: number; realized_pct: number }[];
}
export interface InvestSellResult {
  ok: true; position_id: number; sold: number; sell_price: number; proceeds: number;
  realized_pnl: number; closed: boolean; balance: number;
}
export interface IapProduct { product_id: string; name: string; coins: number; price: string; }
export interface TossConfig { client_key: string; mode: string; enabled: boolean; }
