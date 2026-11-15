"""성향 등급별 추천 상품 — 「OUTOFSTOCK_추천상품_선정.docx」 기준(확인일 2026-09-22).

  · 위험 라벨 : 금융위원회 「투자성 상품 위험등급 산정 가이드라인」(2023.1)의 공식 1~6등급(1=가장 위험)을
               두 등급씩 묶어 세 단계로 쓴다 — 저위험 5·6등급 / 중위험 3·4등급 / 고위험 1·2등급.
  · 공통 제외 : 레버리지·인버스·커버드콜 등 파생형 상품(주식 초보자를 위한 금융 상품이 아님).
  · 조합 규칙 : 안정형 = 저위험 2 + 중위험 1 · 중립형 = 중위험 2 + 저위험 1 · 공격형 = 중위험 1 + 고위험 2.
  · 등급 출처 : ETF 10개는 운용사가 금융감독원 전자공시시스템(DART)에 제출한 투자설명서,
               삼성전자·SK하이닉스는 증권사 분류 기준 1~2등급 수준. 등급은 운용사 결산 시 재산정될 수 있다.
가격은 우리 DB 에 시세가 있는 종목(개별 주식)만 붙인다 — 없는 값을 지어내지 않는다.
"""
from app.services import quotes

CHECKED_AT = "2026-09-22"
GUIDELINE = {"title": "금융위원회 「투자성 상품 위험등급 산정 가이드라인」 (2023.1)",
             "ref": "fsc.go.kr/po010101/79327"}

RISK_LABELS = [
    {"label": "저위험", "grades": "5·6등급", "assets": "초단기 금리, 국공채, 우량 채권, 채권 비중이 큰 자산배분",
     "meaning": "원금이 거의 흔들리지 않고 금리만큼 벌 수 있음"},
    {"label": "중위험", "grades": "3·4등급", "assets": "주식·금·은과 채권을 섞은 혼합형",
     "meaning": "조금 흔들리지만 주식만큼은 아님"},
    {"label": "고위험", "grades": "1·2등급", "assets": "주식형 ETF, 개별 주식",
     "meaning": "시장만큼 또는 그 이상 크게 오르내림"},
]
EXCLUDED = {"keywords": ["레버리지", "인버스", "커버드콜"],
            "note": "레버리지·인버스·커버드콜 등 파생형 상품은 제외해요. 주식 초보자를 위한 금융 상품이 아니에요."}

COMBO = {"안정형": [("저위험", 2), ("중위험", 1)],
         "중립형": [("중위험", 2), ("저위험", 1)],
         "공격형": [("중위험", 1), ("고위험", 2)]}

