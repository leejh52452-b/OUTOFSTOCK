/* =====================================================================
 * OUTOFSTOCK 모의투자 차트
 * - practice.html 의 createChart()(캔들·격자·현재가·툴팁·크로스헤어)를 바탕으로
 *   이동평균선 4종, 볼린저밴드, 아래 패널(거래량·RSI·MACD)을 더한 버전
 * - 데이터를 불러오거나 시간을 계산하지 않고, 받은 상태(S)를 그리기만 함
 * ===================================================================== */
(function (global) {
    'use strict';

    // ---------- 포맷 / 색상 (practice.html 과 동일) ----------
    const num = (v) => Math.round(v).toLocaleString('ko-KR');
    const won = (v) => num(v) + '원';
    const pct = (v, d = 2) => (v > 0.00004 ? '+' : '') + (Math.abs(v) < 0.00004 ? '0.00' : (v * 100).toFixed(d)) + '%';
    const dirCls = (v) => (v > 0.00004 ? 'up' : v < -0.00004 ? 'down' : 'flat');

    const COLOR = {
        up: '#dc2626', down: '#2563eb', grid: '#e8edf3', axis: '#94a3b8', text: '#64748b',
        sma5: '#f59e0b', sma20: '#16a34a', sma60: '#4f46e5', sma120: '#94a3b8',
        bb: '#0891b2', rsi: '#7c3aed', macd: '#4f46e5', signal: '#f59e0b'
    };
    const FONT = "11px 'Pretendard', -apple-system, 'Malgun Gothic', sans-serif";
    const BOLD = "700 10.5px 'Pretendard', -apple-system, 'Malgun Gothic', sans-serif";

    // ---------- 지표 계산 ----------
    function niceStep(range, ticks) { const raw = range / ticks, mag = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / mag; return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag; }
    function calcSMA(c, n) { return c.map((_, i) => i < n - 1 ? null : c.slice(i - n + 1, i + 1).reduce((s, k) => s + k.c, 0) / n); }
    function calcRSI(c, n = 14) {
        const out = new Array(c.length).fill(null); let g = 0, l = 0;
        for (let i = 1; i < c.length; i++) {
            const d = c[i].c - c[i - 1].c, up = Math.max(d, 0), dn = Math.max(-d, 0);
            if (i <= n) { g += up; l += dn; if (i === n) { g /= n; l /= n; out[i] = 100 - 100 / (1 + (l === 0 ? 1e9 : g / l)); } }
            else { g = (g * (n - 1) + up) / n; l = (l * (n - 1) + dn) / n; out[i] = 100 - 100 / (1 + (l === 0 ? 1e9 : g / l)); }
        }
        return out;
    }
    function calcEMA(vals, n) {
        const out = new Array(vals.length).fill(null), k = 2 / (n + 1); let prev = null, seed = [];
        vals.forEach((v, i) => {
            if (v == null) return;
            if (prev == null) { seed.push(v); if (seed.length === n) { prev = seed.reduce((a, b) => a + b, 0) / n; out[i] = prev; } return; }
            prev = v * k + prev * (1 - k); out[i] = prev;
        });
        return out;
    }
    function calcBollinger(c, n = 20, m = 2) {
        const mid = calcSMA(c, n), up = [], lo = [];
        c.forEach((_, i) => {
            if (mid[i] == null) { up.push(null); lo.push(null); return; }
            const sl = c.slice(i - n + 1, i + 1), sd = Math.sqrt(sl.reduce((s, k) => s + (k.c - mid[i]) ** 2, 0) / n);
            up.push(mid[i] + m * sd); lo.push(mid[i] - m * sd);
        });
        return { mid, up, lo };
    }
    function calcMACD(c, f = 12, s = 26, sig = 9) {
        const closes = c.map(k => k.c), ef = calcEMA(closes, f), es = calcEMA(closes, s);
        const macd = closes.map((_, i) => ef[i] != null && es[i] != null ? ef[i] - es[i] : null);
        const signal = calcEMA(macd, sig);
        const hist = macd.map((v, i) => v != null && signal[i] != null ? v - signal[i] : null);
        return { macd, signal, hist };
    }
    function calcAll(c) {
        const bb = calcBollinger(c), m = calcMACD(c);
        return {
            sma5: calcSMA(c, 5), sma20: calcSMA(c, 20), sma60: calcSMA(c, 60), sma120: calcSMA(c, 120),
            bbUp: bb.up, bbMid: bb.mid, bbLo: bb.lo, rsi: calcRSI(c, 14),
            macd: m.macd, signal: m.signal, hist: m.hist
        };
    }

    const PANEL_H = 74, PANEL_GAP = 26;

    // ---------- 차트 ----------
    // S = { candles, ind, upTo, start, slots, labels[], dayMarks[{i,label}], overlays{sma5..bb}, panels[], showCurrent, avgPrice }
    function createChart(canvas, wrap, tip) {
        const ctx = canvas.getContext('2d');
        const ch = { S: null, geom: null, hover: -1, mouse: null };

        function chip(text, x, y, bg, fg, align) {
            ctx.font = BOLD;
            const w = ctx.measureText(text).width + 12, h = 17;
            const x0 = align === 'right' ? x - w : align === 'center' ? x - w / 2 : x;
            ctx.fillStyle = bg; ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x0, y - h / 2, w, h, 5) : ctx.rect(x0, y - h / 2, w, h); ctx.fill();
            ctx.fillStyle = fg; ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(text, x0 + 6, y + 0.5);
            ctx.font = FONT;
        }
        function line(arr, start, last, xOf, yOf, col, width, dash) {
            ctx.save(); ctx.strokeStyle = col; ctx.lineWidth = width || 1.6; ctx.lineJoin = 'round'; if (dash) ctx.setLineDash(dash);
            ctx.beginPath(); let st = false;
            for (let i = start; i <= last; i++) { const v = arr[i]; if (v == null) continue; const x = xOf(i), y = yOf(v); if (!st) { ctx.moveTo(x, y); st = true; } else ctx.lineTo(x, y); }
            ctx.stroke(); ctx.restore();
        }

        function render(S) {
            if (S) ch.S = S; S = ch.S; if (!S) return;
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const W = wrap.clientWidth, H = wrap.clientHeight; if (!W || !H) return;
            canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
            canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);

            const c = S.candles, ind = S.ind, last = S.upTo, start = S.start, slots = S.slots, ov = S.overlays || {};
            // 거래량은 켜져 있으면 항상 RSI·MACD 위(아래 패널 중 맨 위)
            const panels = ((S.panels || []).includes('volume') ? ['volume'] : []).concat((S.panels || []).filter(p => p !== 'volume'));
            const padL = 10, padR = 72, padT = 14, padB = 26;
            const plotW = W - padL - padR, totalH = H - padT - padB;
            const mainH = totalH - panels.length * (PANEL_H + PANEL_GAP);
            const sw = plotW / slots, xOf = (i) => padL + (i - start + 0.5) * sw;
            const bottom = padT + totalH;

            // 가격 범위
            let lo = Infinity, hi = -Infinity;
            const keys = ['sma5', 'sma20', 'sma60', 'sma120'].filter(k => ov[k]);
            if (ov.bb) keys.push('bbUp', 'bbLo');
            for (let i = start; i <= last; i++) {
                lo = Math.min(lo, c[i].l); hi = Math.max(hi, c[i].h);
                keys.forEach(k => { const v = ind[k][i]; if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
            }
            if (!isFinite(lo)) { lo = 0; hi = 1; }
            const padV = (hi - lo) * 0.08 || hi * 0.02; lo -= padV; hi += padV;
            const yOf = (p) => padT + (hi - p) / (hi - lo) * mainH;
            ctx.font = FONT; ctx.textBaseline = 'middle';

            // 가로 격자 + 가격 축
            const step = niceStep(hi - lo, 5);
            for (let p = Math.ceil(lo / step) * step; p <= hi; p += step) {
                const y = yOf(p);
                ctx.strokeStyle = COLOR.grid; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
                ctx.fillStyle = COLOR.axis; ctx.textAlign = 'left'; ctx.fillText(num(p), W - padR + 8, y);
            }
            // 요일 구분선 + 라벨 (시즌 구간)
            ctx.save(); ctx.setLineDash([3, 4]);
            (S.dayMarks || []).forEach((d, k) => {
                const x = padL + (d.i - start) * sw;
                if (x > padL + 1) { ctx.strokeStyle = '#d5dce6'; ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, bottom); ctx.stroke(); }
                const next = (S.dayMarks[k + 1] ? S.dayMarks[k + 1].i : d.i + d.n);
                const cx = padL + ((d.i + next) / 2 - start) * sw;
                ctx.fillStyle = d.muted ? '#b6c0cc' : COLOR.text; ctx.textAlign = 'center'; ctx.fillText(d.label, cx, H - 10);
            });
            ctx.restore();

            // 볼린저밴드
            if (ov.bb) {
                ctx.save(); ctx.fillStyle = 'rgba(8,145,178,0.07)'; ctx.beginPath(); let st = false;
                for (let i = start; i <= last; i++) { const v = ind.bbUp[i]; if (v == null) continue; const x = xOf(i); if (!st) { ctx.moveTo(x, yOf(v)); st = true; } else ctx.lineTo(x, yOf(v)); }
                for (let i = last; i >= start; i--) { const v = ind.bbLo[i]; if (v == null) continue; ctx.lineTo(xOf(i), yOf(v)); }
                ctx.closePath(); ctx.fill(); ctx.restore();
                line(ind.bbUp, start, last, xOf, yOf, COLOR.bb, 1.1);
                line(ind.bbLo, start, last, xOf, yOf, COLOR.bb, 1.1);
                line(ind.bbMid, start, last, xOf, yOf, COLOR.bb, 1, [4, 3]);
            }
            // 이동평균선
            ['sma120', 'sma60', 'sma20', 'sma5'].forEach(k => { if (ov[k]) line(ind[k], start, last, xOf, yOf, COLOR[k], 1.7); });

            // 캔들
            const bw = Math.max(1.6, Math.min(sw * 0.62, 12));
            for (let i = start; i <= last; i++) {
                const k = c[i], col = k.c >= k.o ? COLOR.up : COLOR.down, x = xOf(i);
                ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1;
                ctx.beginPath(); ctx.moveTo(x, yOf(k.h)); ctx.lineTo(x, yOf(k.l)); ctx.stroke();
                const yo = yOf(k.o), yc = yOf(k.c); ctx.fillRect(x - bw / 2, Math.min(yo, yc), bw, Math.max(1, Math.abs(yo - yc)));
            }
            // 평균단가선
            if (S.avgPrice) {
                const y = yOf(S.avgPrice);
                if (y > padT && y < padT + mainH) {
                    ctx.save(); ctx.strokeStyle = '#64748b'; ctx.setLineDash([5, 4]); ctx.lineWidth = 1.2; ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke(); ctx.restore();
                    chip('평균단가 ' + num(S.avgPrice), padL + 4, y - 11, '#64748b', '#fff', 'left');
                }
            }
            // 현재가선
            if (S.showCurrent && last >= 0) {
                const k = c[last], prev = last > 0 ? c[last - 1].c : k.o, y = yOf(k.c), col = k.c >= prev ? COLOR.up : COLOR.down;
                ctx.save(); ctx.strokeStyle = col; ctx.globalAlpha = 0.6; ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke(); ctx.restore();
                ctx.fillStyle = col; ctx.beginPath(); ctx.roundRect ? ctx.roundRect(W - padR + 2, y - 9, padR - 4, 18, 4) : ctx.rect(W - padR + 2, y - 9, padR - 4, 18); ctx.fill();
                ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.font = BOLD; ctx.fillText(num(k.c), W - padR + 2 + (padR - 4) / 2, y + 0.5); ctx.font = FONT;
            }
            // 아래 패널
            panels.forEach((p, n) => {
                const top = padT + mainH + PANEL_GAP + n * (PANEL_H + PANEL_GAP), h = PANEL_H;
                ctx.strokeStyle = COLOR.grid; ctx.lineWidth = 1; ctx.strokeRect(padL, top, plotW, h);
                ctx.textAlign = 'left'; ctx.fillStyle = COLOR.text; ctx.font = BOLD;
                const cur = last >= 0 ? last : 0;
                if (p === 'volume') {
                    let vmax = 0; for (let i = start; i <= last; i++) vmax = Math.max(vmax, c[i].v);
                    vmax = vmax * 1.1 || 1; const yV = (v) => top + h - v / vmax * h;
                    for (let i = start; i <= last; i++) {
                        const k = c[i]; ctx.fillStyle = k.c >= k.o ? 'rgba(220,38,38,0.45)' : 'rgba(37,99,235,0.45)';
                        ctx.fillRect(xOf(i) - bw / 2, yV(k.v), bw, top + h - yV(k.v));
                    }
                    ctx.font = FONT; ctx.fillStyle = COLOR.axis; ctx.fillText(Math.round(vmax / 10000) + '만주', W - padR + 8, top + 6);
                } else if (p === 'rsi') {
                    const yR = (v) => top + (100 - v) / 100 * h;
                    ctx.fillStyle = 'rgba(124,58,237,0.06)'; ctx.fillRect(padL, yR(70), plotW, yR(30) - yR(70));
                    ctx.save(); ctx.setLineDash([4, 4]); ctx.strokeStyle = COLOR.axis;
                    [30, 70].forEach(v => { ctx.beginPath(); ctx.moveTo(padL, yR(v)); ctx.lineTo(padL + plotW, yR(v)); ctx.stroke(); }); ctx.restore();
                    ctx.font = FONT; ctx.fillStyle = COLOR.axis; [30, 70].forEach(v => ctx.fillText(String(v), W - padR + 8, yR(v)));
                    line(ind.rsi, start, last, xOf, yR, COLOR.rsi, 1.7);
                    const v = ind.rsi[cur]; ctx.fillStyle = COLOR.text; ctx.font = BOLD;
                    ctx.fillText('RSI(14) ' + (v != null ? v.toFixed(1) : '-') + (v > 70 ? ' · 과열' : v < 30 ? ' · 침체' : ''), padL, top - 10);
                } else if (p === 'macd') {
                    let m = 0; for (let i = start; i <= last; i++) [ind.macd[i], ind.signal[i]].forEach(v => { if (v != null) m = Math.max(m, Math.abs(v)); });
                    m = m * 1.15 || 1; const yM = (v) => top + h / 2 - v / m * (h / 2);
                    ctx.save(); ctx.strokeStyle = COLOR.grid; ctx.beginPath(); ctx.moveTo(padL, yM(0)); ctx.lineTo(padL + plotW, yM(0)); ctx.stroke(); ctx.restore();
                    for (let i = start; i <= last; i++) {
                        const v = ind.hist[i]; if (v == null) continue;
                        ctx.fillStyle = v >= 0 ? 'rgba(220,38,38,0.4)' : 'rgba(37,99,235,0.4)';
                        ctx.fillRect(xOf(i) - bw / 2, Math.min(yM(0), yM(v)), bw, Math.abs(yM(v) - yM(0)));
                    }
                    line(ind.macd, start, last, xOf, yM, COLOR.macd, 1.6);
                    line(ind.signal, start, last, xOf, yM, COLOR.signal, 1.4);
                    ctx.font = FONT; ctx.fillStyle = COLOR.axis; ctx.fillText('0', W - padR + 8, yM(0));
                    const a = ind.macd[cur], b = ind.signal[cur]; ctx.fillStyle = COLOR.text; ctx.font = BOLD;
                    // 범례: 선 색 견본 + 이름·값 (MACD 선, 시그널 선)
                    let lx = padL; const ly = top - 10;
                    [['MACD(12,26,9) ', a, COLOR.macd], ['시그널(9) ', b, COLOR.signal]].forEach(([name, val, col], n) => {
                        if (n) { ctx.fillStyle = COLOR.axis; ctx.fillText('·', lx + 4, ly); lx += 16; }
                        ctx.strokeStyle = col; ctx.lineWidth = 2.5; ctx.beginPath(); ctx.moveTo(lx, ly); ctx.lineTo(lx + 14, ly); ctx.stroke();
                        lx += 20; ctx.fillStyle = COLOR.text;
                        const t = name + (val != null ? num(val) : '-'); ctx.fillText(t, lx, ly); lx += ctx.measureText(t).width;
                    });
                }
                ctx.font = FONT;
            });

            // 크로스헤어
            if (ch.hover >= start && ch.hover <= last) {
                const x = xOf(ch.hover);
                ctx.save(); ctx.strokeStyle = '#94a3b8'; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
            }
            ch.geom = { W, H, padL, padR, padT, start, last, sw, xOf, yOf, plotW, mainH };
            updateTip(S);
        }

        function updateTip(S) {
            const g = ch.geom;
            if (!tip || !g || ch.hover < g.start || ch.hover > g.last || !ch.mouse) { if (tip) tip.hidden = true; return; }
            const i = ch.hover, k = S.candles[i], ind = S.ind, ov = S.overlays || {};
            let html = `<b>${(S.labels && S.labels[i]) || ''}</b><br>시 ${num(k.o)} · 고 ${num(k.h)}<br>저 ${num(k.l)} · 종 ${num(k.c)}<br>거래량 ${num(k.v)}주`;
            const ma = [['sma5', '5일'], ['sma20', '20일'], ['sma60', '60일'], ['sma120', '120일']].filter(([key]) => ov[key] && ind[key][i] != null).map(([key, t]) => `${t} ${num(ind[key][i])}`);
            if (ma.length) html += '<br>' + ma.join(' · ');
            tip.innerHTML = html; tip.hidden = false;
            const tw = tip.offsetWidth, th = tip.offsetHeight;
            let left = ch.mouse.x + 14, top = ch.mouse.y + 14;
            if (left + tw > g.W - 4) left = ch.mouse.x - tw - 14; if (top + th > g.H - 4) top = ch.mouse.y - th - 14;
            tip.style.left = Math.max(4, left) + 'px'; tip.style.top = Math.max(4, top) + 'px';
        }

        canvas.addEventListener('mousemove', (e) => {
            const g = ch.geom; if (!g) return; const r = canvas.getBoundingClientRect();
            const x = e.clientX - r.left, y = e.clientY - r.top;
            const idx = Math.round((x - g.padL) / g.sw - 0.5 + g.start);
            ch.hover = (idx >= g.start && idx <= g.last) ? idx : -1; ch.mouse = { x, y };
            render();
        });
        canvas.addEventListener('mouseleave', () => { ch.hover = -1; ch.mouse = null; render(); });
        if (window.ResizeObserver) new ResizeObserver(() => render()).observe(wrap);
        ch.render = render;
        return ch;
    }

    global.InvestChart = { createChart, calcAll, num, won, pct, dirCls, COLOR, PANEL_H, PANEL_GAP };
})(window);
