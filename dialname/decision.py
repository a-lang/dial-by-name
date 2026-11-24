# SPDX-License-Identifier: AGPL-3.0-or-later
"""dialname.decision — 本地決策引擎：候選 → route / ask / fallback

比對層（core）把拼音空間收斂成少數候選；這裡只決定「這一輪該轉、該問、還是該收手」。
**全本地、無外部模型存取**：2026-11-21 起外部模型委派機制整個移除（理由與重開條件見
`docs/design.md` §外部模型）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from .core import (
    CONFIDENT_SCORE,
    HOMOPHONE_FLOOR,
    Candidate,
    Department,
    Person,
    confusable,
    has_routing_cue,
    is_given_name_query,
    looks_incomplete_name,
    needs_clarification,
    same_surname_homophone,
)

# 「只有一個候選」不等於「知道他是誰」：實機 8 通裡 0.48 的陌生人也被直接轉出去過
# （2026-11-16 美玲→梅林）。alias 政策只擋掉「只講名」，含姓氏的低分查詢仍走得到 (c2)，
# 2026-11-17 驗收又出現「小米」0.51 → 王小明，所以在這裡設下限。
# 342 題的 16 筆 single-candidate 最低是 0.8027，0.70 不會動到任何既有指標。
SINGLE_CANDIDATE_FLOOR = 0.70

# 所有「有找人意圖但收斂不了」的結局都先播這一句，再交給 dialplan 播 to-operator。
# 沒有它，來電者只會聽到「正在為您轉接總機」，不知道自己為什麼被轉走。
# 服務故障（未預期例外／AGISTATUS）不套用——那不是「找不到對象」。
FALLBACK_SPEECH = ["sounds/zh/not-found"]


@dataclass
class Decision:
    action: Literal["route", "route_dept", "ask", "fallback", "hangup"]
    target: str | None = None          # extension / department dept_id
    label: str | None = None           # 給 log / TTS 用的名稱
    reason: str = ""
    speech: list[str] = field(default_factory=list)   # 要播放的音檔／片段
    confidence: float | None = None
    # 按鍵選項：[(按鍵, 目的地, 給 log 的名稱)]。只用來「選擇系統已知的語意」
    # （部門、總機），不用來盲選人 —— 沒有名字的「按 1 / 按 2」對來電者毫無意義。
    # 放在最後，避免既有以位置傳參的 Decision(...) 被移位。
    options: list[tuple[str, str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 2. 澄清話術（避免執行期 TTS）
# --------------------------------------------------------------------------- #
CLIP_DIR = "sounds/zh"
CLIP_HASH_ALGO = "sha1"
CLIP_HASH_FIELDS = ("pid", "name", "dept")   # 分隔符 '|'，UTF-8，截前 8 個 hex
CLIP_HASH_LEN = 8
CLIP_MANIFEST_NAME = "manifest.json"


def clip_hash(p: Person) -> str:
    """內容指紋：sha1(pid|name|dept) 前 8 個 hex。

    刻意**不含** voice／版本：換聲音庫時檔名不變，AGI 端不需要快取失效處理，
    重生成就覆蓋同一個檔（見 README §7）。姓名或部門一改，hash 就變 → 換檔，
    不會播到舊部門。
    """
    raw = "|".join((p.pid, p.name, p.dept_name or p.dept))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:CLIP_HASH_LEN]


def name_clip(p: Person) -> str:
    """索引建置時就預先產生的音檔路徑（ElevenLabs TTS 離線生成）。

    命名 = `name-{pid}-{hash8}.wav`：pid 給人看（log／稽核），hash 負責失效判斷。
    **不要**把姓名或拼音放進檔名 —— `ls sounds/zh/` 就等於印出全公司名單。
    """
    return f"{CLIP_DIR}/name-{p.pid}-{clip_hash(p)}.wav"      # 內容：「王偉，業務部」


def name_manifest(people: list[Person], voice_id: str | None = None) -> dict:
    """`sounds/zh/manifest.json` 的內容：檔名 → 中繼資料。

    存在的理由：hash 不可逆，沒有這份表就無法回答「這個檔是誰的」「誰還沒生成」
    「某人的姓名被合成成哪些檔」。**含姓名，屬敏感資產**：與 wav 同權限、
    不進版控、不對外 serve（見 README §7）。

    `algorithm` / `fields` / `truncate` 也寫進檔案，讓 manifest 自己能解釋
    雜湊怎麼算出來的（換人接手不必回頭讀程式碼）。
    """
    clips: dict[str, dict] = {}
    for p in people:
        clips[name_clip(p).rsplit("/", 1)[-1]] = {
            "pid": p.pid,
            "name": p.name,
            "dept": p.dept_name or p.dept,
            "extension": p.extension,
            "text": f"{p.name}，{p.dept_name or p.dept}" if p.dept else p.name,
            "voice_id": voice_id,
            "generated_at": None,      # 生成時由 TTS 批次寫入（ISO 8601）
        }
    return {
        "version": 1,
        "hash": {
            "algorithm": CLIP_HASH_ALGO,
            "input": "|".join(CLIP_HASH_FIELDS),
            "encoding": "utf-8",
            "truncate": CLIP_HASH_LEN,
        },
        "naming": f"name-{{pid}}-{{hash}}.wav",
        "clips": clips,
    }


def plausible_options(cands: list[Candidate], max_options: int = 2, band: float = 0.80) -> list[Candidate]:
    """只把「真的可能是同一個人」的候選放進選擇句。

    否則「財務部的張先生」（章家豪 0.45 / 曾雅雯 0.34）會問成
    「是章家豪還是曾雅雯？」—— 曾雅雯只是同部門的雜訊。
    """
    if not cands:
        return []
    top = cands[0]
    picks = [top]
    for c in cands[1:]:
        if len(picks) >= max_options:
            break
        if c.score < top.score * band:
            continue
        same_surname = c.person.name[:1] == top.person.name[:1]
        if same_surname or confusable(top.person, c.person):
            picks.append(c)
    return picks


def clarify_script(
    cands: list[Candidate],
    max_options: int = 2,
    disclose_names: bool = False,
) -> list[str]:
    """澄清話術。

    **預設不播報姓名。** 電話簿是「任何人撥得到就能查」的服務，只要系統開口念名字，
    就等於提供一個列舉介面：來電者換幾個姓氏輪流問，就能把全公司名單蒐集完
    （`我要找王先生` → 系統說「是王偉還是王小明嗎？」→ 兩個名字都洩漏了）。

    因此預設改成「請說全名」：不確認、不否認、不列舉，把資訊補足的責任交回來電者。
    只有在內線專用、且管理層同意的部署才開 `disclose_names=True`（原本的行為）。
    """
    if not disclose_names:
        return ["sounds/zh/q-say-full-name"]

    picks = plausible_options(cands, max_options)
    if not picks:
        return ["sounds/zh/q-say-full-name"]
    script = ["sounds/zh/q-is-it"]
    for i, c in enumerate(picks):
        if i:
            script.append("sounds/zh/q-or")
        script.append(name_clip(c.person))
    script.append("sounds/zh/q-question")
    return script


# --------------------------------------------------------------------------- #
# 3. 決策
# --------------------------------------------------------------------------- #
OPERATOR_DEST = "operator"   # 僅 log／選項用；實際落點在 dialplan


def clarify_plan(
    cands: list[Candidate],
    dept: Department | None,
    departments: list[Department] | None = None,
    disclose_names: bool = False,
    homophone_floor: float = HOMOPHONE_FLOOR,
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """澄清的「話術 + 按鍵逃生口」。

    原則：**按鍵只能選「系統已知的語意」（部門／總機），不能盲選人。**
    沒有名字的「按 1 / 按 2」對來電者毫無意義 —— 他如果分得出是哪一位，
    根本不需要澄清；分不出來，就更不可能知道 1 和 2 是誰。

    四種情況：
      0. 第一名與「同音兄弟」分屬不同部門 → 問部門（即使那個人只拿到 0.78、
         低於 plausible 的 band，他仍是**聽錯字的可能來源**，不能默默丟掉）
      1. 來電者沒說部門、候選又跨部門 → 問部門（部門名不是個資）
      2. 有部門（來電者說的，或候選全在同一個部門）→ 逃生口是「按 0 轉該部門」
      3. 其餘 → 請說全名 + 「按 0 轉總機」

    例外：候選 ≥2 且全部是 alias 命中時，**全部候選都算數**，不做 `plausible_options`
    的姓名相似度過濾。小名命中本身就是指名道姓，沒有「同部門雜訊」可言；若照一般
    路徑過濾，何俊賢（人資）／宋芷賢（資訊）共用「小安」這種全名不同音、不同姓的組合
    會被濾到剩 1 人 → 誤問「請說全名」，而來電者打小名就是不知道全名
    （2026-11-20 實撥回歸，違反 2026-11-16「alias 同音群一律追問部門」定案）。
    """
    if disclose_names:
        return clarify_script(cands, disclose_names=True), []

    # 0) 同音兄弟跨部門：來電者說的「王偉」，名冊裡「汪偉」在別的部門 →
    #    問部門比問全名有用（他已經講了全名，再問一次只會覺得系統壞了）。
    if dept is None and cands:
        top = cands[0]
        rival_depts = {
            c.person.dept_id
            for c in cands[1:]
            if (c.score >= homophone_floor and confusable(top.person, c.person))
            or same_surname_homophone(top.person, c.person)
        }
        if len(rival_depts | {top.person.dept_id}) > 1:
            return ["sounds/zh/q-which-dept"], [("0", OPERATOR_DEST, "總機")]

    # alias 同音群：小名命中即指名道姓，全部候選都算數（見 docstring 的例外說明）
    if len(cands) >= 2 and all("alias" in c.reasons for c in cands):
        plausible = cands
    else:
        plausible = plausible_options(cands) or cands[:1]
    dept_names = {c.person.dept_id for c in plausible}

    # 1) 沒有部門線索、候選跨部門 → 先問部門
    if dept is None and len(dept_names) > 1:
        return ["sounds/zh/q-which-dept"], [("0", OPERATOR_DEST, "總機")]

    # 2) 找得到部門（來電者說的，或候選只有一個部門）→ 逃生口 = 該部門
    target_dept = dept
    if target_dept is None and len(dept_names) == 1 and departments:
        want = next(iter(dept_names))
        target_dept = next((d for d in departments if d.dept_id == want), None)

    if target_dept is not None:
        return (
            ["sounds/zh/q-say-full-name"],
            [("0", target_dept.dept_id, target_dept.name)],
        )

    # 3) 什麼都沒有 → 請說全名 + 按 0 轉總機
    return ["sounds/zh/q-say-full-name"], [("0", OPERATOR_DEST, "總機")]


def homophone_only(cands: list[Candidate], homophone_floor: float = HOMOPHONE_FLOOR) -> bool:
    """澄清還有意義嗎？—— 若可能的人**全在同一個部門、且彼此同音／近音**，就沒有。

    這是 STT 的物理限制：來電者再講一次「王偉」，8 kHz 窄頻逐字稿還是可能給「汪偉」。
    部門相同 → 問部門沒用；彼此同音 → 問全名也沒用。兩者同時成立時，
    任何「再問一次」都只是讓來電者覺得系統壞了。
    """
    if not cands:
        return False
    top = cands[0]
    # 對手的判定要和 needs_clarification 完全一致（否則會出現「判定要問、又判定問了沒用」）：
    #   ① 同音／近音且過 homophone_floor；或 ② 同姓＋名同音不同字（不受分數門檻限制）。
    # 不要用 plausible_options 的 band——0.56 這種分數會被 band 0.8 排除，於是誤判成還問得動。
    rivals = [
        c for c in cands[1:]
        if (c.score >= homophone_floor and confusable(top.person, c.person))
        or same_surname_homophone(top.person, c.person)
    ]
    if not rivals:
        return False                      # 沒有同音對手 → 問全名是有效的
    if any(c.person.dept_id != top.person.dept_id for c in rivals):
        return False                      # 同音對手在別的部門 → 問部門就能解
    return True


def decide(
    utterances: list[str],
    cands: list[Candidate],
    dept: Department | None,
    attempt: int = 1,
    homophone_floor: float = HOMOPHONE_FLOOR,
    disclose_names: bool = False,
    departments: list[Department] | None = None,
    dept_residual: str | None = None,
    surnames: set[str] | None = None,
    name_form: str | None = None,
) -> Decision:
    # 部門名出現在句子裡 ≠ 來電者要轉那個部門。
    # 「我要談業務合作」含「業務」但意圖是談生意 → 不該轉業務部。
    # 只有帶「找／接／轉／分機」這類路由意圖詞，或整句就是部門名時才允許部門路由。
    routing_intent = has_routing_cue(utterances[-1]) if utterances else False
    # 整句就是部門名（去掉部門與虛詞後什麼都不剩）才算明確意圖，例：「業務部」。
    # 注意：`dept_residual` 在沒有偵測到部門時也可能是空字串，所以必須同時要求 dept 存在。
    bare_dept = dept is not None and dept_residual == ""

    # (a0) 第一輪就沒有任何「找人或找部門」的線索 → 掛線，不問第二輪。
    #      有部門詞但無意圖（「我要談業務合作」）也算；attempt >= 2 不套用。
    #      **例外一**：殘留 2–4 字且首字是名冊裡的姓（「王大明」「李佳怡」）——
    #      來電者確實在找人，只是名冊沒有（或 STT 聽歪），要重問而不是掛線。
    #      **例外二（2026-11-19 方案 C）**：部門有解析到（`dept is not None`）——
    #      這句話已帶了「哪個部門」的封閉詞彙線索，不是純亂句。實撥 #7「幫我接業務部」
    #      被聽成「报街业务部」：部門正確、殘留「報街」不在噪音表、0 候選，
    #      舊規則直接掛線，等於把一通合法來電當騷擾電話。改成把部門念回去再問一次，
    #      第二輪仍收斂不了就照既有規則 fallback（`no-candidate-giveup` → 總機）。
    looks_like_name = (
        bool(surnames)
        and dept_residual is not None
        and 2 <= len(dept_residual) <= 4
        and dept_residual[:1] in surnames
    )
    if attempt < 2 and not cands and not routing_intent and not bare_dept \
            and not looks_like_name and name_form != "given":
        if dept is not None:
            return Decision(
                "ask", None, None, "dept-scoped-ask",
                ["sounds/zh/dept-" + dept.dept_id, "sounds/zh/q-dept-who"],
            )
        return Decision("hangup", None, None, "no-intent", ["sounds/zh/goodbye"])

    # (a0b) 只講名／小名，但 alias 沒登記 → **not-found 轉總機**（2026-11-16 定案）。
    #       與亂句的差別：這句發音像名冊裡某人的名，來電者確實在找人，只是沒登記小名。
    #       轉總機而不是掛線——聽錯名字不是來電者的錯，讓人接起來問得出答案。
    if not cands and name_form == "given":
        return Decision("fallback", None, None, "alias-not-found", FALLBACK_SPEECH)

    # (a) 只講部門（有路由意圖詞，或整句就只有部門名）
    if not cands and dept is not None and (routing_intent or bare_dept):
        return Decision("route_dept", dept.dept_id, dept.name, "dept-only",
                        ["sounds/zh/transferring", "sounds/zh/dept-" + dept.dept_id])

    # (b) 沒抓到人（有找人的意圖，或第二輪）
    if not cands:
        if attempt < 2:
            return Decision(
                "ask", None, None, "no-candidate-retry",
                ["sounds/zh/retry"],
                options=[("0", OPERATOR_DEST, "總機")],
            )
        return Decision("fallback", None, None, "no-candidate-giveup",
                        FALLBACK_SPEECH)

    # (b2) 只講姓（含「只有稱謂、剝掉後只剩姓」）→ **一律不轉**，交總機。
    #      2026-11-09 定案：「因為只有姓沒有名，應該直接 fallback」。
    #      這裡不看候選數：部門收斂到 1 人、或該姓在名冊唯一，都不構成「知道他是誰」的證據
    #      ——那只是「名冊裡剛好只有一個這個姓」，而 STT 的同音（張/章、鍾/鐘）本來就會把
    #      不認識的姓對到某個人身上（實測 `我要找工程部的钟先生` 對 `鍾明軒` 只有 0.32）。
    #      追問「請說全名」他也答不出來（他只知道姓），所以直接給總機由人判斷。
    #      例外：Alias 命中（alias 就登記「王伯伯」）分數 0.95 ≥ CONFIDENT_SCORE → 直接轉。
    if dept_residual and len(dept_residual) == 1 and cands[0].score < CONFIDENT_SCORE:
        return Decision("fallback", None, None, "surname-only",
                        FALLBACK_SPEECH)

    # (b3) 疑似漏字（「張豪」對 張家豪）：名字不完整，若還有 ≥2 個候選就不要猜。
    #      只有 1 個候選時交給下面的 single-candidate（收斂了就直接轉）。
    if len(cands) > 1 and looks_incomplete_name(dept_residual or "", cands[0].person):
        return Decision("fallback", None, None, "incomplete-name",
                        FALLBACK_SPEECH)

    # 澄清判定只算一次（b4 與 c 共用；參數不一致會造成「顯示要問、決策不問」）
    ask, why = needs_clarification(cands, homophone_floor=homophone_floor)

    # 澄清的話術與按鍵逃生口只算一次（同一個分支決定兩者）
    plan_speech, plan_options = clarify_plan(
        cands, dept, departments, disclose_names=disclose_names,
        homophone_floor=homophone_floor,
    )

    def exhausted() -> Decision:
        """問過一輪仍收斂不了 → **直接 fallback**，不再問第三次、也不轉部門 queue。

        2026-11-09 定的規則：
          - 候選 ≥2 → 追問部門（`clarify_plan` 的 `q-which-dept`）
          - 第二輪候選仍 ≥2 → fallback（總機／舊 Directory）

        原本這裡會在「已知部門」或「候選全在同部門」時轉該部門 queue；
        改成 fallback 是刻意的：轉進部門 queue 仍有機會被當成「打得進去但没人接」，
        交給總機由人判斷比較不會兩邊都卡住。
        重複問同樣的問題比轉錯更糟，所以 attempt 用完就收手。
        """
        return Decision("fallback", None, None, "clarify-exhausted",
                        FALLBACK_SPEECH)

    def announce(p: Person) -> list[str]:
        """轉接前的播報。不念姓名 —— 來電者已經知道他要找誰，念出來只是讓旁人聽到。"""
        if not disclose_names:
            return ["sounds/zh/transferring"]
        return ["sounds/zh/transferring-name", name_clip(p)]

    # (b5) alias 同音群（≥2 人）：**一律追問部門**（2026-11-16 定案）。
    #      放在 (b4) 前面：alias 的平手是「同一個小名登記在多人身上」，
    #      問部門有鑑別力（兩人通常不同部門），不該套用 homophone-unresolvable 的 fallback。
    if ask and len(cands) >= 2 and all("alias" in c.reasons for c in cands):
        if attempt >= 2:
            return exhausted()
        return Decision(
            "ask", None, None, "alias-ask",
            plan_speech, options=plan_options, confidence=cands[0].score,
        )

    # (b4) 問了也沒用：候選全在同部門且彼此同音／近音 → 直接 fallback。
    #      STT 把「王偉」聽成「汪偉」是 8 kHz 窄頻的物理限制，來電者再講一次全名，
    #      逐字稿還是可能一樣；部門又相同，問部門也沒有鑑別力。
    #      與 (c3) 的差別：(c3) 是跨部門同音 → 問部門有用；這裡是同部門同音 → 無解。
    #      **例外**：來電者只講了名（`美玲`）→ 姓根本還沒聽到，追問全名是「新資訊」，
    #      不是重複同樣的問題，所以要問，不能 fallback。
    if ask and homophone_only(cands, homophone_floor) \
            and not any(is_given_name_query(dept_residual or "", c.person) for c in cands[:2]):
        return Decision("fallback", None, None, "homophone-unresolvable",
                        FALLBACK_SPEECH)

    # (c) 高信心 → 直接轉
    if not ask:
        c = cands[0]
        return Decision(
            "route", c.person.extension, c.person.name, f"direct:{why}",
            announce(c.person), c.score,
        )

    # (c2) 只有一個候選不代表「知道他是誰」——分數太低就還是陌生人的形狀。
    #      下限之上的單一候選沒有「選」的問題，直接轉。
    if len(cands) == 1:
        c = cands[0]
        if c.score < SINGLE_CANDIDATE_FLOOR:
            return Decision("fallback", None, None, "low-confidence-single",
                            FALLBACK_SPEECH, c.score)
        return Decision(
            "route", c.person.extension, c.person.name, "single-candidate",
            announce(c.person), c.score,
        )

    # (c3) 「同音兄弟」一律本地解，不猜。
    #      「王偉／汪偉」在無聲調拼音下完全同形，這是發音層的平手，不是語意問題；
    #      分數再高也解決不了平手，只會變成「有自信地猜一個」。所以這裡直接進澄清
    #      （跨部門時問部門）。
    if why == "homophone-sibling":
        if attempt >= 2:
            return exhausted()
        return Decision(
            "ask", None, None, "homophone-sibling-ask",
            plan_speech, options=plan_options, confidence=cands[0].score,
        )

    # (d) 區：本地收斂不了的多候選拉鋸 → 問一句。
    #      原 Jev 委派點，2026-11-21 徹底移除；重啟議題時全部重新評估。
    if attempt >= 2:
        return exhausted()
    return Decision("ask", None, None, f"ambiguous-ask:{why}",
                    plan_speech, options=plan_options, confidence=cands[0].score)
