#!/usr/bin/env python3
"""Stop フック(試す仕組みの候補 v2。2026-10-12): 「ない」を、見た範囲で限らず、見ていない所も書かずに言い切った文があれば止める。

- v1(scope-absence.py)は見た場所を並べただけの答えを通し(A1 で見ていない所の読みで 4/5→3/5)、止めた後の答えが長くなった
  (A2 の「一言で」で 3.6倍)。v2 は場所の言葉では通さず、止めたときの文で、範囲を短い句で足させる。
- 判定は graders/unscoped_absence_v2.py と同じ。
- STRESS_MODE=sham のときは、同じ場面で止めるが、中身のない文だけを返す(偽の対照)。
"""
import json, os, re, sys

MODE = os.environ.get('STRESS_MODE', 'real')
# v2(2026-10-12): v1 は「このプロジェクトのスキルにも、一覧にもありません」のように、見た場所を並べただけの答えを通した。
# 元の失敗(10-11)も見た場所を2つ挙げていたので、場所の言葉だけでは通さない。通すのは次の2つだけ。
# (1) 結論そのものを限る形: 「〜には/では/の中には/範囲では/限りでは」のすぐ後の「ない」(例: 見た範囲では、ない)
# (2) 返答のどこかで、見ていない所を書く(未確認・調べていない・見ていない・ほかは・それ以外 など)
ABSENCE = re.compile(r"(?:^|[はがも、\s*→:：」])(ありません|ありませんでした|ないです|ないよ|ないね|存在しません|存在しない|ない|なし|無い)(?=[*]*(?:です)?[*]*[。．.！!\n）)(（]|[*]*$)")
LIMITED = re.compile(r"(には|では|の中に|の中には|の中では|範囲で|範囲では|範囲には|限りでは|限り)[*、\s]*(?:は、?)?[*]*(ありません|ありませんでした|ないです|ないよ|ないね|存在しません|存在しない|ない|なし|無い)")
UNCHECKED = re.compile(r"(未確認|確認していな|確認していません|探していな|探していません|調べていな|調べていません|確かめていな|確かめていません|見ていな|見ていません|ほかは|他は|それ以外|範囲の外|範囲外|わかりません|分かりません)")


def absence_sentences(reply):
    text = re.sub(r"```.*?```", "", reply, flags=re.S)
    text = re.sub(r"「[^「」]*」", "「」", text)
    text = re.sub(r"(?m)^[*# >]*本題[:：]", "", text)
    if UNCHECKED.search(text):
        return []
    sents = [s.strip() for s in re.split(r"(?<=[。！!\n])", text) if s.strip()]
    out = []
    for s in sents:
        body = s.lstrip("-*・ ")
        if ABSENCE.search(body) and not LIMITED.search(body):
            out.append(s)
    return out


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
        reason = ("範囲の点検: 「ない」と言い切った文に、見た範囲が書かれていません: " + " / ".join(h[:60] for h in hits[:2])
                  + "。結論の文に見た範囲を短く足して答え直してください(例: 「見た範囲では、ない(プラグインは未確認)」)。"
                  "依頼が短い答えを求めているなら、短いままにしてください。")
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


if __name__ == '__main__':
    main()
