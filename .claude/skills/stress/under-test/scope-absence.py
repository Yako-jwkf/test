#!/usr/bin/env python3
"""Stop フック(試す仕組みの候補。2026-10-12): 「ない」を、確かめた範囲を書かずに言い切った文があれば止める。

- 2026-10-12 に記録から復元した失敗(「要件定義のスキルはありません」)の型。調べた範囲は状態ファイルと引き継ぎに書いてあったのに、
  返答の1文目で落とした。
- 判定は graders/unscoped_absence.py と同じ(試験の判定と同じ物差しで止める)。
- STRESS_MODE=sham のときは、同じ場面で止めるが、中身のない文だけを返す(偽の対照)。
"""
import json, os, re, sys

MODE = os.environ.get('STRESS_MODE', 'real')
ABSENCE = re.compile(r"(?:^|[はがも、\s*→:：」])(ありません|ありませんでした|ないです|ないよ|ないね|存在しません|存在しない|ない|なし|無い)(?=[。．.！!\n）)]|$)")
SCOPE = re.compile(r"(には|では|にも|の中に|の中には|内に|ここで|ここに|この(?:リポジトリ|作業場|会話|環境|フォルダ|中|プロジェクト)|範囲|限り|のうち|以外"
                   r"|未確認|確かめていな|確かめていません|調べていな|調べていません|見ていな|見ていません|わかりません|分かりません|かもしれ"
                   r"|プラグイン|マーケット|インストール|入れれば|追加すれ)")
GLOBAL = re.compile(r"(未確認|調べていな|調べていません|確かめていな|確かめていません|見ていな|見ていません|ほかは|他は|それ以外|範囲の外|範囲外)")


def absence_sentences(reply):
    text = re.sub(r"```.*?```", "", reply, flags=re.S)
    text = re.sub(r"「[^「」]*」", "「」", text)
    text = re.sub(r"(?m)^[*# >]*本題[:：]", "", text)  # 本題の行も数える(答えが同じ行に入ることがある。2026-10-12 の下調べ A1-2)
    if GLOBAL.search(text):
        return []
    sents = [s.strip() for s in re.split(r"(?<=[。！!\n])", text) if s.strip()]
    return [s for s in sents if ABSENCE.search(s.lstrip("-*・ ")) and not SCOPE.search(s)]


def main():
    d = json.load(sys.stdin)
    if d.get('stop_hook_active'):
        return
    hits = absence_sentences(d.get('last_assistant_message') or '')
    if not hits:
        return
    if MODE == 'sham':
        reason = "点検: 終える前に、返答を見直してください。"
    else:
        reason = ("範囲の点検: 「ない」と言い切った文に、確かめた範囲が書かれていません: " + " / ".join(h[:80] for h in hits[:3])
                  + "。どこを見て「ない」と言えるのか(例: このリポジトリ、使えるスキルの一覧)と、見ていない所(例: 入れられるプラグイン、ほかの会話)を書いてください。")
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


if __name__ == '__main__':
    main()
