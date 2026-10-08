#!/bin/bash
# 会話の開始時の点検(SessionStart フック)。出力は会話の最初に Claude へ渡る。
# 1. main に入っていない claude/* ブランチと、そこで増えたミス置き場の行数を知らせる。
#    並行する会話や、マージ前の PR の学びが次の会話に届かず、同じミスを記録し直した(原因 C19、2026-10-02・10-06)。
# 2. ミスの原因ごとの判定(scripts/mistake_check.py)の要約を出す。
# 3. news(外の情報の号、https://github.com/Yako-jwkf/news)の最新の号の日付と、食い違いの数を出す(2026-10-08)。
#    号は定期実行が2週に1回書く。15日より古ければ、定期実行が止まっているかもしれないと知らせる。
#    news は作業フォルダの中の .news/ に、読むだけの写しとして取る(.gitignore で git の対象外)。
#    はじめは作業フォルダの外(~/.cache)に置いたが、アプリで利用者が開けるのは作業フォルダの中だけで、
#    表示した場所を開けなかった(原因 C18、2026-10-08)。読むときは /news スキルで号を画面に出す。
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

if [ -z "$inner" ]; then
  news=".news"
  if [ -d "$news/.git" ]; then
    timeout 20 git -C "$news" fetch --quiet --depth 1 origin main >/dev/null 2>&1 \
      && git -C "$news" reset --quiet --hard origin/main >/dev/null 2>&1
    ok=$?
  else
    timeout 20 git clone --quiet --depth 1 https://github.com/Yako-jwkf/news "$news" >/dev/null 2>&1
    ok=$?
  fi
  latest=$(ls "$news/issues/" 2>/dev/null | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}\.md$' | sort | tail -1)
  if [ "$ok" -ne 0 ] && [ -z "$latest" ]; then
    echo "- news を取得できませんでした。外の情報の最新の号は未確認です。"
  elif [ -z "$latest" ]; then
    echo "- news にはまだ号がありません(https://github.com/Yako-jwkf/news)。"
  else
    day="${latest%.md}"
    age=$(( ( $(TZ=Asia/Tokyo date +%s) - $(TZ=Asia/Tokyo date -d "$day" +%s) ) / 86400 ))
    diffs=$(awk '/^## 1\. 食い違い/{on=1;next} /^## /{on=0} on && /^- / && !/^- なし$/' "$news/issues/$latest" | wc -l)
    old=""
    [ "$ok" -ne 0 ] && old="(取得に失敗したので、手元の古い写し)"
    echo "- news の最新の号: issues/${latest}(${age}日前、食い違い ${diffs} 件)${old}。読むときは「号を見せて」(/news スキル)"
    if [ "$age" -gt 15 ]; then
      echo "  - 15日より古いので、定期実行が止まっているかもしれません(claude.ai/code/routines で確かめる)。"
    fi
  fi
fi
exit 0
