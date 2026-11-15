"""보조지표 계산 — SMA(단순이동평균) · RSI(상대강도). 실습·종목상세 차트 공용.
입력은 종가 리스트(시간순). 계산 불가한 앞부분은 None 으로 채워 길이를 맞춘다."""


def sma(closes, window):
    out = []
    for i in range(len(closes)):
        if i + 1 < window:
            out.append(None)
        else:
            seg = closes[i + 1 - window:i + 1]
            out.append(round(sum(seg) / window, 2))
    return out


def rsi(closes, window=14):
    """Wilder RSI. 앞 window 개는 None."""
    out = [None] * len(closes)
    if len(closes) <= window:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    # 최초 평균(단순), 이후 Wilder 평활
    avg_g = sum(gains[:window]) / window
    avg_l = sum(losses[:window]) / window
    for i in range(window, len(closes)):
        if i > window:
            avg_g = (avg_g * (window - 1) + gains[i - 1]) / window
            avg_l = (avg_l * (window - 1) + losses[i - 1]) / window
        if avg_l > 0:
            out[i] = round(100 - 100 / (1 + avg_g / avg_l), 2)
        else:                                   # 하락이 없으면 100 — 단 오르지도 않은(가격 변동 없음, 거래정지) 구간은 중립 50
            out[i] = 100.0 if avg_g > 0 else 50.0
    return out