# 위험 단계별 상품 12개 — (코드, 상품, 운용사, 공식등급, 라벨, 종류, 한 줄 설명, 투자설명서 출처 | None)
_P = [
    ("453010", "PLUS KOFR금리", "한화자산운용", "6", "저위험", "초단기 금리(레포)",
     "나라 채권을 담보로 하루짜리 이자를 받는 상품이에요. 거의 움직이지 않아요.",
     ("「한화 PLUS KOFR금리특별자산상장지수투자신탁(금리)」 투자설명서", "2026.01.28", "20260213000014")),
    ("114260", "KODEX 국고채3년", "삼성자산운용", "5", "저위험", "국고채",
     "나라에 3년 동안 돈을 빌려주는 채권에 투자해요. 금리와 채권 가격의 관계를 배우기 좋아요.",
     ("「삼성KODEX 국고채3년 증권상장지수투자신탁[채권]」 투자설명서", "2026.07.31", "20260813000086")),
    ("273130", "KODEX 종합채권(AA-이상)액티브", "삼성자산운용", "5", "저위험", "국채+우량 회사채",
     "나라와 튼튼한 회사의 채권을 골고루 담아요.",
     ("「삼성KODEX종합채권(AA-이상)액티브증권상장지수투자신탁[채권]」 투자설명서", "2026.07.31", "20260813000375")),
    ("329650", "KODEX TRF3070", "삼성자산운용", "5", "저위험", "세계 주식 3 : 국내 채권 7",
     "세계 주식 30%와 채권 70%를 섞었어요. 주식이 들어 있어도 채권이 많으면 덜 흔들려요.",
     ("「삼성KODEX TRF 3070 증권상장지수투자신탁[채권혼합]」 투자설명서", "2026.07.31", "20260813000081")),
    ("0138Y0", "PLUS 금채권혼합", "한화자산운용", "4", "중위험", "금 5 : 국고채 5",
     "금과 나라 채권을 반씩 담아요. 주식이 아닌 자산도 오르내린다는 걸 배워요.",
     ("「한화 PLUS 금채권혼합증권상장지수투자신탁(채권혼합-재간접형)」 투자설명서", "2026.09.03", "20260916000018")),
    ("0184N0", "PLUS 은채권혼합", "한화자산운용", "4", "중위험", "은 5 : 국고채 5",
     "은과 나라 채권을 반씩 담아요.",
     ("「한화 PLUS 은채권혼합증권상장지수투자신탁(채권혼합-재간접형)」 투자설명서", "2026.09.03", "20260916000034")),
    ("284430", "KODEX 200미국채혼합50", "삼성자산운용", "4", "중위험", "코스피200 + 미국 국채",
     "우리나라 대표 200개 회사 주식과 미국 국채를 섞어 담아요.",
     ("「삼성 KODEX200 미국채혼합50증권상장지수투자신탁[채권혼합-파생형]」 투자설명서", "2026.06.19", "20260630000020")),
    ("448330", "KODEX 삼성전자채권혼합", "삼성자산운용", "4", "중위험", "삼성전자 + 국고채",
     "삼성전자와 나라 채권을 섞었어요. 같은 회사라도 채권과 섞으면 덜 흔들려요.",
     ("「삼성KODEX 삼성전자채권혼합증권상장지수투자신탁[채권혼합]」 투자설명서", "2026.07.09", "20260722000125")),
    ("381170", "TIGER 미국테크TOP10 INDXX", "미래에셋자산운용", "2", "고위험", "미국 빅테크 10종목",
     "애플·엔비디아 같은 미국 기술 회사 10곳에 집중해요. 환율에 따라서도 움직여요.",
     ("「미래에셋TIGER미국테크TOP10INDXX증권상장지수투자신탁(주식)」 투자설명서", "2026.05.21", "20260528000058")),
    ("161510", "PLUS 고배당주", "한화자산운용", "2", "고위험", "국내 배당주 30종목",
     "배당을 많이 주는 우리나라 회사 30곳에 담아요. 배당이 무엇인지 배워요.",
     ("「한화 PLUS 고배당주증권상장지수투자신탁(주식)」 투자설명서", "2026.01.16", "20260203000007")),
    ("005930", "삼성전자", None, "1~2", "고위험", "국내 대형주",
     "우리나라 대표 회사 한 곳의 주식이에요. ETF와 달리 한 회사에만 투자해요.", None),
    ("000660", "SK하이닉스", None, "1~2", "고위험", "국내 반도체 대형주",
     "반도체 회사 한 곳의 주식이에요. 오르내림이 커요.", None),
]


def _product(code, name, manager, grade, risk, kind, desc, src):
    p = {"code": code, "name": name, "manager": manager or "— (개별 주식)", "grade": grade, "risk": risk,
         "kind": kind, "type": "주식" if manager is None else "ETF", "desc": desc}
    if src:
        title, written, rcp = src
        p["source"] = {"text": f"{manager}, {title}, 작성기준일 {written}, "
                               f"금융감독원 전자공시시스템 접수번호 {rcp}, 확인일 {CHECKED_AT.replace('-', '.')}",
                       "dart_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"}
    else:
        p["source"] = {"text": "증권사 분류 기준으로 1~2등급 수준", "dart_url": None}
    return p


