// ============================================================
//  양봉이 꾸미기 — 아이템 그림·이름·가격(화면 공용: avatar.html · mypage.html · index.html)
//
//  ▶ 그림은 전부 SVG(코드로 그림) — 팀 마스코트 화풍(남색 굵은 윤곽 #37355a · 크림색 촛대 몸통 · 촛농 · 불꽃)에 맞췄다.
//  ▶ 좌표계: viewBox 0 0 420 440 — 몸통 가운데 x=200 · 머리 꼭대기 y=110 · 눈 y=226 · 옷 y≈282~350 · 바지 y≈342~389
//            · 발 y≈393 · 펫은 오른쪽 아래(x 300~410). 모자를 쓰면 불꽃이 모자 위로 올라간다(아이템의 lift).
//  ▶ 가격·이름을 바꾸면 서버 사본도 다시 만든다: python tools/build_avatar_items.py  (서버가 이 값으로 돈을 뺀다)
//  ▶ 등급 5단계 × 10배: 서민 1만 · 보통 10만 · 고급 100만 · 명품 1,000만 · 전설 1억 (가상머니)
// ============================================================
const AVATAR_SLOTS = [
    { id: 'hat', name: '모자' }, { id: 'top', name: '옷' }, { id: 'bottom', name: '바지' },
    { id: 'shoes', name: '신발' }, { id: 'glasses', name: '안경' }, { id: 'pet', name: '펫' }
];
const AVATAR_TIERS = [
    { tier: 1, name: '서민', color: '#475569', bg: '#f1f5f9' },     // 글자색: 배경 위 대비 4.5:1 이상(웹 접근성)
    { tier: 2, name: '보통', color: '#15803d', bg: '#dcfce7' },
    { tier: 3, name: '고급', color: '#1d4ed8', bg: '#dbeafe' },
    { tier: 4, name: '명품', color: '#7e22ce', bg: '#f3e8ff' },
    { tier: 5, name: '전설', color: '#854d0e', bg: '#fef3c7' }
];
// 꾸민 양봉이 몸값(입은 아이템 가격 합) → 수저 등급
const AVATAR_CLASSES = [
    { min: 0, name: '흙수저', color: '#78716c' },
    { min: 300000, name: '동수저', color: '#b45309' },
    { min: 3000000, name: '은수저', color: '#475569' },
    { min: 30000000, name: '금수저', color: '#a16207' },
    { min: 300000000, name: '다이아수저', color: '#0e7490' }
];

