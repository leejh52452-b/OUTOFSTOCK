"""KRX OpenAPI 클라이언트 — 응답 파싱/코드 정규화(네트워크 없이 검증).
필드명·콤마·ISIN 단축코드 추출 로직이 실서버 응답과 맞물리는 부분을 고정한다."""
from app.collectors import krx


def test_num_parsing():
    assert krx._num("71,000") == 71000.0
    assert krx._num("-") == 0.0
    assert krx._num("") == 0.0
    assert krx._num("1.05") == 1.05


def test_short_code():
    assert krx._short("005930") == "005930"          # 단축코드 그대로
    assert krx._short("KR7005930003") == "005930"     # 표준코드(ISIN) → 6자리


def test_daily_stocks_maps_fields(monkeypatch):
    fake = {krx.SVC_STK: [{
        "BAS_DD": "20200527", "ISU_CD": "005930", "ISU_NM": "삼성전자", "MKT_NM": "KOSPI",
        "TDD_OPNPRC": "50,000", "TDD_HGPRC": "51,000", "TDD_LWPRC": "49,500",
        "TDD_CLSPRC": "50,500", "ACC_TRDVOL": "12,345,678", "ACC_TRDVAL": "620,000,000,000",
        "MKTCAP": "300,000,000,000,000", "LIST_SHRS": "5,969,782,550", "FLUC_RT": "1.01"}],
        krx.SVC_KSQ: []}
    monkeypatch.setattr(krx, "_get", lambda svc, d: fake.get(svc, []))
    out = krx.daily_stocks("2020-05-27")
    assert "005930" in out
    r = out["005930"]
    assert r["open"] == 50000.0 and r["close"] == 50500.0 and r["high"] == 51000.0
    assert r["volume"] == 12345678 and r["shares"] == 5969782550
    assert r["change_rate"] == 1.01 and r["market"] == "KOSPI"


def test_index_day_picks_main(monkeypatch):
    fake = {krx.SVC_IDX_KOSPI: [
        {"IDX_NM": "코스피 200", "CLSPRC_IDX": "400.00", "CMPPREVDD_IDX": "1.0", "FLUC_RT": "0.25"},
        {"IDX_NM": "코스피", "CLSPRC_IDX": "3,124.68", "CMPPREVDD_IDX": "18.42", "FLUC_RT": "0.59"}],
        krx.SVC_IDX_KOSDAQ: [
        {"IDX_NM": "코스닥", "CLSPRC_IDX": "842.17", "CMPPREVDD_IDX": "-4.31", "FLUC_RT": "-0.51"}]}
    monkeypatch.setattr(krx, "_get", lambda svc, d: fake.get(svc, []))
    rows = krx.index_day("2026-09-11")
    by = {r["code"]: r for r in rows}
    assert by["KOSPI"]["value"] == 3124.68 and by["KOSPI"]["change_rate"] == 0.59
    assert by["KOSDAQ"]["value"] == 842.17 and by["KOSDAQ"]["change"] == -4.31


def test_transient_errors_are_retried_but_auth_errors_are_not(monkeypatch):
    # 연결 끊김·5xx 는 잠깐 뒤 다시(예전: 하루치 실패 → 그날 데이터 누락), 401/403(이용신청 미승인)은 즉시 실패
    import requests
    from app.collectors import krx
    from app.config import settings

    class R:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

        def json(self):
            return {"OutBlock_1": [{"ok": 1}]}
    seq = iter([requests.ConnectionError("reset"), R(503), R(200)])

    def fake_get(_url, **_kw):
        x = next(seq)
        if isinstance(x, Exception):
            raise x
        return x
    monkeypatch.setattr(settings, "KRX_API_KEY", "k")
    monkeypatch.setattr(krx.time, "sleep", lambda _s: None)
    monkeypatch.setattr(krx.requests, "get", fake_get)
    assert krx._get("svc", "2026-09-30") == [{"ok": 1}]
    calls = []
    monkeypatch.setattr(krx.requests, "get", lambda _u, **_k: calls.append(1) or R(401))
    try:
        krx._get("svc", "2026-09-30")
        raise AssertionError("401 이면 예외")
    except krx.KrxAuthError:
        pass
    assert len(calls) == 1
