#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""stt_batch.py — ElevenLabs STT：冒煙測試（單檔）＋ 離線重播（整批）。

用途
----
1. **冒煙測試**：單檔上傳，驗 key、驗 multipart 格式、驗端點可用——先跑這個，確認通了再撥電話。
2. **離線重播**：同一批 wav 換 keyterms 設定重跑（A6 的 sc／tc／none 三組），輸出分檔不覆蓋。
   30 通電話的即時轉稿由 AGI 流程自己完成，不靠這支程式。

用法
----
音檔目錄與 `truth.csv` 的路徑**自選、不依賴 repo 內的固定目錄**（PoC 用完即丟；
正式開發改用 Realtime STT）。下面是示意：

    uv run stt_batch.py --audio ~/dbn-audio/20261110-143012-u01.wav
    uv run stt_batch.py --audio-dir ~/dbn-audio --truth ~/dbn-truth.csv \\
                        --out out/transcripts_fake.json --fake
    uv run stt_batch.py --audio-dir ~/dbn-audio --truth ~/dbn-truth.csv \\
                        --out data/transcripts/sc.json \\
                        --keyterms-from data/people.json --keyterms-style sc --limit 5

選項
----
    --fake                 不呼叫 API：transcript = truth_text、cer=0.0、lang=null（零成本驗流程）
    --keyterms FILE        一行一個詞（UTF-8）
    --keyterms-from PATH   從名冊生成姓名＋部門名＋別名（去重保序；與 --keyterms 互斥）
    --keyterms-style sc|tc sc 用 core.to_simplified() 轉換（轉換邏輯只在 core，這裡不重寫）
    --limit N              只處理 truth.csv 前 N 筆

輸出契約（A3）
--------------
    [{"file", "transcript", "truth_text", "expect_pid",
      "kind", "dept_expect", "cer", "lang"}]
    cer = rapidfuzz 字元級 Levenshtein 距離 ÷ truth_text 長度（不引入 jiwer）。

錯誤處理沿用 `SttClient`：401/403 與 422 帶診斷訊息直接拋出、不重試；
批次中止時**保留已完成的 JSON**，不讓整批白跑。
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from rapidfuzz.distance import Levenshtein

from dialname.core import load_directory, to_simplified
from dialname.stt_client import SttClient

ROOT = Path(__file__).parent
DEFAULT_OUT = ROOT / "out" / "transcripts.json"

TRUTH_COLUMNS = ("file", "truth_text", "expect_pid", "kind", "dept_expect")


