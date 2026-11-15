"""한글 검색 유틸 — 초성 · 자모 분해 · 정규화 (종목 검색/자동완성용).

  chosung("삼성전자")  → "ㅅㅅㅈㅈ"          초성 검색(ㅅㅅㅈㅈ)
  jamo("삼성젅")       → "ㅅㅏㅁㅅㅓㅇㅈㅓㄴㅈ"  타이핑 중(IME 조합 중) 미완성 글자까지 비교
  normalize(" SK 하이닉스 ") → "sk하이닉스"    대소문자·공백 무시
  ko_reading("LG화학")  → "엘지화학"         영문 약자를 한글로 읽은 이름('엘지'·'에스케이'로 찾기)

왜 자모 분해가 필요한가
  한글 입력기는 글자를 조합하는 동안 '삼서'(→삼성), '삼성젅'(→삼성전자)처럼 완성 전 글자를 입력창에 둔다.
  완성형 문자열로만 비교하면 이 순간 결과가 0건이 돼 자동완성이 깜빡이거나 '결과 없음'이 뜬다.
  자모로 풀어 접두 비교하면(ㅅㅏㅁㅅㅓ ⊂ ㅅㅏㅁㅅㅓㅇㅈㅓㄴㅈㅏ) 조합 중에도 끊김 없이 후보가 유지된다.
  겹받침(ㄵ→ㄴㅈ)·이중모음(ㅘ→ㅗㅏ)도 풀어야 '젅'→'전자', '고'→'과' 같은 중간 상태가 맞는다.
"""

import re
import unicodedata

CHOSUNG = ["ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ", "ㅅ",
           "ㅆ", "ㅇ", "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
JUNGSUNG = ["ㅏ", "ㅐ", "ㅑ", "ㅒ", "ㅓ", "ㅔ", "ㅕ", "ㅖ", "ㅗ", "ㅘ", "ㅙ", "ㅚ", "ㅛ", "ㅜ", "ㅝ",
            "ㅞ", "ㅟ", "ㅠ", "ㅡ", "ㅢ", "ㅣ"]
JONGSUNG = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ",
            "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
_CHO_SET = set(CHOSUNG)
# 겹받침·이중모음 → 기본 자모(입력 순서대로). 쌍자음(ㄲ·ㅆ…)은 한 키라 풀지 않는다.
_SPLIT = {"ㄳ": "ㄱㅅ", "ㄵ": "ㄴㅈ", "ㄶ": "ㄴㅎ", "ㄺ": "ㄹㄱ", "ㄻ": "ㄹㅁ", "ㄼ": "ㄹㅂ", "ㄽ": "ㄹㅅ",
          "ㄾ": "ㄹㅌ", "ㄿ": "ㄹㅍ", "ㅀ": "ㄹㅎ", "ㅄ": "ㅂㅅ",
          "ㅘ": "ㅗㅏ", "ㅙ": "ㅗㅐ", "ㅚ": "ㅗㅣ", "ㅝ": "ㅜㅓ", "ㅞ": "ㅜㅔ", "ㅟ": "ㅜㅣ", "ㅢ": "ㅡㅣ"}
# 검색에서 무시할 구분 기호(종목명에 섞여 있어도 같은 이름으로 취급: 'LG 에너지', 'CJ제일제당(우)' 등)
_IGNORE = set(" \t\r\n·・.,-_/()[]&'\"")


def _is_syllable(code):
    return 0xAC00 <= code <= 0xD7A3                 # 완성형 한글 음절 영역


def chosung(s):
    """문자열의 각 한글 음절을 초성으로 변환. 한글이 아니면 그대로."""
    out = []
    for ch in s or "":
        code = ord(ch)
        if _is_syllable(code):
            out.append(CHOSUNG[(code - 0xAC00) // 588])
        else:
            out.append(ch)
    return "".join(out)


def is_chosung_query(q):
    """질의가 초성(자음)만으로 이루어졌는지(예: 'ㅅㅅ', 'ㅅㅅㅈㅈ')."""
    return len(q) > 0 and all(ch in _CHO_SET for ch in q)


def normalize(s):
    """검색 비교용 — NFC(완성형) + 소문자 + 공백·구분기호 제거. ('SK 하이닉스' == 'sk하이닉스')
    NFC: macOS 등에서 붙여 넣은 '풀어 쓴(NFD)' 한글('ㅅ+ㅏ+ㅁ…')은 완성형 음절이 아니라 검색이 0건이었다."""
    return "".join(ch for ch in unicodedata.normalize("NFC", s or "").lower() if ch not in _IGNORE)


def jamo(s):
    """완성형 음절을 초·중·종성 자모로 풀고 겹받침/이중모음도 기본 자모로 분해.
    한글이 아닌 문자는 그대로 둔다(정규화는 호출 측에서 normalize 먼저)."""
    out = []
    for ch in s or "":
        code = ord(ch)
        if _is_syllable(code):
            idx = code - 0xAC00
            cho, jung, jong = idx // 588, (idx % 588) // 28, idx % 28
            out.append(CHOSUNG[cho])
            v = JUNGSUNG[jung]
            out.append(_SPLIT.get(v, v))
            if jong:
                f = JONGSUNG[jong]
                out.append(_SPLIT.get(f, f))
        else:
            out.append(_SPLIT.get(ch, ch))          # 단독 자모(ㄳ, ㅘ 등)도 분해
    return "".join(out)


# 영문 약자의 한글 읽기 — 국내 종목명은 'LG화학'·'SK하이닉스'·'KT&G'처럼 약자로 시작하는 게 많은데
# 한국어로 칠 때는 '엘지'·'에스케이'·'케이티앤지'로 친다. 이름에 이 읽기를 별칭으로 붙여 한글로도 찾게 한다.
_LETTER_KO = {"A": "에이", "B": "비", "C": "씨", "D": "디", "E": "이", "F": "에프", "G": "지", "H": "에이치",
              "I": "아이", "J": "제이", "K": "케이", "L": "엘", "M": "엠", "N": "엔", "O": "오", "P": "피",
              "Q": "큐", "R": "알", "S": "에스", "T": "티", "U": "유", "V": "브이", "W": "더블유",
              "X": "엑스", "Y": "와이", "Z": "제트"}
_UPPER_RUN = re.compile(r"[A-Z]+")


def ko_reading(name):
    """'LG화학' → '엘지화학', 'SK하이닉스' → '에스케이하이닉스', 'KT&G' → '케이티앤지', 'HD현대' → '에이치디현대'.
    대문자 1~4자 약자만 글자 단위로 읽는다 — 5자 이상(NAVER·POSCO)은 단어라 그대로, 뒤에 소문자가 이어지면
    ('S-Oil'의 Oil) 단어의 첫 글자라 그대로. 바뀐 게 없으면 None."""
    s = name or ""

    def read(m):
        run = m.group(0)
        nxt = s[m.end():m.end() + 1]
        if len(run) > 4 or nxt.islower():
            return run
        return "".join(_LETTER_KO[c] for c in run)
    out = _UPPER_RUN.sub(read, s)
    if out == s:
        return None
    return out.replace("&", "앤")
