-- V009 기능 정리 — 2026-10 팀 결정으로 화면에서 뺀 기능의 저장 구조를 걷어 낸다.
--   제거: 커뮤니티(종목 토론) · 시장 분석(테마·가상자산) · 60초 챌린지(게임) · 단기 랜덤 시뮬 · 아바타 ·
--         구 모의투자(simulator)·백테스트 세션 · 테마 추천 랭킹(theme_reco)
--
-- 왜 표까지 지우나(기록만 남기지 않고)
--   개인정보 보호법 제21조(파기) — 처리 목적을 달성한 개인정보는 지체 없이 파기한다. 기능이 사라지면 그 기능을 위해
--   모은 글·댓글·게임 기록·장착 정보는 보관할 이유가 없다. 표를 남겨 두면 접근 통제·백업·탈퇴 처리 대상만 늘어난다.
--   (최소 수집·최소 보관 — 공격 표면 축소)
-- 남기는 것
--   지갑 원장(wallet_ledger)의 과거 행(sim_stake·shop_buy 등 사유) — 금액 이력이라 잔액 재계산·원장 대사(daily close)의
--   근거다. 사유 이름(REASON_KO)은 코드에 그대로 둬 화면의 '전체' 목록에서 읽힌다.
--   analytics.security_master.ksic_seen_at — 업종 코드 수집일(공시·업종 적재가 계속 쓴다).

-- 테마(V008) — 함수 먼저(표를 참조), 그다음 표
DROP FUNCTION IF EXISTS analytics.theme_performance(text);
DROP TABLE IF EXISTS analytics.theme_label, analytics.theme_member, analytics.theme_evidence,
                     analytics.business_text, analytics.etf_holding, analytics.etf_master, analytics.theme CASCADE;
-- 가상자산 시세(V002) — 시장 분석 화면 전용이었다
DROP TABLE IF EXISTS analytics.crypto_quote;

-- 서비스(public) 표 — create_all 로 만들어졌던 것들. 자식 표부터.
DROP TABLE IF EXISTS public.community_report, public.community_like, public.community_comment, public.community_post,
                     public.game_event, public.game_trade, public.game_result, public.game_session,
                     public.sim_round_trade, public.sim_round, public.avatar_owned, public.avatar,
                     public.sim_trade, public.sim_holding, public.idempotency, public.sim_session,
                     public.backtest_trade, public.backtest_session, public.theme_reco CASCADE;