(function () {
    const O = '#37355a';                                   // 윤곽선(마스코트와 같은 남색)
    const S = (w) => `stroke="${O}" stroke-width="${w}" stroke-linejoin="round" stroke-linecap="round"`;
    // 반짝이(4갈래 별) — 전설 등급 장식. class="av-sparkle" 는 화면 CSS 가 반짝이게 한다(없으면 그대로)
    const star = (x, y, r) => `<path class="av-sparkle" d="M${x} ${y - r}Q${x} ${y} ${x + r} ${y}Q${x} ${y} ${x} ${y + r}Q${x} ${y} ${x - r} ${y}Q${x} ${y} ${x} ${y - r}Z" fill="#fffbe6" stroke="#e2a93b" stroke-width="1.6"/>`;
    const gem = (x, y, r, c) => `<circle cx="${x}" cy="${y}" r="${r}" fill="${c}" ${S(2.5)}/><circle cx="${x - r * 0.35}" cy="${y - r * 0.35}" r="${r * 0.32}" fill="#fff" opacity=".85"/>`;
    const mirror = (svg) => `<g transform="translate(400 0) scale(-1 1)">${svg}</g>`;
    const both = (svg) => svg + mirror(svg);              // 왼쪽을 그리면 오른쪽은 거울로(x=200 기준)
    const GOLD = 'url(#@gold)';

    // ── 소매(왼쪽 기준, both 로 양쪽) — 짧은 소매 / 긴 소매 ─────────────
    const sleeveShort = (fill, extra = '') => both(`<path d="M114 286C99 287 88 297 86 311L106 320L114 314Z" fill="${fill}" ${S(5)}/>${extra}`);
    const sleeveLong = (fill, cuff, extra = '') => both(`<path d="M114 286C98 287 86 299 85 313C84 323 88 329 95 330L114 321Z" fill="${fill}" ${S(5)}/>`
        + `<path d="M85 313C84 323 88 329 95 330L99 318Z" fill="${cuff}" ${S(4)}/>${extra}`);
    // 바지 기본 틀(몸통 아래 곡선을 따라감) + 가운데 다리 사이
    const pantsShape = (fill) => `<path d="M109 342C109 372 134 389 168 389L232 389C266 389 291 372 291 342Z" fill="${fill}" ${S(5)}/>`;
    const legGap = '<path d="M191 389Q200 368 209 389Z" fill="#fde8cb" stroke="#37355a" stroke-width="4" stroke-linejoin="round"/>';
    // 신발 기본 틀(왼발 기준) — both 로 양발
    const shoe = (fill, sole, extra = '') => both(`<path d="M137 399C137 381 150 371 166 371C182 371 195 381 195 399Z" fill="${fill}" ${S(5)}/>`
        + `${extra}<rect x="134" y="395" width="64" height="15" rx="7.5" fill="${sole}" ${S(5)}/>`);
    // 셔츠 V 자 깃(정장·턱시도·카디건 공용)
    const torsoV = (fill, vBottom) => `<path d="M109 290C126 283 148 282 164 286L200 ${vBottom}L236 286C252 282 274 283 291 290L291 350L109 350Z" fill="${fill}" ${S(5)}/>`;

    const ITEMS = [
        // ════ 모자 ════════════════════════════════════════════════
        { id: 'hat-1', slot: 'hat', tier: 1, name: '신문지 고깔모자', price: 10000, lift: 46,
          desc: '경제신문을 접어 만든 모자. 파란 하락 차트가 살짝 비쳐요.',
          art: `<path d="M124 150L200 64L276 150Z" fill="#f3efe2" ${S(5)}/>`
            + `<path d="M186 100H214M172 114H228M160 128H240" stroke="#a7a3b8" stroke-width="4" stroke-linecap="round"/>`
            + `<path d="M112 148H288L283 170H117Z" fill="#e6dfcb" ${S(5)}/>`
            + `<polyline points="138,163 154,156 168,160 184,153 200,165 216,159" fill="none" stroke="#3b6fd8" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>`
            + `<path d="M246 156h22v10h-22z" fill="#cfc6ad" stroke="#a7a3b8" stroke-width="2"/>` },
        { id: 'hat-2', slot: 'hat', tier: 2, name: '빨간 야구모자', price: 100000, lift: 22,
          desc: '상승을 부르는 빨간 캡. 가운데 위쪽 화살표가 포인트예요.',
          art: `<path d="M122 158C122 112 156 92 200 92C244 92 278 112 278 158Z" fill="#e04848" ${S(5)}/>`
            + `<path d="M200 96V150M160 102C152 122 150 140 152 156M240 102C248 122 250 140 248 156" fill="none" stroke="#b33434" stroke-width="3" stroke-linecap="round"/>`
            + `<path d="M140 124C146 110 160 101 174 98" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round" opacity=".45"/>`
            + `<circle cx="200" cy="128" r="14" fill="#fff" ${S(3)}/>`
            + `<path d="M200 137V120M193 127L200 120L207 127" fill="none" stroke="#e04848" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>`
            + `<path d="M110 155Q200 198 290 155Q200 168 110 155Z" fill="#c63a3a" ${S(5)}/>`
            + `<circle cx="200" cy="92" r="6" fill="#b33434" ${S(3)}/>` },
        { id: 'hat-3', slot: 'hat', tier: 3, name: '와인색 베레모', price: 1000000, lift: 18,
          desc: '살짝 기울여 쓴 와인색 베레모. 미술관 가는 양봉이.',
          art: `<path d="M102 148C94 118 140 94 198 92C258 90 304 110 296 140C292 156 252 160 200 160C150 160 106 162 102 148Z" fill="#9b2f4c" ${S(5)}/>`
            + `<path d="M126 146Q200 170 274 146L274 160Q200 184 126 160Z" fill="#76223a" ${S(5)}/>`
            + `<path d="M128 118C148 104 178 99 200 99" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round" opacity=".35"/>`
            + `<path d="M246 106C262 110 276 118 282 128" fill="none" stroke="#76223a" stroke-width="3" stroke-linecap="round"/>` },
        { id: 'hat-4', slot: 'hat', tier: 4, name: '실크 톱햇', price: 10000000, lift: 74,
          desc: '광택 나는 검정 실크 톱햇. 보라 띠와 금 버클이 포인트예요.',
          art: `<ellipse cx="200" cy="150" rx="102" ry="15" fill="#2a2840" ${S(5)}/>`
            + `<path d="M146 150L150 52C150 46 156 42 162 42H238C244 42 250 46 250 52L254 150Z" fill="#2a2840" ${S(5)}/>`
            + `<ellipse cx="200" cy="45" rx="49" ry="7" fill="#3c3a58" ${S(4)}/>`
            + `<path d="M148 118H252L253 142H147Z" fill="#6b3fa6" ${S(4)}/>`
            + `<rect x="190" y="119" width="20" height="22" rx="3" fill="none" stroke="#f2c14e" stroke-width="4"/>`
            + `<path d="M166 58L164 110" stroke="#fff" stroke-width="6" stroke-linecap="round" opacity=".16"/>`
            + `<path d="M98 150A102 15 0 0 0 302 150Z" fill="#2a2840" ${S(5)}/>` },
        { id: 'hat-5', slot: 'hat', tier: 5, name: '다이아 왕관', price: 100000000, lift: 62,
          desc: '루비·사파이어·다이아가 박힌 황금 왕관. 양봉이 왕국의 주인.',
          art: `<path d="M134 128L127 78L160 106L200 58L240 106L273 78L266 128Z" fill="${GOLD}" ${S(5)}/>`
            + `<path d="M132 150L268 150L266 124L134 124Z" fill="${GOLD}" ${S(5)}/>`
            + `<path d="M188 96L200 82L212 96L200 114Z" fill="#e6f7ff" ${S(3)}/><path d="M188 96H212M200 82V114" stroke="#9fd3f0" stroke-width="1.5"/>`
            + gem(127, 76, 7, '#fff6d5') + gem(200, 56, 8, '#fff6d5') + gem(273, 76, 7, '#fff6d5')
            + gem(200, 137, 8, '#e0405e') + gem(166, 137, 6, '#3f7be0') + gem(234, 137, 6, '#3f7be0')
            + gem(147, 137, 4, '#e8f6ff') + gem(253, 137, 4, '#e8f6ff')
            + `<path d="M144 120L150 92" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".5"/>`
            + star(104, 70, 9) + star(298, 96, 8) + star(236, 44, 6) },

        // ════ 옷 ══════════════════════════════════════════════════
        { id: 'top-1', slot: 'top', tier: 1, name: '구멍 난 러닝셔츠', price: 10000,
          desc: '오래 입어 늘어난 흰 러닝. 구멍과 덧댄 천이 정겨워요.',
          art: `<path d="M109 290L140 283C156 304 244 304 260 283L291 290L291 350L109 350Z" fill="#f7f6f0" ${S(5)}/>`
            + `<path d="M144 288C160 306 240 306 256 288" fill="none" stroke="#dcd8c8" stroke-width="3" stroke-linecap="round"/>`
            + `<path d="M146 324l7-6 6 5-3 8-8 1z" fill="#fde8cb" ${S(3)}/>`
            + `<path d="M244 332l6-4 5 4-2 6-7 0z" fill="#fde8cb" ${S(3)}/>`
            + `<rect x="210" y="309" width="24" height="19" rx="2" fill="#a9c6e8" ${S(3)}/>`
            + `<rect x="214" y="313" width="16" height="11" fill="none" stroke="#5b7fb5" stroke-width="1.6" stroke-dasharray="3 2"/>`
            + `<ellipse cx="178" cy="336" rx="8" ry="4" fill="#e9dcae" opacity=".9"/>` },
        { id: 'top-2', slot: 'top', tier: 2, name: '줄무늬 티셔츠', price: 100000,
          desc: '파랑·하양 줄무늬 반팔 티. 깔끔한 주말 차림.',
          art: sleeveShort('#3b5bb5')
            + `<path d="M109 288C138 281 158 282 171 289C185 300 215 300 229 289C242 282 262 281 291 288L291 350L109 350Z" fill="#fff" ${S(5)}/>`
            + `<path d="M112 304H288V313H112ZM112 324H288V333H112ZM112 342H288V348H112Z" fill="#3b5bb5"/>`
            + `<path d="M109 288C138 281 158 282 171 289C185 300 215 300 229 289C242 282 262 281 291 288L291 350L109 350Z" fill="none" ${S(5)}/>`
            + `<path d="M173 292C186 302 214 302 227 292" fill="none" stroke="#3b5bb5" stroke-width="4" stroke-linecap="round"/>` },
        { id: 'top-3', slot: 'top', tier: 3, name: '초록 니트 카디건', price: 1000000,
          desc: '셔츠 위에 걸친 포근한 초록 카디건. 단추까지 단정해요.',
          art: sleeveLong('#3c7d5a', '#2f6347')
            + `<path d="M164 286L200 326L236 286Z" fill="#fff" ${S(3)}/>`
            + `<path d="M166 286L184 284L191 301ZM234 286L216 284L209 301Z" fill="#fff" ${S(3)}/>`
            + torsoV('#3c7d5a', 326)
            + `<path d="M124 298V346M140 294V346M156 292V346M244 292V346M260 294V346M276 298V346" stroke="#2f6347" stroke-width="2.4" stroke-linecap="round" opacity=".7"/>`
            + `<circle cx="200" cy="334" r="4.2" fill="#c9a26b" ${S(2.2)}/><circle cx="200" cy="345" r="4.2" fill="#c9a26b" ${S(2.2)}/>` },
        { id: 'top-4', slot: 'top', tier: 4, name: '증권맨 정장', price: 10000000,
          desc: '네이비 정장에 빨간 넥타이, 금색 행커치프. 여의도 출근룩.',
          art: sleeveLong('#263a6b', '#ffffff', `<circle cx="91" cy="323" r="2.6" fill="#f2c14e"/>`)
            + `<path d="M164 286L200 332L236 286Z" fill="#fff" ${S(3)}/>`
            + `<path d="M194 289H206L204 299H196Z" fill="#c8323c" ${S(2.8)}/><path d="M196 299H204L210 324L200 336L190 324Z" fill="#c8323c" ${S(2.8)}/>`
            + torsoV('#263a6b', 332)
            + `<path d="M166 288L190 318L178 324ZM234 288L210 318L222 324Z" fill="#1c2b52" ${S(3)}/>`
            + `<path d="M128 308H152L148 300L141 306L134 300Z" fill="#f2c14e" ${S(2.5)}/>`
            + `<circle cx="200" cy="342" r="4" fill="#1c2b52" ${S(2)}/>` },
        { id: 'top-5', slot: 'top', tier: 5, name: '황금 턱시도', price: 100000000,
          desc: '온몸이 반짝이는 황금 턱시도. 다이아 브로치와 나비넥타이까지.',
          art: sleeveLong(GOLD, '#1f1d33')
            + `<path d="M164 286L200 334L236 286Z" fill="#fff" ${S(3)}/>`
            + `<path d="M190 302Q200 310 210 302M188 314Q200 322 212 314" fill="none" stroke="#d9d6e8" stroke-width="2.5" stroke-linecap="round"/>`
            + torsoV(GOLD, 334)
            + `<path d="M166 288L192 320L180 330ZM234 288L208 320L220 330Z" fill="#1f1d33" ${S(3)}/>`
            + `<path d="M182 284L198 292L182 300ZM218 284L202 292L218 300Z" fill="#1f1d33" ${S(3)}/><circle cx="200" cy="292" r="4.5" fill="#1f1d33" ${S(2.5)}/>`
            + `<path d="M141 304L148 312L141 322L134 312Z" fill="#e6f7ff" ${S(2.5)}/>`
            + `<path d="M124 300V346M276 300V346" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".35"/>`
            + `<circle cx="200" cy="341" r="4" fill="#1f1d33" ${S(2)}/>`
            + star(112, 276, 8) + star(286, 330, 7) + star(250, 298, 5) },

        // ════ 바지 ════════════════════════════════════════════════
        { id: 'bottom-1', slot: 'bottom', tier: 1, name: '꽃무늬 고무줄 바지', price: 10000,
          desc: '할머니 댁에서 빌려 입은 꽃무늬 고무줄 바지. 무릎엔 천을 덧댔어요.',
          art: pantsShape('#8e6bbf')
            + `<path d="M118 349H282" stroke="#6f4fa0" stroke-width="5"/><path d="M126 343V354M142 343V354M158 343V354M174 343V354M190 343V354M206 343V354M222 343V354M238 343V354M254 343V354M270 343V354" stroke="#6f4fa0" stroke-width="2"/>`
            + [[140, 366], [178, 378], [226, 364], [262, 376], [200, 360]].map(([x, y]) => `<g fill="#fde4f0"><circle cx="${x - 4}" cy="${y}" r="3.4"/><circle cx="${x + 4}" cy="${y}" r="3.4"/><circle cx="${x}" cy="${y - 4}" r="3.4"/><circle cx="${x}" cy="${y + 4}" r="3.4"/></g><circle cx="${x}" cy="${y}" r="2.2" fill="#f2c14e"/>`).join('')
            + `<rect x="236" y="368" width="20" height="15" rx="2" fill="#b98b5a" ${S(2.6)}/>`
            + legGap },
        { id: 'bottom-2', slot: 'bottom', tier: 2, name: '청바지', price: 100000,
          desc: '어디에나 어울리는 파란 청바지. 주황 스티치가 살아 있어요.',
          art: pantsShape('#4a6fb5')
            + `<path d="M112 352H288" stroke="#38599a" stroke-width="5"/>`
            + `<path d="M130 356C134 368 146 372 158 370M270 356C266 368 254 372 242 370M200 356V376" fill="none" stroke="#f0a24b" stroke-width="2.2" stroke-dasharray="4 3" stroke-linecap="round"/>`
            + `<circle cx="200" cy="350" r="4.2" fill="#d79b4a" ${S(2.2)}/>`
            + `<path d="M150 345V356M250 345V356" stroke="#38599a" stroke-width="3" stroke-linecap="round"/>`
            + legGap },
        { id: 'bottom-3', slot: 'bottom', tier: 3, name: '베이지 면바지', price: 1000000,
          desc: '주름 잡힌 베이지 면바지에 갈색 가죽 벨트.',
          art: pantsShape('#d9c19c')
            + `<path d="M110 344H290V354H110Z" fill="#7a4b2a" ${S(3.5)}/>`
            + `<rect x="191" y="343" width="18" height="12" rx="2" fill="#d9dde6" ${S(2.5)}/>`
            + `<path d="M158 358V384M242 358V384" stroke="#c4a87f" stroke-width="2.5" stroke-linecap="round"/>`
            + legGap },
        { id: 'bottom-4', slot: 'bottom', tier: 4, name: '정장 바지', price: 10000000,
          desc: '칼주름 네이비 정장 바지. 금장 버클 가죽 벨트.',
          art: pantsShape('#263a6b')
            + `<path d="M110 344H290V354H110Z" fill="#1f1d33" ${S(3.5)}/>`
            + `<rect x="189" y="342" width="22" height="14" rx="2.5" fill="none" stroke="#f2c14e" stroke-width="4"/>`
            + `<path d="M158 358V385M242 358V385" stroke="#3d5590" stroke-width="2.5" stroke-linecap="round"/>`
            + legGap },
        { id: 'bottom-5', slot: 'bottom', tier: 5, name: '황금 스팽글 바지', price: 100000000,
          desc: '한 걸음마다 반짝이는 황금 스팽글 바지와 다이아 버클.',
          art: pantsShape(GOLD)
            + [[130, 362], [150, 374], [172, 364], [190, 380], [214, 368], [236, 378], [256, 362], [272, 374], [160, 384], [244, 386]].map(([x, y]) => `<circle cx="${x}" cy="${y}" r="3.2" fill="#fff6c9" opacity=".95"/>`).join('')
            + `<path d="M110 344H290V354H110Z" fill="#1f1d33" ${S(3.5)}/>`
            + `<path d="M190 349L200 340L210 349L200 360Z" fill="#e6f7ff" ${S(2.5)}/><path d="M190 349H210" stroke="#9fd3f0" stroke-width="1.5"/>`
            + legGap + star(118, 372, 7) + star(284, 360, 6) },

        // ════ 신발 ════════════════════════════════════════════════
        { id: 'shoes-1', slot: 'shoes', tier: 1, name: '파란 고무 슬리퍼', price: 10000,
          desc: '동네 마실용 파란 고무 슬리퍼. 발가락이 시원해요.',
          art: both(`<ellipse cx="166" cy="407" rx="31" ry="6.5" fill="#5b8fd6" ${S(4)}/>`
            + `<path d="M141 391C150 384 182 384 191 391L189 400C178 395 154 395 143 400Z" fill="#5b8fd6" ${S(4)}/>`) },
        { id: 'shoes-2', slot: 'shoes', tier: 2, name: '하얀 운동화', price: 100000,
          desc: '빨간 줄이 들어간 하얀 운동화. 출석 체크하러 뛰어가요.',
          art: shoe('#fbfbfd', '#e3e6ee', `<path d="M141 392C151 388 163 392 172 386C179 382 186 384 191 388" fill="none" stroke="#e04848" stroke-width="4.5" stroke-linecap="round"/>`
            + `<path d="M159 377H173M158 382H174" stroke="${O}" stroke-width="2.4" stroke-linecap="round"/>`)
            + both(`<path d="M138 403H194" stroke="#a3a9bb" stroke-width="2" stroke-dasharray="4 3"/>`) },
        { id: 'shoes-3', slot: 'shoes', tier: 3, name: '갈색 워커', price: 1000000,
          desc: '스웨이드 느낌의 갈색 워커. 어디든 든든하게.',
          art: shoe('#9a6a3f', '#4a2f1b', `<path d="M140 397C142 388 150 384 158 384L166 397Z" fill="#7a5130" stroke="${O}" stroke-width="2.5" stroke-linejoin="round"/>`
            + `<path d="M162 377L172 381M162 383L172 387" stroke="#e8d3b5" stroke-width="2.4" stroke-linecap="round"/><circle cx="174" cy="381" r="2.2" fill="#f2c14e"/><circle cx="174" cy="387" r="2.2" fill="#f2c14e"/>`
            + `<path d="M176 377C183 380 188 386 190 392" fill="none" stroke="#c99a6a" stroke-width="3" stroke-linecap="round"/>`) },
        { id: 'shoes-4', slot: 'shoes', tier: 4, name: '광택 구두', price: 10000000,
          desc: '거울처럼 반짝이는 검정 가죽 구두.',
          art: shoe('#22203a', '#0e0d1b', `<path d="M143 395C145 386 154 382 166 382C178 382 187 386 189 395" fill="none" stroke="#6d6a90" stroke-width="2" stroke-dasharray="3 3"/>`
            + `<path d="M148 380C155 376 165 375 172 376" fill="none" stroke="#fff" stroke-width="3.5" stroke-linecap="round" opacity=".75"/><path d="M181 389L187 393" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".5"/>`) },
        { id: 'shoes-5', slot: 'shoes', tier: 5, name: '다이아 황금 구두', price: 100000000,
          desc: '앞코에 다이아가 박힌 황금 구두. 걸을 때마다 반짝.',
          art: shoe(GOLD, '#5a3a14', `<path d="M160 386L166 378L172 386L166 394Z" fill="#e6f7ff" ${S(2.2)}/><path d="M160 386H172" stroke="#9fd3f0" stroke-width="1.4"/>`
            + `<path d="M146 382C151 377 158 375 162 375" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".7"/>`)
            + star(126, 396, 6) + star(276, 392, 6) },

        // ════ 안경 ════════════════════════════════════════════════
        { id: 'glasses-1', slot: 'glasses', tier: 1, name: '테이프 붙인 뿔테', price: 10000,
          desc: '부러진 곳을 테이프로 감은 두꺼운 뿔테. 렌즈엔 금도 갔어요.',
          art: `<path d="M138 218L112 214M262 218L288 214" ${S(5)}/>`
            + `<rect x="138" y="206" width="56" height="42" rx="13" fill="#fff" fill-opacity=".14" stroke="#24223a" stroke-width="7"/>`
            + `<rect x="206" y="206" width="56" height="42" rx="13" fill="#fff" fill-opacity=".14" stroke="#24223a" stroke-width="7"/>`
            + `<path d="M194 222H206" stroke="#24223a" stroke-width="7"/>`
            + `<rect x="191" y="214" width="18" height="16" rx="2" fill="#f5f4ee" ${S(2.5)}/><path d="M195 218V226M200 218V226M205 218V226" stroke="#c9c5b4" stroke-width="1.5"/>`
            + `<polyline points="248,212 240,224 249,230 241,243" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>` },
        { id: 'glasses-2', slot: 'glasses', tier: 2, name: '동그란 안경', price: 100000,
          desc: '공부하는 양봉이의 동그란 안경. 재무제표가 잘 보여요.',
          art: `<path d="M141 220L112 214M259 220L288 214" ${S(5)}/>`
            + `<circle cx="166" cy="226" r="25" fill="#fff" fill-opacity=".16" ${S(5)}/><circle cx="234" cy="226" r="25" fill="#fff" fill-opacity=".16" ${S(5)}/>`
            + `<path d="M191 223Q200 214 209 223" fill="none" ${S(5)}/>`
            + `<path d="M150 214Q156 207 164 206M218 214Q224 207 232 206" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".8"/>` },
        { id: 'glasses-3', slot: 'glasses', tier: 3, name: '검정 선글라스', price: 1000000,
          desc: '눈빛을 숨긴 검정 선글라스. 차트도 시크하게.',
          art: `<path d="M134 214L112 210M266 214L288 210" ${S(5)}/>`
            + both(`<path d="M134 210H196C198 236 190 252 166 252C142 252 132 236 134 210Z" fill="#1f1d33" ${S(5)}/><path d="M145 220H162M145 229H152" stroke="#fff" stroke-width="3.5" stroke-linecap="round" opacity=".55"/>`)
            + `<path d="M196 214H204" ${S(6)}/>` },
        { id: 'glasses-4', slot: 'glasses', tier: 4, name: '금테 외알 안경', price: 10000000,
          desc: '오른쪽 눈에 끼우는 금테 외알 안경과 금줄. 신사의 품격.',
          art: `<path d="M240 251C248 276 268 286 276 306" fill="none" stroke="${O}" stroke-width="7" stroke-linecap="round"/>`
            + `<path d="M240 251C248 276 268 286 276 306" fill="none" stroke="#f2c14e" stroke-width="3.6" stroke-dasharray="1 6" stroke-linecap="round"/>`
            + `<circle cx="234" cy="226" r="27" fill="#e8f4ff" fill-opacity=".22" stroke="${O}" stroke-width="10"/>`
            + `<circle cx="234" cy="226" r="27" fill="none" stroke="#f2c14e" stroke-width="5"/>`
            + `<path d="M218 212Q226 204 236 203" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".85"/>`
            + `<circle cx="276" cy="308" r="5" fill="#f2c14e" ${S(2.4)}/>` },
        { id: 'glasses-5', slot: 'glasses', tier: 5, name: '다이아 하트 선글라스', price: 100000000,
          desc: '다이아가 박힌 황금 테의 하트 선글라스. 시선 집중.',
          art: `<path d="M140 214L112 208M260 214L288 208" ${S(5)}/>`
            + both(`<path d="M166 248C149 237 134 226 137 211C140 199 157 197 166 210C175 197 192 199 195 211C198 226 183 237 166 248Z" fill="#ff6f9c" fill-opacity=".86" stroke="${O}" stroke-width="9"/>`
                + `<path d="M166 248C149 237 134 226 137 211C140 199 157 197 166 210C175 197 192 199 195 211C198 226 183 237 166 248Z" fill="none" stroke="#f2c14e" stroke-width="4.5"/>`
                + `<circle cx="146" cy="203" r="2.6" fill="#fff"/><circle cx="158" cy="201" r="2.2" fill="#fff"/><circle cx="174" cy="201" r="2.2" fill="#fff"/><circle cx="186" cy="203" r="2.6" fill="#fff"/>`
                + `<path d="M148 218Q152 212 158 212" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".8"/>`)
            + `<path d="M195 214Q200 208 205 214" fill="none" stroke="#f2c14e" stroke-width="5" stroke-linecap="round"/>`
            + star(124, 196, 8) + star(282, 238, 7) },

        // ════ 펫 ══════════════════════════════════════════════════
        { id: 'pet-1', slot: 'pet', tier: 1, name: '개미', price: 10000,
          desc: '동전 하나를 꼭 쥔 개미 투자자. 티끌 모아 태산!',
          art: `<ellipse cx="356" cy="416" rx="40" ry="6" fill="#e5e3ef"/>`
            + `<path d="M344 392L330 410M352 396L350 414M366 394L378 410" ${S(4)}/>`
            + `<ellipse cx="376" cy="388" rx="19" ry="15" fill="#5b3b2e" ${S(4.5)}/>`
            + `<ellipse cx="352" cy="384" rx="13" ry="15" fill="#6b4636" ${S(4.5)}/>`
            + `<circle cx="346" cy="352" r="19" fill="#6b4636" ${S(4.5)}/>`
            + `<path d="M338 336C334 322 326 318 320 320M354 334C358 320 366 316 372 318" fill="none" ${S(3.5)}/><circle cx="320" cy="320" r="4" fill="${O}"/><circle cx="372" cy="318" r="4" fill="${O}"/>`
            + `<circle cx="339" cy="350" r="6" fill="#fff"/><circle cx="355" cy="350" r="6" fill="#fff"/><circle cx="340" cy="351" r="3" fill="${O}"/><circle cx="356" cy="351" r="3" fill="${O}"/>`
            + `<path d="M342 361Q347 365 352 361" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/>`
            + `<circle cx="330" cy="384" r="10" fill="#f2c14e" ${S(3.5)}/><path d="M326 384H334M330 380V388" stroke="#a16207" stroke-width="2" stroke-linecap="round"/>` },
        { id: 'pet-2', slot: 'pet', tier: 2, name: '주린이 병아리', price: 100000,
          desc: '막 껍질을 깨고 나온 병아리. 주식 공부 1일차.',
          art: `<ellipse cx="356" cy="416" rx="38" ry="6" fill="#e5e3ef"/>`
            + `<path d="M346 404V414M366 404V414M340 414H352M360 414H372" stroke="#f08a24" stroke-width="4" stroke-linecap="round"/>`
            + `<circle cx="356" cy="378" r="30" fill="#ffd84d" ${S(4.5)}/>`
            + `<path d="M374 382C388 378 392 392 380 398" fill="#f7c22a" ${S(3.5)}/>`
            + `<path d="M330 360L336 344L344 356L352 342L360 356L368 344L374 358C366 352 340 352 330 360Z" fill="#fffdf6" ${S(3.5)}/>`
            + `<circle cx="346" cy="374" r="4.5" fill="${O}"/><circle cx="364" cy="374" r="4.5" fill="${O}"/><circle cx="347.5" cy="372.5" r="1.6" fill="#fff"/><circle cx="365.5" cy="372.5" r="1.6" fill="#fff"/>`
            + `<path d="M350 382L356 389L362 382Z" fill="#f08a24" ${S(2.5)}/>`
            + `<ellipse cx="340" cy="384" rx="5" ry="3" fill="#f7a6a0" opacity=".85"/><ellipse cx="372" cy="384" rx="5" ry="3" fill="#f7a6a0" opacity=".85"/>` },
        { id: 'pet-3', slot: 'pet', tier: 3, name: '장기투자 거북이', price: 1000000,
          desc: '느려도 꾸준한 거북이. 장기투자의 상징이에요.',
          art: `<ellipse cx="360" cy="417" rx="46" ry="6" fill="#e5e3ef"/>`
            + `<ellipse cx="328" cy="388" rx="15" ry="13" fill="#a8d5a2" ${S(4.5)}/>`
            + `<circle cx="324" cy="385" r="3.4" fill="${O}"/><path d="M318 394Q323 397 328 394" fill="none" stroke="${O}" stroke-width="2.5" stroke-linecap="round"/>`
            + `<path d="M340 404V413M380 404V413" stroke="${O}" stroke-width="13" stroke-linecap="round"/><path d="M340 404V413M380 404V413" stroke="#a8d5a2" stroke-width="7" stroke-linecap="round"/>`
            + `<path d="M330 406C330 372 344 358 362 358C380 358 394 372 394 406Z" fill="#4f9d69" ${S(4.5)}/>`
            + `<path d="M352 370L362 364L372 370L372 382L362 388L352 382ZM338 392L346 384L352 392L346 400ZM372 392L378 384L386 392L378 400Z" fill="#6cbb85" stroke="#3b7d52" stroke-width="2.4" stroke-linejoin="round"/>`
            + `<path d="M326 406H398" ${S(4.5)}/><path d="M396 404Q404 404 404 398" fill="none" ${S(3.5)}/>` },
        { id: 'pet-4', slot: 'pet', tier: 4, name: '아기 황소', price: 10000000,
          desc: '상승장의 상징, 아기 황소. 금 코뚜레가 반짝여요.',
          art: `<ellipse cx="362" cy="417" rx="44" ry="6" fill="#e5e3ef"/>`
            + `<path d="M344 402V414M358 404V414M374 404V414M388 400V412" stroke="#7a3f22" stroke-width="8" stroke-linecap="round"/>`
            + `<path d="M396 384Q408 380 406 392" fill="none" ${S(3.5)}/>`
            + `<ellipse cx="368" cy="390" rx="32" ry="20" fill="#b5643c" ${S(4.5)}/>`
            + `<path d="M318 344C308 340 304 330 310 324C314 334 322 336 328 338M366 344C376 340 380 330 374 324C370 334 362 336 356 338" fill="#f3e2b8" ${S(3.5)}/>`
            + `<ellipse cx="342" cy="360" rx="25" ry="22" fill="#c06f45" ${S(4.5)}/>`
            + `<ellipse cx="342" cy="374" rx="17" ry="11" fill="#eab396" ${S(3.5)}/><circle cx="336" cy="374" r="2.6" fill="${O}"/><circle cx="348" cy="374" r="2.6" fill="${O}"/>`
            + `<circle cx="342" cy="384" r="5" fill="none" stroke="#f2c14e" stroke-width="3"/>`
            + `<circle cx="333" cy="356" r="4" fill="${O}"/><circle cx="351" cy="356" r="4" fill="${O}"/><circle cx="334.3" cy="354.6" r="1.4" fill="#fff"/><circle cx="352.3" cy="354.6" r="1.4" fill="#fff"/>`
            + `<path d="M320 352L314 348M364 352L370 348" ${S(3.5)}/>` },
        { id: 'pet-5', slot: 'pet', tier: 5, name: '황금 유니콘', price: 100000000,
          desc: '기업가치 1조의 꿈, 황금 뿔 유니콘. 무지개 갈기가 휘날려요.',
          art: `<ellipse cx="362" cy="417" rx="46" ry="6" fill="#fde68a" opacity=".7"/>`
            + `<path d="M346 402V414M360 404V414M376 404V414M390 400V412" stroke="#fff3df" stroke-width="9" stroke-linecap="round"/>`
            + `<path d="M346 410V414M360 410V414M376 410V414M390 408V412" stroke="#f2c14e" stroke-width="9" stroke-linecap="round"/>`
            + `<path d="M398 380C412 376 414 392 404 398" fill="none" stroke="#c084fc" stroke-width="6" stroke-linecap="round"/>`
            + `<ellipse cx="370" cy="390" rx="32" ry="20" fill="#fffaf0" ${S(4.5)}/>`
            + `<ellipse cx="344" cy="360" rx="22" ry="21" fill="#fffaf0" ${S(4.5)}/>`
            + `<path d="M340 340L352 304L356 344Z" fill="${GOLD}" ${S(3.5)}/><path d="M345 330L354 326M347 320L354 316" stroke="#a16207" stroke-width="2"/>`
            + `<path d="M360 346C372 350 376 362 372 374M364 344C380 344 386 358 384 372" fill="none" stroke="#f472b6" stroke-width="6" stroke-linecap="round"/>`
            + `<path d="M358 352C366 360 368 370 364 380" fill="none" stroke="#60a5fa" stroke-width="5" stroke-linecap="round"/>`
            + `<circle cx="336" cy="358" r="4.2" fill="${O}"/><circle cx="337.4" cy="356.6" r="1.5" fill="#fff"/>`
            + `<ellipse cx="330" cy="370" rx="5" ry="3" fill="#f7a6a0" opacity=".85"/>`
            + `<path d="M326 376Q331 379 336 376" fill="none" stroke="${O}" stroke-width="2.4" stroke-linecap="round"/>`
            + star(318, 324, 8) + star(404, 352, 7) + star(380, 318, 5) }
    ];

    // ── 기본 양봉이 ───────────────────────────────────────────
    const BODY = 'M112 172C112 130 150 110 200 110C250 110 288 130 288 172L288 334C288 366 264 386 232 386L168 386C136 386 112 366 112 334Z';
    const DRIP = 'M288 194C288 214 266 216 264 200C262 186 250 182 238 188C226 194 214 180 200 180C184 180 174 194 160 190C148 186 140 192 138 204C136 222 112 222 112 198';
    const defs = (u) => `<defs>`
        + `<linearGradient id="${u}body" x1="0" x2="1"><stop offset="0" stop-color="#fff3e2"/><stop offset=".5" stop-color="#fde8cb"/><stop offset="1" stop-color="#f2cfae"/></linearGradient>`
        + `<linearGradient id="${u}cap" x1="0" x2="1"><stop offset="0" stop-color="#fffaf0"/><stop offset="1" stop-color="#fbe6c9"/></linearGradient>`
        + `<linearGradient id="${u}flame" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe9a6"/><stop offset="1" stop-color="#ffb347"/></linearGradient>`
        + `<linearGradient id="${u}gold" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff2a8"/><stop offset=".45" stop-color="#f6c744"/><stop offset="1" stop-color="#d4921c"/></linearGradient>`
        + `</defs>`;
    const base = {
        under: `<path d="M118 290C100 288 82 300 82 316C82 330 98 336 112 328L118 320Z" fill="#fbe2c6" ${S(6)}/>`
            + `<path d="M282 290C300 288 318 300 318 316C318 330 302 336 288 328L282 320Z" fill="#f4d4b4" ${S(6)}/>`
            + `<ellipse cx="166" cy="393" rx="27" ry="16" fill="#f8dcc0" ${S(6)}/><ellipse cx="234" cy="393" rx="27" ry="16" fill="#f3d2b2" ${S(6)}/>`,
        body: `<path d="${BODY}" fill="url(#@body)" ${S(6.5)}/>`
            + `<path d="M112 172C112 130 150 110 200 110C250 110 288 130 288 172L288 194C288 214 266 216 264 200C262 186 250 182 238 188C226 194 214 180 200 180C184 180 174 194 160 190C148 186 140 192 138 204C136 222 112 222 112 198Z" fill="url(#@cap)"/>`
            + `<path d="${DRIP}" fill="none" ${S(5.5)}/>`
            + `<path d="M152 140Q200 156 248 140" fill="none" stroke="#e6c19c" stroke-width="4" stroke-linecap="round"/>`
            + `<path d="M132 156Q136 132 160 124" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" opacity=".75"/>`
            + `<path d="M${BODY.slice(1)}" fill="none" ${S(6.5)}/>`,
        face: `<ellipse cx="166" cy="226" rx="18" ry="20" fill="${O}"/><ellipse cx="234" cy="226" rx="18" ry="20" fill="${O}"/>`
            + `<circle cx="160" cy="218" r="7" fill="#fff"/><circle cx="173" cy="232" r="3.2" fill="#fff"/><circle cx="228" cy="218" r="7" fill="#fff"/><circle cx="241" cy="232" r="3.2" fill="#fff"/>`
            + `<ellipse cx="138" cy="256" rx="15" ry="8.5" fill="#f0a59b" opacity=".9"/><ellipse cx="262" cy="256" rx="15" ry="8.5" fill="#f0a59b" opacity=".9"/>`
            + `<path d="M186 250C188 266 212 266 214 250Z" fill="${O}" ${S(4)}/><path d="M192 259C196 265 204 265 208 259C204 256 196 256 192 259Z" fill="#f28b8b"/>`
    };
    const flame = (lift) => `<path d="M200 110V${86 - lift}" ${S(6)}/>`;
    const fire = (lift) => `<g transform="translate(0 ${-lift})"><path d="M200 22C216 38 226 54 222 70C219 84 210 90 200 90C190 90 181 84 178 70C174 54 184 38 200 22Z" fill="url(#@flame)" ${S(5)}/>`
        + `<path d="M200 48C208 58 212 68 208 76C205 82 195 82 192 76C188 68 192 58 200 48Z" fill="#fff4c8"/></g>`;

    const byId = new Map(ITEMS.map((it) => [it.id, it]));
    let seq = 0;
    const uid = () => `av${(++seq).toString(36)}`;
    const art = (it, u) => it.art.replace(/@/g, u);

    /** 꾸민 양봉이 SVG. eq = { hat, top, bottom, shoes, glasses, pet } (아이템 id 또는 비움) */
    function avatarSVG(eq, opts) {
        eq = eq || {}; opts = opts || {};
        const u = uid() + '-';
        const pick = (slot) => { const it = byId.get(eq[slot]); return it && it.slot === slot ? it : null; };
        const hat = pick('hat'), lift = hat ? hat.lift || 0 : 0;
        const layer = (slot) => { const it = pick(slot); return it ? art(it, u) : ''; };
        const top = Math.min(0, 22 - lift - 14);
        const label = opts.label || '꾸민 양봉이';
        return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 ${top} 420 ${440 - top}" preserveAspectRatio="xMidYMax meet" role="img" aria-label="${label.replace(/"/g, '')}">`
            + defs(u)
            + `<ellipse cx="200" cy="410" rx="120" ry="11" fill="#e5e3ef"/>`
            + base.under + layer('shoes') + base.body.replace(/@/g, u) + layer('bottom') + layer('top')
            + base.face + layer('glasses') + flame(lift) + layer('hat') + fire(lift).replace(/@/g, u) + layer('pet')
            + `</svg>`;
    }
    // 아이템만(상점 목록 그림) — 부위별로 잘라 보여 준다
    const CROP = { hat: '88 30 244 150', top: '74 270 252 88', bottom: '100 334 200 62', shoes: '126 368 148 48',
                   glasses: '104 190 192 70', pet: '296 296 120 128' };
    const CROP_ITEM = { 'hat-4': '88 26 244 140', 'glasses-4': '160 190 140 124', 'pet-5': '296 290 124 134', 'hat-5': '88 32 244 130' };
    function avatarItemSVG(id) {
        const it = byId.get(id); if (!it) return '';
        const u = uid() + '-';
        return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${CROP_ITEM[id] || CROP[it.slot]}" role="img" aria-label="${it.name}">${defs(u)}${art(it, u)}</svg>`;
    }
    // <img> 에 바로 넣는 주소(마이페이지·홈 로그인 카드의 프로필 그림) — 화면 CSP img-src 'self' data: 안에서
    const dataUrl = (eq, opts) => 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(avatarSVG(eq, opts));
    const worth = (eq) => Object.values(eq || {}).reduce((s, id) => s + ((byId.get(id) || {}).price || 0), 0);
    const classOf = (w) => AVATAR_CLASSES.reduce((c, k, i) => (w >= k.min ? Object.assign({ level: i + 1 }, k) : c), null);
    /** 가격 짧게 — 10000 → '1만원' · 111110000 → '1억 1,111만원' · 12345 → '1만 2,345원' */
    function fmt(n) {
        n = Math.max(0, Math.round(Number(n) || 0));
        const eok = Math.floor(n / 1e8), man = Math.floor((n % 1e8) / 1e4), rest = n % 1e4, out = [];
        if (eok) out.push(eok.toLocaleString('ko-KR') + '억');
        if (man) out.push(man.toLocaleString('ko-KR') + '만');
        if (rest || !out.length) out.push(rest.toLocaleString('ko-KR'));
        return out.join(' ') + '원';
    }

    window.AVATAR_ITEMS = ITEMS;
    window.AVATAR = Object.freeze({ items: ITEMS, byId: (id) => byId.get(id) || null, svg: avatarSVG, itemSvg: avatarItemSVG,
                                    dataUrl, worth, classOf, fmt, slots: AVATAR_SLOTS, tiers: AVATAR_TIERS, classes: AVATAR_CLASSES });
})();
