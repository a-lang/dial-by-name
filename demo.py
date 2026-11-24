# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dialname.core import load_directory
from dialname.decision import decide

directory = load_directory(ROOT / "data" / "people.json")
people, depts = directory.people, directory.departments

# 逐字稿刻意用「簡體」—— 真實 STT（Whisper / Scribe / Azure）大多輸出簡體，
# 繁體只會出現在少數引擎或人工輸入。這裡順便驗證 core.normalize() 的簡→繁。
cases = [
    (["我要找业务部的王先生"], 1),
    (["我要找业务部的王先生", "业务部的王伟"], 2),
    (["章家豪"], 1),
    (["转业务部"], 1),
    (["我想找Amy"], 1),
    (["帮我接人资"], 1),
    (["找一个很奇怪的人"], 1),
    (["找一个很奇怪的人", "就那个谁"], 2),
    (["章家豪", "章家豪"], 2),
]
for utts, attempt in cases:
    cands, dept, q = directory.search(utts[-1])
    d = decide(utts, cands, dept, attempt=attempt, departments=depts,
               dept_residual=q, surnames=directory.surnames)
    print("─"*78)
    print("UTT:", utts, "| attempt", attempt, "| dept:", dept.name if dept else None)
    print(f"  top3: " + ", ".join(f"{c.person.name}({c.score})" for c in cands[:3]))
    print(f"  => {d.action:10s} target={d.target} label={d.label} reason={d.reason} conf={d.confidence}")
    if d.speech: print("     speech:", d.speech)
    if d.options: print("     options:", d.options)
