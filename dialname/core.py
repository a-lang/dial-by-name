# SPDX-License-Identifier: AGPL-3.0-or-later
"""dialname.core — 語音撥號：姓名索引與模糊/同音比對（參考實作）

設計原則
--------
1. 比對用「拼音」而不是漢字：同音字（張/章、王/汪、陳/程、林/凌）在無聲調拼音下
   完全同形，所以「同音」不需要額外的同音字典，是拼音化的自然結果。
2. 漢字只用來做「字面加分」（逐字命中）與顯示，不用來當唯一鍵。
3. 兩層寬鬆度：tight（原拼音）與 loose（台灣常見模糊音合併），loose 只做折扣加分。
4. 候選收斂與最終決策都在本地完成（無外部模型存取）。
"""

from __future__ import annotations

import json
import re
import string
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import islice, product
from pathlib import Path

import opencc
from pypinyin import Style, lazy_pinyin, pinyin
from rapidfuzz import fuzz

_T2S = opencc.OpenCC("t2s")
_S2T = opencc.OpenCC("s2t")

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
PUNCT_RE = re.compile(r"[\s\u3000,，。.、;；:：!！?？()（）\[\]【】\"'“”‘’·…~～\-—_/\\|]+")
ASCII_RUN_RE = re.compile(r"[a-z0-9]+")
DEPT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# 只折 ASCII 大小寫：英文部門別名（Sales/IT）要能命中 STT 的 sales/it，
# 但中文、拼音與其他語系不動。
_ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


# 口語贅詞（越長越先剝）
NOISE_PREFIXES = [
    "我要找他一下", "我想找一下", "請幫我轉接", "請幫我接", "麻煩幫我接", "幫我轉接",
    "幫我接", "麻煩接", "我要找", "我想找", "我要打給", "請轉", "請接", "轉接", "轉", "接", "找",
    # 「喂」經過 normalize() 的簡→繁會被轉成「餵」，真正會出現的是後者；
    # 兩個都留，避免日後調整 normalize 時這條前綴又變成死條目。
    # （實機：「喂你好請問王偉在嗎？」殘留「餵你好請問王偉」）
    "請問", "你好", "餵", "喂", "一個", "一位", "叫",
    # 「我找王先生」「幫我找一下王偉」是最常見的講法；不加這幾條會留下一個「找」字
    # 去拉低分數（實測：'我找王先生' 會變成 q='找王'，分數 0.57 而不是 0.62）
    "幫我找一下", "幫我找", "我找", "麻煩找",
]
# 稱謂（結尾剝除）
TITLES = [
    "總經理", "副總經理", "協理", "副理", "經理", "主任", "課長", "組長", "主管",
    "先生", "小姐", "女士", "專員", "助理", "工程師", "博士", "老師", "醫師", "姊", "姐", "哥",
    # 親屬／敬稱：剝掉之後只剩姓 → 走「只講姓」的處理（不追問，直接給總機）
    # 注意「董」也是姓（董先生）：靠 len(s) > len(t) 的守衛避免把「董」本身剝掉
    "伯伯", "阿伯", "叔叔", "阿姨", "阿嬤", "大哥", "大姐", "大姊", "董", "老大",
]
# 結尾客套／緩衝詞（越長越先剝）。不剝會留在查詢裡把每個候選的拼音覆蓋率稀釋掉：
# 實測「你好我要找王偉麻煩一下」→ q 變成 8 個字，同音兄弟 汪偉 從 1.0 掉到 0.5143，
# 低於同音門檻 0.70，於是「跨部門同音」被誤判成 confident 而直接轉接。
SUFFIX_NOISE = [
    "麻煩一下", "麻煩妳", "麻煩你", "麻煩您", "拜託一下", "不好意思",
    "謝謝妳", "謝謝你", "謝謝您", "謝謝", "感謝", "拜託", "請問一下",
    "一下", "好嗎", "好不好", "可以嗎",
]
# 台灣常見模糊音（只用在模糊音層，與 tight 併算後 ×0.9）
_FUZZY_INITIALS = [("zh", "z"), ("ch", "c"), ("sh", "s"), ("n", "l"), ("r", "l")]
_FUZZY_FINALS = [("eng", "en"), ("ing", "in"), ("ang", "an"), ("iang", "ian"), ("uang", "uan"), ("v", "u")]


