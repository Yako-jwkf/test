#!/bin/bash
# 会話の開始時の点検(SessionStart フック)。出力は会話の最初に Claude へ渡る。
# 1. main に入っていない claude/* ブランチと、そこで増えたミス置き場の行数を知らせる。
#    並行する会話や、マージ前の PR の学びが次の会話に届かず、同じミスを記録し直した(原因 C19、2026-10-02・10-06)。
# 2. ミスの原因ごとの判定(scripts/mistake_check.py)の要約を出す。
# 失敗しても会話は止めない(いつも終了コード 0)。クラウドの会話でだけ動く。
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

echo "【会話開始の点検】(.claude/hooks/session-start.sh)"

# scripts/mistake_check.py の中から試しに動かされたときは、取得と判定の要約を飛ばす
# (このフックが判定の台本を呼び、台本がこのフックを呼ぶ、の繰り返しで 60 秒止まった。2026-10-06)
inner="${MISTAKE_CHECK_RUNNING:-}"

if [ -z "$inner" ] && ! timeout 30 git fetch --quiet origin main '+refs/heads/claude/*:refs/remotes/origin/claude/*' 2>/dev/null; then
  echo "- ブランチを取得できませんでした。main に入っていない学びがあるかは未確認です。"
else
  inbox="docs/knowledge/mistake-inbox.md"
  found=0
  for ref in $(git for-each-ref --format='%(refname:short)' 'refs/remotes/origin/claude/'); do
    if git merge-base --is-ancestor "$ref" origin/main 2>/dev/null; then
      continue
    fi
    rows=$(git diff origin/main..."$ref" -- "$inbox" 2>/dev/null | grep -c '^+| 20' || true)
    when=$(git log -1 --format='%cd' --date=short "$ref")
    if [ "$found" -eq 0 ]; then
      echo "- main に入っていない claude/* ブランチがあります。ここにある学び(ミス置き場の行・ルールの変更)は、この会話の main にはありません。同じ問題を「新しく発見」する前に確かめてください。"
    fi
    found=$((found + 1))
    echo "  - ${ref#origin/}(最後のコミット ${when}、ミス置き場に足した行 ${rows})"
  done
  if [ "$found" -eq 0 ]; then
    echo "- main に入っていない claude/* ブランチはありません。"
  fi
fi

if [ -z "$inner" ] && [ -f scripts/mistake_check.py ]; then
  summary=$(timeout 60 python3 scripts/mistake_check.py 2>/dev/null | head -1)
  if [ -n "$summary" ]; then
    echo "- ミスの原因ごとの判定: ${summary}(詳しくは docs/knowledge/mistake-causes.md、python3 scripts/mistake_check.py)"
  fi
fi
exit 0