PRODUCTS = [_product(*row) for row in _P]
BY_CODE = {p["code"]: p for p in PRODUCTS}

# 문서 3절 '성향별 추천 예시' 순서 그대로
PICKS = {"안정형": ["453010", "329650", "0138Y0"],          # PLUS KOFR금리 · KODEX TRF3070 · PLUS 금채권혼합
         "중립형": ["284430", "0184N0", "273130"],          # KODEX 200미국채혼합50 · PLUS 은채권혼합 · KODEX 종합채권
         "공격형": ["448330", "381170", "000660"]}          # KODEX 삼성전자채권혼합 · TIGER 미국테크TOP10 · SK하이닉스

NOTICE = ("상품 위험 라벨은 금융위원회 「투자성 상품 위험등급 산정 가이드라인」(2023.1)의 6단계 위험등급을 "
          "저위험(5·6등급)·중위험(3·4등급)·고위험(1·2등급)으로 묶어 OUTOFSTOCK 팀이 교육 목적으로 구성한 것이다. "
          "개별 ETF의 투자위험등급은 각 운용사(삼성자산운용, 한화자산운용, 미래에셋자산운용)가 금융감독원 "
          "전자공시시스템에 제출한 투자설명서에서 인용하였으며, 해당 문서의 저작권은 각 운용사에 있다. "
          "KODEX, PLUS, TIGER는 각 운용사의 등록 상표이다. 확인일 2026년 9월 22일. "
          "위험등급은 운용사 결산 시 재산정되어 변경될 수 있다. 본 서비스는 교육용이며 투자 권유가 아니다.")


def rule_text(profile):
    return " + ".join(f"{label} {n}" for label, n in COMBO[profile])


# 설문 결과의 key(stable/neutral/aggressive)로 불러도 같은 성향으로(예전엔 조용히 안정형으로 바뀜)
_ALIASES = {"stable": "안정형", "neutral": "중립형", "aggressive": "공격형"}


def _normalize(profile):
    p = (profile or "").strip()
    p = _ALIASES.get(p.lower(), p)
    return p if p in PICKS else "안정형"


def _with_quotes(items):
    """DB 에 시세가 있는 코드(개별 주식)만 현재가·등락률을 붙인다. ETF 는 None(지어내지 않음)."""
    qmap = quotes.kr_quotes([p["code"] for p in items])
    out = []
    for p in items:
        q = qmap.get(p["code"])
        out.append({**p, "price": q["price"] if q else None,
                    "change_rate": q["change_rate"] if q else None,
                    "status": q["status"] if q else None})
    return out


def for_profile(profile="안정형", with_price=True):
    """성향별 추천 3개(조합 규칙 준수) + 규칙·출처 문구."""
    profile = _normalize(profile)
    items = [BY_CODE[c] for c in PICKS[profile]]
    return {"profile": profile, "grade": {"안정형": "안정형 · 저위험", "중립형": "중립형 · 중위험",
                                           "공격형": "공격형 · 고위험"}[profile],
            "rule": rule_text(profile), "subtitle": f"{rule_text(profile)} 조합",
            "items": _with_quotes(items) if with_price else [dict(p) for p in items],
            "excluded": EXCLUDED["note"], "notice": NOTICE, "checked_at": CHECKED_AT}


def catalog():
    """위험 단계별 전체 12개 + 라벨 기준 + 조합 규칙 + 출처(문서 1·2·4·5절)."""
    groups = [{**lab, "items": _with_quotes([p for p in PRODUCTS if p["risk"] == lab["label"]])}
              for lab in RISK_LABELS]
    return {"labels": RISK_LABELS, "groups": groups,
            "combos": [{"profile": k, "rule": rule_text(k), "picks": PICKS[k]} for k in PICKS],
            "excluded": EXCLUDED, "guideline": GUIDELINE, "notice": NOTICE, "checked_at": CHECKED_AT}