# --------------------------------------------------------------------------- #
# keyterms
# --------------------------------------------------------------------------- #
def _dedupe(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_keyterms_file(path: Path) -> list[str]:
    return _dedupe(path.read_text(encoding="utf-8").splitlines())


def keyterms_from_roster(path: Path, style: str) -> list[str]:
    """名冊 → keyterms：姓名、部門名、別名；`sc` 轉簡體。

    不手抄清單：名冊改了 keyterms 自動跟上（A6 的 sc／tc 兩組就是同一個來源）。
    """
    directory = load_directory(path)
    terms: list[str] = []
    for p in directory.people:
        terms.append(p.name)
        terms.extend(p.aliases)
    for d in directory.departments:
        terms.append(d.name)
        terms.extend(d.aliases)
    if style == "sc":
        terms = [to_simplified(t) for t in terms]
    return _dedupe(terms)


# --------------------------------------------------------------------------- #
# truth.csv 與 CER
# --------------------------------------------------------------------------- #
def read_truth(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:   # utf-8-sig：吃 Excel 存的 BOM
        reader = csv.DictReader(f)
        missing = [c for c in TRUTH_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"truth.csv 缺少欄位：{'、'.join(missing)}（需要 {','.join(TRUTH_COLUMNS)}）")
        return [row for row in reader if (row.get("file") or "").strip()]


def cer(truth: str, hypothesis: str) -> float:
    """字元級 CER；truth 為空時視為無可比較（0.0 或 1.0）。"""
    if not truth:
        return 0.0 if not hypothesis else 1.0
    return Levenshtein.distance(truth, hypothesis) / len(truth)


def missing_audio(rows: list[dict[str, str]], audio_dir: Path) -> list[str]:
    return [r["file"].strip() for r in rows
            if not (audio_dir / r["file"].strip()).is_file()]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_rows(rows: list[dict[str, str]], audio_dir: Path, client: SttClient | None,
               fake: bool, out: list[dict]) -> None:
    """逐筆轉稿並 append 到 `out`：中途失敗時已完成的部分還在，才能落檔保留。"""
    for i, row in enumerate(rows, 1):
        path = audio_dir / row["file"].strip()
        truth = row.get("truth_text") or ""
        if fake:
            text, lang = truth, None
        else:
            tr = client.transcribe(path)
            text, lang = tr.text, tr.language
        item = {
            "file": path.name,
            "transcript": text,
            "truth_text": truth,
            "expect_pid": (row.get("expect_pid") or "").strip() or None,
            "kind": (row.get("kind") or "").strip(),
            "dept_expect": (row.get("dept_expect") or "").strip() or None,
            "cer": round(cer(truth, text), 4),
            "lang": lang,
        }
        out.append(item)
        print(f"  [{i}/{len(rows)}] {item['file']}  cer={item['cer']:.3f}  {item['transcript']}")


def write_rows(out_path: str | Path, rows: list[dict]) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ElevenLabs STT：單檔冒煙測試／批次離線重播")
    ap.add_argument("--audio", help="單檔冒煙測試：印出逐字稿，不寫檔")
    ap.add_argument("--audio-dir", help="wav 目錄（批次模式）")
    ap.add_argument("--truth", help="truth.csv（批次模式必填）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="輸出 JSON（預設 out/transcripts.json）")
    kt = ap.add_mutually_exclusive_group()
    kt.add_argument("--keyterms", metavar="FILE", help="一行一個詞")
    kt.add_argument("--keyterms-from", metavar="PATH", help="從名冊生成 keyterms（如 data/people.json）")
    ap.add_argument("--keyterms-style", choices=("sc", "tc"), default="tc",
                    help="名冊詞彙樣式：sc 轉簡體、tc 保留繁體（預設 tc，只在 --keyterms-from 時有意義）")
    ap.add_argument("--fake", action="store_true", help="不呼叫 API：transcript = truth_text、cer=0.0、lang=null")
    ap.add_argument("--limit", type=int, help="只處理前 N 筆")
    args = ap.parse_args(argv)

    if args.audio and (args.audio_dir or args.truth):
        ap.error("--audio 是單檔模式，不能與 --audio-dir／--truth 併用")
    if not args.audio and not (args.audio_dir and args.truth):
        ap.error("批次模式需要 --audio-dir 與 --truth（單檔請用 --audio）")
    if args.keyterms_style != "tc" and not args.keyterms_from:
        ap.error("--keyterms-style 只在 --keyterms-from 時有意義")
    if args.limit is not None and args.limit <= 0:
        ap.error("--limit 必須 > 0")

    if args.keyterms:
        keyterms = load_keyterms_file(Path(args.keyterms))
    elif args.keyterms_from:
        keyterms = keyterms_from_roster(Path(args.keyterms_from), args.keyterms_style)
    else:
        keyterms = []

    if args.audio:
        if args.fake:
            ap.error("--fake 需要 truth_text 才有意義；單檔模式請直接打真 API")
        client = SttClient(keyterms=keyterms)
        if not client.api_key:
            raise SystemExit("ELEVENLABS_API_KEY 未設定：請先設定環境變數（不要寫進檔案）")
        tr = client.transcribe(Path(args.audio))
        print(json.dumps({"file": Path(args.audio).name, "transcript": tr.text,
                          "lang": tr.language}, ensure_ascii=False))
        return 0

    audio_dir = Path(args.audio_dir)
    rows = read_truth(Path(args.truth))
    if args.limit:
        rows = rows[: args.limit]
    missing = missing_audio(rows, audio_dir)
    if missing:
        raise SystemExit(f"truth.csv 指到不存在的 wav：{'、'.join(missing)}")

    client: SttClient | None = None
    if not args.fake:
        client = SttClient(keyterms=keyterms)
        if not client.api_key:
            raise SystemExit("ELEVENLABS_API_KEY 未設定：請先設定環境變數，或改用 --fake")

    print(f"audio-dir={audio_dir}  truth={args.truth}  "
          f"模式={'fake' if args.fake else 'live'}  keyterms={len(keyterms)} 筆數={len(rows)}")
    done: list[dict] = []
    try:
        build_rows(rows, audio_dir, client, args.fake, done)
    except Exception as exc:      # 不重試；保留已完成的部分，讓下次只補沒跑到的
        path = write_rows(args.out, done)
        raise SystemExit(f"STT 失敗，已保留 {len(done)} 筆於 {path}："
                         f"{type(exc).__name__}: {exc}")

    path = write_rows(args.out, done)
    print(f"wrote {path}（{len(done)} 筆）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
