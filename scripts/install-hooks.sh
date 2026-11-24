#!/usr/bin/env bash
# 安裝 .git/hooks/pre-commit：動到評分／決策／名冊時先跑 L0＋L1 回歸。
#
# 為什麼是 pre-commit 而不是文件裡的「請記得跑」：golden 過期 6 天沒人發現，
# 紅燈沒有強制力就等於沒有（`docs/design.md` §回歸三層）。
#
# 用法：bash scripts/install-hooks.sh（可重複執行，會覆寫既有 hook）
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
hook="$root/.git/hooks/pre-commit"

mkdir -p "$root/.git/hooks"

cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
# 由 scripts/install-hooks.sh 產生；直接改這裡會在下次安裝時被覆寫。
set -u

root="$(git rev-parse --show-toplevel)" || exit 0
cd "$root" || exit 0

staged="$(git diff --cached --name-only --diff-filter=ACMR)" || exit 0
[ -z "$staged" ] && exit 0

# 只有會影響評分／決策／名冊的變更才需要跑回歸
watch=0
while IFS= read -r path; do
  case "$path" in
    dialname/*.py | eval.py | data/people.json) watch=1 ;;
  esac
done <<< "$staged"
[ "$watch" -eq 1 ] || exit 0

# 測試檔還不存在時放行（分階段落地；目前 L1 尚未建立）
tests=()
for t in tests/test_eval_golden.py tests/test_call_script.py; do
  [ -f "$t" ] && tests+=("$t")
done
if [ "${#tests[@]}" -eq 0 ]; then
  echo "pre-commit: L0/L1 測試檔尚未建立，跳過回歸。"
  exit 0
fi

echo "pre-commit: 偵測到評分／決策／名冊變更，跑 ${tests[*]}"
if ! uv run pytest -q "${tests[@]}"; then
  echo
  echo "pre-commit 擋下 commit：L0/L1 回歸失敗（先看上面的 L0 golden 差異題目）。"
  echo "  刻意改行為 → 依 tests/golden/README.md 與 docs/design.md §回歸三層 更新基準，"
  echo "  並在 CHANGELOG 記一筆；名冊變更另走單獨政策（docs/design.md §名冊來源）。"
  echo "  只是想繞過 → git commit --no-verify（請在 CHANGELOG 記原因）。"
  exit 1
fi
HOOK

chmod +x "$hook"
echo "installed $hook"