# --------------------------------------------------------------------------- #
# 正規化
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    """NFKC（全形→半形）→ 簡→繁（Scribe 中文輸出常是簡體）→ 去標點空白。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    # 只有在含漢字時才做繁化，避免把日文/韓文亂轉
    if CJK_RE.search(s):
        s = _S2T.convert(s)
    s = PUNCT_RE.sub("", s)
    return s.translate(_ASCII_FOLD).strip()


def to_simplified(text: str) -> str:
    """繁 → 簡。公開版本，給測資產生器用（模擬 STT 常輸出簡體）。

    存在理由：`eval.py` 原本自己 new 一個 `opencc.OpenCC("t2s")`，
    等於同一件事有兩個轉換器實例；轉換規則要改時只會改到其中一個。
    """
    return _T2S.convert(text)


def _strip_fillers(s: str, explain: list[str] | None = None) -> str:
    """去掉中文口語虛詞（不會出現在人名裡的字）。"""
    for ch in ("的", "個"):
        if ch in s:
            s = s.replace(ch, "")
            if explain is not None:
                explain.append(f"虛詞:{ch}")
    # 這些字不會出現在台灣姓名裡，出現在頭尾就當虛詞剝掉（含「找」）。
    # 但「單位」的「位」不是虛詞；必須保留給 dept_match 剝除，否則 `IT單位` 會變成 `IT單`。
    # 「呃／嗯／欸／哦」是 STT 最常見的開頭墊字；不剝的話 NOISE_PREFIXES 的
    # startswith 全部失效，整句都進比對（實機：「呃，我要找古廷伟。」→ 0 候選）。
    # 「好」是客人最常見的開頭（「好，請問…」），但不在 NOISE_PREFIXES 裡；
    # 不剝掉的話整串前綴剝除全部失效（「好請問小美」→ 小名比對永遠 miss）。
    fillers = "了在是我你他她們啦喔啊嗎呢就要想有那這請問題跟和與找呃嗯欸哦好"
    while s and s[0] in fillers:
        if explain is not None:
            explain.append(f"虛詞:{s[0]}")
        s = s[1:]
    while s and s[-1] in fillers:
        if s.endswith("單位"):
            break
        if explain is not None:
            explain.append(f"虛詞:{s[-1]}")
        s = s[:-1]
    return s


def strip_noise(text: str, explain: list[str] | None = None) -> tuple[str, list[str]]:
    """剝掉口語贅詞、稱謂與虛詞；回傳 (剩下的字串, 命中的詞)。

    `explain` 傳入 list 時，連「虛詞逐字剝了哪些」都會記進去（trace 用）。
    """
    s = text
    removed: list[str] = []
    changed = True
    while changed:
        changed = False
        for p in NOISE_PREFIXES:
            if s.startswith(p) and len(s) > len(p):
                s = s[len(p):]
                removed.append(p)
                changed = True
    changed = True
    while changed:
        changed = False
        for t in TITLES:
            if s.endswith(t) and len(s) > len(t):
                s = s[: -len(t)]
                removed.append(t)
                changed = True
    # 客套／緩衝詞放在稱謂之後：先剝「先生」再剝「麻煩一下」，
    # 順序反過來會留下「...王偉先生」的尾巴。
    changed = True
    while changed:
        changed = False
        for t in SUFFIX_NOISE:
            if s.endswith(t) and len(s) > len(t):
                s = s[: -len(t)]
                removed.append(t)
                changed = True
    fillers: list[str] = []
    s = _strip_fillers(s, fillers)
    if explain is not None:
        explain.extend(removed)
        explain.extend(fillers)
    return s, removed


# --------------------------------------------------------------------------- #
# 拼音
# --------------------------------------------------------------------------- #
def _syllables(text: str) -> list[str]:
    out: list[str] = []
    for tok in lazy_pinyin(text, style=Style.NORMAL, errors=lambda x: list(x)):
        tok = tok.lower()
        if tok:
            out.append(tok)
    return out


def pinyin_variants(text: str, max_variants: int = 8) -> list[list[str]]:
    """回傳所有破音字組合（曾/樂/解…）。單字最多 2–3 讀音，組合數可控。"""
    per_char: list[list[str]] = []
    for tok in pinyin(text, style=Style.NORMAL, heteronym=True, errors="default"):
        if isinstance(tok, list):
            cleaned = [t.lower() for t in tok if t]
            if cleaned:
                per_char.append(cleaned)
        elif tok:
            per_char.append([tok.lower()])
    combos: list[list[str]] = []
    for combo in islice(product(*per_char), max_variants):
        combos.append(list(combo))
    return combos or [_syllables(text)]


def fuzzy_syllable(syl: str) -> str:
    s = syl
    for a, b in _FUZZY_INITIALS:
        if s.startswith(a):
            s = b + s[len(a):]
            break
    for a, b in _FUZZY_FINALS:
        s = s.replace(a, b)
    return s


@lru_cache(maxsize=512)
def _fuzzy_pinyin_key(text: str) -> frozenset[tuple[str, ...]]:
    """整串拼音（含破音字）套上 `fuzzy_syllable` 後的鍵集合。

    給「字面不同、發音相同」的比對用：STT 在 8 kHz＋台灣口音下常把
    `zh/ch/sh` 聽成 `z/c/s`（實機：「人資部」→「人之不」）。
    回傳 frozenset 才能放進 `lru_cache`，也讓部門別名只算一次。
    """
    return frozenset(tuple(fuzzy_syllable(s) for s in v) for v in pinyin_variants(text))


def name_match_keys(text: str) -> set[str]:
    """「同一個講法」的鍵集合：正規化字面 + 整串拼音（tight 與模糊音）。

    給 alias（小名／英文名）與「只講名」比對用：
      - 中文小名：`小美` 與 `筱美` 的拼音鍵都是 `xiao mei`
      - 模糊音：`美玲`（mei ling）與 `梅林`（mei lin）在 n/ng 合併後相同
      - 英文別名：只靠字面鍵（英文不做拼音）
    """
    n = normalize(text)
    if not n:
        return set()
    keys = {n.lower()}
    if CJK_RE.search(n):
        for v in pinyin_variants(n):
            if v:
                keys.add(" ".join(v))
                keys.add(" ".join(fuzzy_syllable(s) for s in v))
    return keys


def _window_keys(q: str, min_len: int = 2, max_len: int = 4) -> set[str]:
    """查詢字串裡每一段 2–4 字視窗的 `name_match_keys` 聯集。

    給「只講名」分支用。原本整串鍵比對要求殘留**剛好等於**小名，實機
    「好，請問小美在嗎？」殘留「好請問小美」時整串永遠對不上，同音的
    「筱美」就從候選池消失，只剩字面命中的陳美玲以 0.95 直轉（2026-11-17 驗收）。
    alias 是封閉白名單，逐窗比對不會生出名冊以外的候選，所以放寬是安全的。
    """
    n = len(q)
    out: set[str] = set()
    for size in range(min_len, max_len + 1):
        if size > n:
            break
        for i in range(n - size + 1):
            out |= name_match_keys(q[i:i + size])
    return out


def _incompatible_name(q: str, p: Person) -> bool:
    """同字數、整串發音完全不相容、且不是別名命中 → 視為不同人。

    同姓 ＋ 同字數就先拿 0.44 的結構分，字形重疊再推上去；若不擋，
    `王大明` 會變成 王小明(0.84)、`李佳怡` 會變成 李佳容(0.88)，
    然後被當成「需要澄清」甚至「單一候選」直接轉給陌生人（2026-11-14 實機）。

    只作用在「字數相同」的查詢：漏字（張豪→張家豪）與只講姓（王→王偉）
    長度不同，維持原本的寬鬆度。
    """
    if len(q) != len(p.name_key) or not CJK_RE.search(q):
        return False
    if any(normalize(a) and normalize(a) in q for a in p.aliases):
        return False        # 別名命中（中文綽號／英文名）不套用姓名拼音規則
    return not (_fuzzy_pinyin_key(q) & _fuzzy_pinyin_key(p.name_key))


def initials_of(syls: list[str]) -> str:
    """聲母序列，例：['wang','wei'] -> 'w w'（比字尾更抗韻母辨識錯誤）。"""
    out = []
    for syl in syls:
        for a in ("zh", "ch", "sh"):
            if syl.startswith(a):
                out.append(a)
                break
        else:
            out.append(syl[0] if syl else "")
    return " ".join(out)


# --------------------------------------------------------------------------- #
# 資料模型
# --------------------------------------------------------------------------- #
@dataclass
class Person:
    pid: str
    name: str                       # 顯示用（繁體）；對應 userman_users.displayname
    directory: str = ""             # 來源 directory（userman_directories.name）
    dept: str = ""                  # 目錄原始 department 字串（可空；只用來解析）
    title: str = ""
    extension: str = ""
    aliases: list[str] = field(default_factory=list)  # 英文名、綽號、舊名
    # 結構化姓名欄位（userman_users.lname / fname）。沒有值時才退回「顯示名第 1 字＝姓」。
    # 有欄位就不能用字串切：西方名（David Wang）、複姓（歐陽）、只有名沒有姓都會切錯。
    last_name: str = ""
    first_name: str = ""

    # 索引欄位（build 時填）
    name_key: str = ""
    dept_id: str = ""               # 路由鍵（ASCII，由 dept 解析而來）
    dept_name: str = ""             # 正規部門顯示名（中文，由 dept 解析而來）
    surname_key: str = ""           # 姓（比對用）
    given_key: str = ""             # 名（比對用）
    variants: list[list[str]] = field(default_factory=list)
    fuzzy_variants: list[list[str]] = field(default_factory=list)
    initials: list[str] = field(default_factory=list)
    sur_variants: list[list[str]] = field(default_factory=list)     # 姓的拼音變體
    sur_fuzzy: list[list[str]] = field(default_factory=list)
    given_variants: list[list[str]] = field(default_factory=list)   # 名的拼音變體
    given_fuzzy: list[list[str]] = field(default_factory=list)
    alias_keys: set[str] = field(default_factory=set)               # 小名／英文名的同音鍵


@dataclass
class Department:
    dept_id: str
    name: str
    aliases: list[str] = field(default_factory=list)


def build_person(p: Person) -> Person:
    p.name_key = normalize(p.name)
    # 姓名拆分：優先序 lname/fname → 顯示名（第 1 字＝姓）→ 整串當名（沒有姓）
    # 用欄位而不是字串切，才處理得了西方名、複姓、只有名的情況。
    surname = normalize(p.last_name) if p.last_name else ""
    given = normalize(p.first_name) if p.first_name else ""
    if not surname and not given:
        surname, given = p.name_key[:1], p.name_key[1:]
    elif not surname:
        # 有 fname 沒有 lname：若 fname 就是整個顯示名，視為沒有姓
        surname = "" if p.name_key.startswith(given) else p.name_key[: max(len(p.name_key) - len(given), 0)]
    elif not given:
        given = p.name_key[len(surname):] if p.name_key.startswith(surname) else p.name_key
    p.surname_key, p.given_key = surname, given
    p.variants = pinyin_variants(p.name_key)
    p.fuzzy_variants = [[fuzzy_syllable(s) for s in v] for v in p.variants]
    p.initials = [initials_of(v) for v in p.variants]
    # 姓／名分開的拼音變體（姓同音與名同音的權重不同，必須分開算）
    p.sur_variants = pinyin_variants(surname) if surname else []
    p.sur_fuzzy = [[fuzzy_syllable(s) for s in v] for v in p.sur_variants]
    p.given_variants = pinyin_variants(given) if given else []
    p.given_fuzzy = [[fuzzy_syllable(s) for s in v] for v in p.given_variants]
    p.alias_keys = set()
    for alias in p.aliases:
        p.alias_keys |= name_match_keys(alias)
    return p


# --------------------------------------------------------------------------- #
# 比對
# --------------------------------------------------------------------------- #
def query_features(query: str) -> tuple[list[list[str]], list[str]]:
    """查詢的拼音特徵只算一次（原本每個候選人重算一次，是最大的效能浪費）。

    回傳 (破音字變體, 聲母序列)。姓名分開計分之後，整串的模糊音版本不再需要，
    模糊音改在 `_part_sim()` 內對「姓」「名」各自計算。
    """
    variants = pinyin_variants(query)
    initials = [initials_of(v) for v in variants]
    return variants, initials


# 六項加權：改權重只改這裡，name_score() 與 trace 的拆解都吃同一份
# 優先序（取捨理由見 `docs/decision-logic.md#scoring`）：**姓的同音 > 字數 > 名的同音**
#   sur 0.30 > length 0.14 > given 0.10
# 中文姓是封閉集合（幾百個），同音的姓（王/汪、張/章）幾乎等於同一個人；
# 名字較發散，同音只能當輔助。字數居中，因為 2 字 vs 3 字幾乎一定是不同人。
WEIGHTS = {
    "char": 0.20,          # 漢字逐字（ratio，長度敏感）
    "char_partial": 0.16,  # 漢字部分命中
    "sur": 0.30,           # **姓**的拼音吻合（tight + 模糊音）
    "given": 0.10,         # **名**的拼音吻合（同上）
    "initials": 0.10,      # 聲母序列（抗韻母誤聽）
    "length": 0.14,        # 字數吻合度（min(len_q,len_n) / max(...)）
}

# 「查詢比姓名多字」的結構折扣指數：查詢多一個字時，姓名必須連續出現才算部分命中。
# 1.0 = 線性（原本）、2.0 = 平方（多一個字就掉到 0.44，直接低於 MIN_SCORE）
LENGTH_SHRINK_POWER = 2.0

# 沒有漢字重疊時（純同音、STT 全字形都錯）拼音項只給半權重：
# 否則亂句（發票事 vs 范曉琪）會靠拼音序相似 + 字數分混過門檻。
PINYIN_EVIDENCE_NO_CHAR = 0.5


@dataclass
class ScoreParts:
    """單一（查詢, 人）配對的評分組成。trace 靠它回答「為什麼這句只有 0.57」。"""

    char: float = 0.0
    char_partial: float = 0.0
    sur: float = 0.0            # 姓的拼音吻合
    given: float = 0.0          # 名的拼音吻合
    initials: float = 0.0
    length: float = 0.0         # 字數吻合度
    raw: float = 0.0            # 六項加權和（任何提分／壓分之前）
    override: str = ""          # 觸發的強制提分：exact-in-query / name-prefix / alias
    cap: str = ""               # 觸發的壓分：單字查詢、兩字查詢
    partial_length: float = 0.0  # 查詢比姓名長時的長度折扣（未觸發為 0）
    variant: int = 0            # 最後勝出的破音字組合索引
    final: float = 0.0

    def contributions(self) -> dict[str, float]:
        """每一項實際貢獻的分數（值 × 權重），加總 = raw。"""
        return {k: round(getattr(self, k) * w, 4) for k, w in WEIGHTS.items()}


def _part_sim(
    q_syls: list[str],
    tight_variants: list[list[str]],
    fuzzy_variants: list[list[str]],
) -> float:
    """一段（姓或名）的拼音吻合度：tight 與模糊音兩層取較高者。

    模糊音層乘 0.9：只在「合併後明顯更接近」時才勝出（林/凌、陳/程），
    否則真同音的 tight 應該主導。
    """
    if not q_syls or not tight_variants:
        return 0.0
    qs = " ".join(q_syls)
    best_t = max(
        (0.6 * fuzz.ratio(qs, " ".join(v)) + 0.4 * fuzz.partial_ratio(qs, " ".join(v))) / 100.0
        for v in tight_variants
    )
    qf = " ".join(fuzzy_syllable(s) for s in q_syls)
    best_f = max(
        (0.6 * fuzz.ratio(qf, " ".join(v)) + 0.4 * fuzz.partial_ratio(qf, " ".join(v))) / 100.0
        for v in fuzzy_variants
    ) if fuzzy_variants else 0.0
    return max(best_t, 0.9 * best_f)


def score_breakdown(
    query: str,
    p: Person,
    features: tuple[list[list[str]], list[list[str]], list[str]] | None = None,
    raw: str | None = None,
) -> ScoreParts:
    """name_score() 的展開版。query 必須已 normalize + strip_noise。

    `raw` 是剝贅詞／稱謂**之前**的整句（已 normalize）。別名要比對它：
    否則「找王伯伯」剝掉「伯伯」只剩「王」，就永遠對不到 alias 登記為「王伯伯」的人。
    """
    if not query:
        return ScoreParts()
    q = query
    q_variants, q_init = features or query_features(q)

    best: ScoreParts | None = None
    for i, qv in enumerate(q_variants):
        char = fuzz.ratio(q, p.name_key) / 100.0
        char_partial = fuzz.partial_ratio(q, p.name_key) / 100.0

        # 姓與名分開算拼音吻合：姓的音節數由名冊決定（複姓 2 個音節）。
        # 兩種對齊都算：
        #   A. 查詢前 k 個音節＝姓（正常情況）
        #   B. 整串查詢都是「名字」（只講名字：美玲 對 陳美玲）
        # 名的部分取兩者較高者；姓只有 A 有意義。
        k_sur = len(p.sur_variants[0]) if p.sur_variants else 0
        q_sur = qv[:k_sur] if k_sur else []
        sur = _part_sim(q_sur, p.sur_variants, p.sur_fuzzy)
        given_a = _part_sim(qv[k_sur:] if k_sur else list(qv), p.given_variants, p.given_fuzzy)
        given_b = _part_sim(list(qv), p.given_variants, p.given_fuzzy)
        given = max(given_a, given_b)

        # 完全沒有漢字重疊時，拼音只給半權重（見 PINYIN_EVIDENCE_NO_CHAR）
        if char_partial == 0:
            sur *= PINYIN_EVIDENCE_NO_CHAR
            given *= PINYIN_EVIDENCE_NO_CHAR

        ini = max((fuzz.ratio(qi, ni) / 100.0 for qi in q_init for ni in p.initials), default=0.0)

        # 字數吻合度：2 字名 vs 3 字查詢 = 2/3；同字數 = 1.0。
        # 只給「字有對上」的候選：實測亂句「我要問發票的事」→ q='發票事' 與 范曉琪
        # 字數剛好相同（3 vs 3）且拼音序相似（tight 0.75），若無條件給 0.14 的字數分，
        # 一個漢字都沒對上也會被推過 MIN_SCORE 0.45，變成「亂句抓到人」。
        # char_partial == 0 → 視為沒有名字證據，字數分不計。
        len_q, len_n = len(q), len(p.name_key)
        length_sim = 0.0
        if char_partial > 0 and len_q and len_n:
            length_sim = min(len_q, len_n) / max(len_q, len_n)

        part = ScoreParts(char, char_partial, sur, given, ini, length_sim, variant=i)
        weighted = sum(getattr(part, k) * w for k, w in WEIGHTS.items())
        part.raw = round(weighted, 4)
        score = weighted

        # 查詢比姓名多字時的結構折扣：姓名必須「連續出現」才算部分命中。
        # 否則 rapidfuzz 的 ratio 只看字有沒有出現（不看順序）：
        # 「王智偉」對「王偉」會給 char=0.80（王、偉都在裡面），總分 0.7565，
        # 於是完全不同的人被當成候選，還稀釋了真正的 top-1。
        if p.name_key and len_q > len_n and p.name_key not in q:
            shrink = (len_n / len_q) ** LENGTH_SHRINK_POWER
            score *= shrink
            part.partial_length = round(shrink, 4)

        # 完整包含：query 內含全名，或全名內含 query（短 query 要打折）
        if p.name_key and p.name_key in q:
            if score < 0.97:
                part.override = "exact-in-query→0.97"
            score = max(score, 0.97)
        elif q in p.name_key:
            ratio = len(q) / max(len(p.name_key), 1)
            if score < 0.55 + 0.4 * ratio:
                part.override = f"name-prefix→{0.55 + 0.4 * ratio:.3f}"
            score = max(score, 0.55 + 0.4 * ratio)

        # 英文/別名（也看未剝詞的整句，見 raw 的說明；反向包含仍以剝詞後的 q 判斷）
        haystack = (q if raw is None else q + "\x00" + raw).lower()
        alias_hit = False
        for alias in p.aliases:
            a = normalize(alias).lower()
            # 反向包含（q 是 alias 的子字串）要求 q 至少 2 字：否則「王先生」會因為
            # alias「王伯伯」而以 q='王' 反向命中，單姓就被誤認成指名道姓。
            if a and (a in haystack or (len(q) >= 2 and q.lower() in a)):
                if score < 0.95:
                    part.override = f"alias:{alias}→0.95"
                score = max(score, 0.95)
                alias_hit = True

        # 單字查詢（只說姓）本質上不可能唯一 → 壓分，交給澄清流程。
        # 但 alias 命中是「指名道姓」等級的證據（例如 alias 就登記「王伯伯」），不受 cap 限制。
        if alias_hit:
            pass
        elif len(q) <= 1 and score > 0.62:
            score = 0.62
            part.cap = "len==1→cap 0.62"
        elif len(q) == 2 and len(p.name_key) >= 3 and score > 0.88:
            score = 0.88
            part.cap = "len==2→cap 0.88"

        part.final = round(score, 4)
        if best is None or part.final > best.final:
            best = part
    return best or ScoreParts()


def name_score(
    query: str,
    p: Person,
    features: tuple[list[list[str]], list[list[str]], list[str]] | None = None,
    raw: str | None = None,
) -> float:
    """0–1。query 必須已 normalize + strip_noise。"""
    return score_breakdown(query, p, features, raw=raw).final


def dept_match(query: str, departments: list[Department]) -> tuple[Department | None, str]:
    """在 query 裡找部門別名（長別名優先），回傳 (部門, 剝除後的字串)。

    **query 必須已經 `normalize()`**（ASCII 已折小寫）；直接傳 `"IT"` 會因為
    `ASCII_RUN_RE` 只收小寫而 miss。

    中文別名用 substring；**純 ASCII 別名只對「整段 Latin run」比對**，
    否則 `IT` 會在 `with`／`quality` 裡誤中（casefold 之後更嚴重）。
    部門詞後面的「部門／單位」一併剝掉：「我要找IT部門」＝「我要找IT」。
    """
    runs = ASCII_RUN_RE.findall(query)
    best: tuple[int, Department, str] | None = None
    for d in departments:
        for alias in [d.name, *d.aliases]:
            a = normalize(alias)
            if not a:
                continue
            if a.isascii():
                if a not in runs:
                    continue
            elif a not in query:
                continue
            idx = query.find(a)
            rest = query[:idx] + query[idx + len(a):]
            rest = rest.replace("部門", "").replace("單位", "")
            # 裸部門的常見後綴（IT部／工程處／資訊科）也算「整句只有部門名」
            if rest in {"部", "處", "科", "室", "組", "中心"}:
                rest = ""
            cand = (len(a), d, rest)
            if best is None or cand[0] > best[0]:
                best = cand
    if not best:
        # 拼音後援：字面沒中時，比「整句發音」是否等於某個部門別名。
        # 只接受整串相等（含破音字變體），所以不會把半句話誤認成部門。
        # 2026-11-14 實機：找李佳容（人資部），第二輪「人資部」被聽成「人之不」——
        # zi→zhi、bu→bu，字面比對救不到，發音則完全相同。
        if query:
            key = _fuzzy_pinyin_key(query)
            for d in departments:
                for alias in [d.name, *d.aliases]:
                    a = normalize(alias)
                    if len(a) < 2 or a.isascii():
                        continue
                    if key & _fuzzy_pinyin_key(a):
                        return d, ""
        return None, query
    return best[1], best[2]


def title_match(query: str, p: Person) -> bool:
    return bool(p.title) and normalize(p.title) in query


def is_given_name_query(q: str, p: Person) -> bool:
    """q 是不是「只講了名、沒講姓」？

    用結構化欄位判斷（`fname`）：q == given_key 且不等於完整姓名。
    例：`美玲` 對 `陳美玲` 是；`陳美玲` 對自己不是。
    """
    g = p.given_key
    return bool(g) and q == g and q != p.name_key


def looks_incomplete_name(q: str, p: Person) -> bool:
    """q 像「漏掉字」的姓名嗎？

    用結構化欄位判斷，不靠字串切：
      - q 等於 fname → 這是「只講名字」，不是漏字（`美玲` 對 `陳美玲`）
      - q 等於 lname → 這是「只講姓」，不是漏字
      - 其餘：q 剛好是完整姓名少一個字（`張豪` 對 `張家豪`、`林明` 對 `林志明`）

    只有姓名 ≥3 字才可能被判為漏字；2 字姓名少一個字就只剩姓，走「只講姓」那條。
    """
    n = p.name_key
    if not CJK_RE.search(n):        # 英文名掉一個字母是拼字錯誤，不是「漏字」
        return False
    if len(n) < 3 or len(q) != len(n) - 1:
        return False
    if q in (p.given_key, p.surname_key):
        return False
    return any(n[:i] + n[i + 1:] == q for i in range(len(n)))


MIN_SCORE = 0.45   # 低於此分數一律當作「沒有可信候選」
ALIAS_MATCH_SCORE = 0.95   # alias（小名／英文名）命中＝指名道姓，直接給這個分數
HOMOPHONE_FLOOR = 0.70   # 第二名要到此分數，才把「同音兄弟」當成風險（唯一入口）
CONFIDENT_SCORE = 0.92   # 第一名要到此分數才可能跳過澄清

# 部門／職稱當「強訊號」時的加減分（唯一入口；消融實驗靠改這裡）
DEPT_BONUS = 0.10        # 候選與來電者所說的部門相同
DEPT_PENALTY = 0.08      # 不同部門（不是硬過濾：同音不同部門仍要能被看到）
TITLE_BONUS = 0.04       # 來電者提到職稱且與候選相符
DEPT_SCOPE_MIN = 1       # 部門收斂候選池至少要剩幾人（1 = 部門內唯一的人也收斂）
# 為什麼是 1：STT 對「部門名」這種封閉詞彙的辨識遠比姓名可靠，所以來電者報了部門，
# 就應該先在該部門裡找。設成 2 會漏掉「該部門只有一個人」的情況，於是那個唯一人選
# 不參與競爭，字面完全命中的錯字候選（王偉）就會贏過正確的同音候選（汪偉，客服部）。
# 部門內完全沒有候選時仍會退回全名冊（見 _search 的 scoped 判斷），所以不是硬過濾。

# ── 部門路由的意圖閘門 ──────────────────────────────────────────────────
# 句子裡出現部門名，不代表來電者要「轉到那個部門」。
# 「我要談業務合作」提到業務部，但意圖是談生意；「客服態度很差我要投訴」提到客服部，
# 但意圖是抱怨。這種句子如果自動轉部門，等於系統在亂猜。
#
# 所以部門路由要求「路由意圖詞」（找／接／轉／分機／窗口…）或整句就是部門名
# （殘留字串為空）。實測：5 句部門詞陷阱全部被擋下，3 種合法講法不受影響。
ROUTING_CUES = ("找", "接", "轉", "分機", "窗口", "聯絡", "聯繫", "打給")


def has_routing_cue(text: str) -> bool:
    """這句話有沒有「請幫我接某人／某部門」的意圖。"""
    t = normalize(text)
    return any(cue in t for cue in ROUTING_CUES)


@dataclass
class Candidate:
    person: Person
    score: float
    reasons: list[str] = field(default_factory=list)


def _search(
    utterance: str,
    people: list[Person],
    departments: list[Department],
    top_k: int = 20,
    pool=None,
    _fallback: bool = True,
    _explain: dict | None = None,
    alias_pids: dict[str, set[str]] | None = None,
    surname_firsts: set[str] | None = None,
    given_keys: set[str] | None = None,
    meta: dict | None = None,
) -> tuple[list[Candidate], Department | None, str]:
    """回傳 (候選清單, 命中的部門, 正規化後的查詢字串)。

    部門是「強訊號但不是硬過濾」：同部門 +0.10、不同部門 −0.08，
    讓「財務部的張先生」這種『同音不同部門』的情況仍能看到兩個候選，
    交給後面的澄清 / 決策決定。

    `_explain` 傳入 dict 時，會把所有中間值（含被門檻砍掉的候選）寫進去，
    供 trace 除錯用；平常不傳，行為與效能不變。

    `meta` 是**對外**的分支結果（`alias`／`given_like`），給決策層用；
    與 `_explain`（除錯用）刻意分開，避免把除錯資料帶進正式路徑。
    """
    q = normalize(utterance)
    # 先剝口語贅詞，再找部門：否則「轉業務部」會被部門比對吃掉「業務部」而留下「轉」
    noise_log: list[str] | None = [] if _explain is not None else None
    q_clean, removed = strip_noise(q, noise_log)
    dept, q_name = dept_match(q_clean, departments)
    q_name = _strip_fillers(q_name, noise_log)

    if _explain is not None:
        _explain.update(
            normalized=q, noise_removed=noise_log or [], cleaned=q_clean,
            dept=dept.name if dept else None, dept_residual=q_name,
            pool="bucket" if pool is not None else "full", rows=[], fallback=False,
            pool_size=0, dept_scoped=False,
        )

    if not q_name:
        return [], dept, ""

    # ── 「只講名／小名」分支（2026-11-16 定案） ──────────────────────────
    # 查詢首字不是名冊裡的姓時，姓名模糊比對**不可靠**：實撥「美玲」被 STT 聽成
    # 「梅林」後以 0.4816 命中「林志明」並直接轉給陌生人（單一候選不澄清）。
    # 所以這條路只認 alias（小名／英文名，同音）：
    #   命中 1 人 → 直接轉；命中多人 → 追問部門；查無但像某人的名 → 轉總機。
    # 兩者皆非（例如亂句「訂便當」）→ 照原本流程，避免把垃圾句送進總機。
    alias_pool: set[str] | None = None
    if (
        alias_pids is not None
        and surname_firsts is not None
        and q_name[0] not in surname_firsts
    ):
        # 比對用「整串鍵 ＋ 2–4 字窗鍵」：客人不會只說小名（「好，請問小美在嗎？」），
        # 只比整串會讓同音小名整個人消失，字面命中的那位卻拿到 0.95 直轉。
        keys = name_match_keys(q_name) | _window_keys(q_name)
        hits: set[str] = set()
        for k in keys:
            hits |= alias_pids.get(k, set())
        if hits:
            alias_pool = hits
            if meta is not None:
                meta["alias"] = True
        elif given_keys and keys & given_keys:
            if meta is not None:
                meta["given_like"] = True
            return [], dept, q_name

    candidates_pool = pool(q_name) if pool else people
    if alias_pool is not None:
        # alias 命中就是「指名道姓」，直接用命中的那幾個人，不經首音節桶
        candidates_pool = [p for p in people if p.pid in alias_pool]
    if _explain is not None:
        _explain["pool_size"] = len(candidates_pool)
        _explain["dept_scoped"] = False
    # 部門限制可以再縮小一次候選池
    if dept is not None:
        scoped = [p for p in candidates_pool if p.dept_id == dept.dept_id]
        if len(scoped) >= DEPT_SCOPE_MIN:
            candidates_pool = scoped
            if _explain is not None:
                _explain["dept_scoped"] = True
                _explain["pool_size"] = len(candidates_pool)

    feats = query_features(q_name)
    scored: list[Candidate] = []
    for p in candidates_pool:
        parts = score_breakdown(q_name, p, feats, raw=q) if _explain is not None else None
        s = parts.final if parts is not None else name_score(q_name, p, feats, raw=q)
        base = s
        reasons: list[str] = []
        dept_delta = 0.0
        if dept is not None:
            same = p.dept_id == dept.dept_id
            s = min(1.0, s + DEPT_BONUS) if same else max(0.0, s - DEPT_PENALTY)
            dept_delta = round(s - base, 4)
            if same:
                reasons.append(f"dept:{dept.name}")
        title_delta = 0.0
        if title_match(q_name, p):
            before = s
            s = min(1.0, s + TITLE_BONUS)
            title_delta = round(s - before, 4)
            reasons.append(f"title:{p.title}")
        if p.name_key and p.name_key in q_name:
            reasons.append("exact-name")
        elif q_name in p.name_key:
            reasons.append("name-prefix")
        # 單一漢字的查詢（只講姓）本來就只會拿到 0.3–0.6，門檻要另外放寬，
        # 否則「財務部的張先生」會在部門內全滅、被誤判成「沒聽懂」
        floor = MIN_SCORE if len(q_name) > 1 else 0.30
        if alias_pool is not None and p.pid in alias_pool:
            # alias 命中＝指名道姓：分數固定 0.95，不受姓名相似度影響
            # （「筱美」對「程美玲」的姓名分數很低，但 alias 就是她的登記小名）
            s = max(s, ALIAS_MATCH_SCORE)
            reasons.append("alias")
            floor = 0.0
        kept = s >= floor          # 低於門檻視為「沒聽懂」，不要拿雜訊去問使用者
        if _explain is not None:
            _explain["rows"].append(
                dict(pid=p.pid, name=p.name, dept=p.dept_name or p.dept, parts=parts,
                     base=round(base, 4), dept_delta=dept_delta, title_delta=title_delta,
                     score=round(s, 4), floor=floor, kept=kept, reasons=list(reasons))
            )
        if kept:
            scored.append(Candidate(p, round(s, 4), reasons))

    scored.sort(key=lambda c: (-c.score, c.person.pid))
    # 發音完全不像的同字數姓名不是同一個人（王大明≠王小明）；清掉之後若全滅，
    # 下面的 fallback 會再全掃一次，兩條路徑都經過同一道濾網。
    # alias 命中的候選不受這道規則約束：小名本來就不會跟本名發音相近。
    scored = [
        c for c in scored
        if "alias" in c.reasons or not _incompatible_name(q_name, c.person)
    ]
    # alias 分支是權威判定，不可以被「全掃」覆蓋掉（否則梅林又會被林志明撿回去）
    if not scored and _fallback and pool is not None and alias_pool is None:
        # 桶/部門縮小後全滅 → 只回退一次全掃（用旗標，不要用物件 identity 判斷，
        # 否則 scoped 每次都是新 list，會無限遞迴）
        if _explain is not None:
            _explain["fallback"] = True
        return _search(utterance, people, departments, top_k, _fallback=False, _explain=_explain)
    return scored[:top_k], dept, q_name


def search(utterance: str, people: list[Person], departments: list[Department], top_k: int = 20):
    """便利函式（每次呼叫都重建索引，只建議用在小目錄或測試）。"""
    return Directory(people, departments).search(utterance, top_k)


# --------------------------------------------------------------------------- #
# 索引：先用「首音節桶」把候選池縮小，再逐筆打分
# --------------------------------------------------------------------------- #
class Directory:
    """電話簿索引。5,000 人以內用暴力打分也夠快，但首音節桶幾乎零成本、可再快一個量級。"""

    def __init__(self, people: list[Person], departments: list[Department]):
        self.people = people
        self.departments = departments
        self._by_pid = {p.pid: p for p in people}
        # 名冊裡出現過的姓：`decide()` 用「首字是不是姓」判斷「這句像不像在找人」，
        # 免得「名冊查不到的名字」被當成垃圾句直接掛線。
        self.surnames = {p.surname_key for p in people if p.surname_key}
        # 「只講名」分支用：首字不是姓 → 只認 alias；查無但像某人的名 → 轉總機
        self.surname_firsts = {s[0] for s in self.surnames if s}
        self._alias_pids: dict[str, set[str]] = {}
        self._given_keys: set[str] = set()
        for p in people:
            for k in p.alias_keys:
                self._alias_pids.setdefault(k, set()).add(p.pid)
            if p.given_key:
                self._given_keys |= name_match_keys(p.given_key)
        self._buckets: dict[str, set[str]] = {}
        for p in people:
            for v in p.fuzzy_variants:
                if v:
                    self._buckets.setdefault(v[0], set()).add(p.pid)
            # 英文別名也建桶（用首字母）
            for a in p.aliases:
                key = normalize(a).lower()[:1]
                if key:
                    self._buckets.setdefault("#" + key, set()).add(p.pid)

    def person(self, pid: str) -> Person | None:
        """依 `pid` 取回 `Person`；找不到回 None。

        給「第二輪沿用第一輪候選」用：AGI 無狀態，候選只能靠 dialplan 的通道變數帶回來。
        """
        return self._by_pid.get(pid)

    def _pool(self, q_name: str) -> list[Person]:
        keys = set()
        for v in pinyin_variants(q_name):
            if v:
                keys.add(fuzzy_syllable(v[0]))
        keys |= {"#" + c for c in q_name.lower()[:1]}
        ids: set[str] = set()
        for k in keys:
            ids |= self._buckets.get(k, set())
        # 桶太小 / 查詢太短時寧可全掃，避免漏人
        if len(ids) < 3:
            return self.people
        return [self._by_pid[i] for i in ids]

    def search_meta(
        self, utterance: str, top_k: int = 20
    ) -> tuple[list[Candidate], Department | None, str, dict]:
        """`search()` 加上分支資訊：`{"alias": bool, "given_like": bool}`。

        `alias`＝這次是 alias 命中（候選都帶 `alias` 標記）；
        `given_like`＝無姓氏、查無 alias，但發音像名冊裡某人的名 → 決策層轉總機。
        """
        meta: dict = {"alias": False, "given_like": False}
        kwargs = dict(
            alias_pids=self._alias_pids,
            surname_firsts=self.surname_firsts,
            given_keys=self._given_keys,
        )
        cands, dept, q = _search(
            utterance, self.people, self.departments, top_k,
            pool=self._pool, meta=meta, **kwargs,
        )
        # 信心不足時補一次全掃：桶只用來加速「有信心」的多數情況，不能讓它造成漏人。
        # alias 分支是權威判定，不做補掃（否則會把「梅林」撿回林志明）。
        if not meta["alias"] and (not cands or cands[0].score < 0.75):
            full_meta: dict = {"alias": False, "given_like": False}
            full, dept2, q2 = _search(
                utterance, self.people, self.departments, top_k,
                meta=full_meta, **kwargs,
            )
            if not cands or (full and full[0].score > cands[0].score):
                return full, dept2, q2, full_meta
        return cands, dept, q, meta

    def search(self, utterance: str, top_k: int = 20) -> tuple[list[Candidate], Department | None, str]:
        cands, dept, q, _meta = self.search_meta(utterance, top_k)
        return cands, dept, q

    def search_trace(self, utterance: str, top_k: int = 20) -> dict:
        """`search()` 的除錯版：回傳兩條路徑（首音節桶／全掃）的中間值與最終採用哪一條。

        刻意重用同一組 `_search` / `score_breakdown`，不另外實作評分，
        否則 trace 印出來的數字會跟正式流程漂移。
        """
        bucket: dict = {}
        meta: dict = {"alias": False, "given_like": False}
        kwargs = dict(
            alias_pids=self._alias_pids,
            surname_firsts=self.surname_firsts,
            given_keys=self._given_keys,
        )
        cands, dept, q = _search(
            utterance, self.people, self.departments, top_k,
            pool=self._pool, _explain=bucket, meta=meta, **kwargs,
        )
        out = {
            "utterance": utterance,
            "chosen": "bucket",
            "cands": cands,
            "dept": dept,
            "q_name": q,
            "bucket": bucket,
            "meta": meta,
            "full": None,
        }
        # 與 search() 相同的補掃條件（alias 分支不做補掃）
        if not meta["alias"] and (not cands or cands[0].score < 0.75):
            full: dict = {}
            f_meta: dict = {"alias": False, "given_like": False}
            f_cands, f_dept, f_q = _search(
                utterance, self.people, self.departments, top_k,
                _explain=full, meta=f_meta, **kwargs,
            )
            out["full"] = full
            if not cands or (f_cands and f_cands[0].score > cands[0].score):
                out.update(chosen="full", cands=f_cands, dept=f_dept, q_name=f_q, meta=f_meta)
        return out


# --------------------------------------------------------------------------- #
# 載入
# --------------------------------------------------------------------------- #
def same_pinyin(a: Person, b: Person) -> bool:
    """同音：無聲調拼音完全相同（王/汪、張/章）。"""
    ka = {" ".join(v) for v in a.variants}
    kb = {" ".join(v) for v in b.variants}
    return bool(ka & kb)


def homophone_groups(people: list[Person]) -> dict[str, set[str]]:
    """同音群：回傳 pid → 同群 pid 集合（含自己）。

    群組的定義就是 `same_pinyin()`：任一種拼音組合有交集即同群，並具遞移性。
    存在理由：`eval.py` 原本自己用 `p.variants[0]` 分群，那是「同音」定義的第二份
    實作——只比第一種讀音，遇到只在第二種讀音相同的破音字就會分錯群。
    """
    parent: dict[str, str] = {p.pid: p.pid for p in people}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    owner: dict[str, str] = {}      # 拼音鍵 → 第一個持有者的 pid
    for p in people:
        for v in p.variants:
            key = " ".join(v)
            if key in owner:
                union(owner[key], p.pid)
            else:
                owner[key] = p.pid

    groups: dict[str, set[str]] = {}
    for p in people:
        groups.setdefault(find(p.pid), set()).add(p.pid)
    return {pid: groups[root] for root, members in groups.items() for pid in members}


def confusable(a: Person, b: Person) -> bool:
    """同音或近音（模糊音合併後相同，如 林/凌、陳/程 的 n/ng、捲舌/不捲舌）。"""
    if same_pinyin(a, b):
        return True
    ka = {" ".join(v) for v in a.fuzzy_variants}
    kb = {" ".join(v) for v in b.fuzzy_variants}
    return bool(ka & kb)


def same_surname_homophone(a: Person, b: Person) -> bool:
    """同姓、名同音、但漢字不同（李佳蓉 / 李佳容、偉 / 瑋）。

    這是 STT 唯一會犯的錯：它把音訊映射成字元，同音字就是同一個音。
    這種對手的「分數低」純粹來自漢字字面項（0.56 vs 0.82），拼音項完全一樣，
    所以**不能用分數門檻把它篩掉**——門檻篩掉的是「字面不同」，而那正是不可信的部份。
    """
    if not a.surname_key or a.surname_key != b.surname_key:
        return False
    if a.name_key == b.name_key:
        return False
    ga = {tuple(v) for v in a.given_variants}
    gb = {tuple(v) for v in b.given_variants}
    return bool(ga.intersection(gb))


def needs_clarification(
    cands: list[Candidate],
    margin: float = 0.06,
    confident: float = CONFIDENT_SCORE,
    homophone_floor: float = HOMOPHONE_FLOOR,
) -> tuple[bool, str]:
    """要不要再問一句？回傳 (bool, 原因)。

    判斷順序（2026-11-09 修正，順序本身就是設計）：
      1. 沒有候選
      2. **同音兄弟**：第二名與第一名同音/近音（且夠格），或同姓＋名同音不同字
      3. 最高分不到 confident（低信心）
      4. 第二名咬得太近（非低分時的拉鋸）

    為什麼同音檢查必須排在 low-score 之前：`王偉` 字面完全命中（1.00），但名冊裡同時有
    `汪偉`（0.85）——單看 margin 不會警示，可是轉錯人的代價更高。原本的順序把
    `top.score < confident` 放在最前面，於是**所有 top1 < 0.92 的查詢都不會被檢查同音**，
    直接掉進 low-score 交給模型猜（實測 `佳蓉` 0.817 vs `李佳容` 0.56 就被猜掉了）。
    """
    if not cands:
        return True, "no-candidate"
    top = cands[0]
    second = cands[1] if len(cands) > 1 else None
    if second is not None:
        if second.score >= homophone_floor and confusable(top.person, second.person):
            return True, "homophone-sibling"
        if same_surname_homophone(top.person, second.person):
            return True, "homophone-sibling"
    if top.score < confident:
        return True, "low-score"
    if second is not None and top.score - second.score < margin:
        return True, "close-race"
    return False, "confident"


def load_directory(path: str | Path, source_directory: str | None = None) -> "Directory":
    """載入名冊並做一次解析／驗證（執行期不再比中文字串）。

    - `source_directory`（或 JSON 頂層同名欄位）必填；每位 `person.directory` 必須等於它。
    - `department.dept_id` 必須是 `^[a-z][a-z0-9_]*$` 且唯一。
    - `person.dept` 空字串合法（`dept_id=""`）；非空但對不到部門 → 載入錯誤。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    source = source_directory or data.get("source_directory")
    if not source:
        raise ValueError("source_directory 未設定：無法確認名冊來源目錄")

    departments = [Department(**row) for row in data["departments"]]
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for d in departments:
        if not DEPT_ID_RE.fullmatch(d.dept_id or ""):
            raise ValueError(f"department {d.name!r}: dept_id {d.dept_id!r} 不合 ^[a-z][a-z0-9_]*$")
        if d.dept_id in seen_ids:
            raise ValueError(f"department dept_id 重複：{d.dept_id!r}")
        seen_ids.add(d.dept_id)
        d.name = normalize(d.name)
        if not d.name:
            raise ValueError(f"department {d.dept_id!r}: name 不可為空")
        if d.name in seen_names:
            raise ValueError(f"department name 重複：{d.name!r}")
        seen_names.add(d.name)

    alias_owner: dict[str, str] = {}
    for d in departments:
        for alias in (d.name, *d.aliases):
            key = normalize(alias)
            if not key:
                continue
            prev = alias_owner.get(key)
            if prev is not None and prev != d.dept_id:
                raise ValueError(
                    f"部門 alias {alias!r} 同時屬於 {prev!r} 與 {d.dept_id!r}"
                )
            alias_owner[key] = d.dept_id

    people: list[Person] = []
    seen_pids: set[str] = set()
    for row in data["people"]:
        if row.get("directory") != source:
            raise ValueError(
                f"person {row.get('pid')!r}: directory {row.get('directory')!r} "
                f"不符 source_directory {source!r}"
            )
        if not row.get("extension"):
            raise ValueError(f"person {row.get('pid')!r}: extension 不可為空")
        p = build_person(Person(**row))
        if p.pid in seen_pids:
            raise ValueError(f"person pid 重複：{p.pid!r}")
        seen_pids.add(p.pid)
        p.dept_id, p.dept_name = _resolve_department(p.dept, departments, p.pid)
        people.append(p)
    return Directory(people, departments)


def _resolve_department(
    value: str, departments: list[Department], pid: str
) -> tuple[str, str]:
    """把目錄的 department 自由文字解析成 (dept_id, 正規顯示名)；空字串合法。"""
    raw = normalize(value)
    if not raw:
        return "", ""
    raw = raw.replace("部門", "").replace("單位", "")
    if not raw:
        raise ValueError(f"person {pid!r}: dept {value!r} 只有『部門／單位』，無法解析")
    for d in departments:
        for alias in (d.name, *d.aliases):
            if normalize(alias) == raw:
                return d.dept_id, d.name
    raise ValueError(f"person {pid!r}: dept {value!r} 對不到任何部門的 name/aliases")
