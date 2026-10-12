#!/usr/bin/env python3
"""UserPromptSubmit フック(試す仕組みの候補。2026-10-12): 依頼が「あるか・ないか」を聞いているとき、答える前に、
「ない」は見た範囲で限り、見ていない所を短く書くよう知らせる。

- Stop フックの v1・v2(scope-absence.py・scope-absence-v2.py)は最後の答えを直したが、最初に表示された答えは直らず(5/5)、
  表示が2倍余りになった(C18)。返答の前に効く形なら、最初の答えに作用するはず(仮説)。
- 依頼の判定は文字で行う(「ってある」「ある？」「ない？」「有無」など)。当たらない依頼には何も足さない。
- STRESS_MODE=sham のときは、同じ依頼に中身のない文だけを足す(偽の対照)。
- 標準出力の文は、依頼といっしょに文脈として渡る(UserPromptSubmit の標準出力。試験の記録で届いたかを確かめる)。
"""
import json, os, re, sys

MODE = os.environ.get('STRESS_MODE', 'real')
ASKS_EXISTENCE = re.compile(r"(ってある|ってない|はある|がある|ある[？?]|あるか|ない[？?]|ないよね|ないんだよね|無い|有無|どこにもない|存在する[？?])")


def main():
    d = json.load(sys.stdin)
    prompt = d.get('prompt') or ''
    if not ASKS_EXISTENCE.search(prompt):
        return
    if MODE == 'sham':
        print("(点検: 答える前に、依頼をもう一度読んでください。)")
    else:
        print("(範囲の点検: この依頼は、あるか・ないかを聞いています。「ない」と答えるときは、結論を見た範囲で限り"
              "(例: 「見た範囲では、ない」)、見ていない所(入れられるプラグイン、ほかの会話・ブランチなど)を短く書いてください。"
              "短い答えを求められていたら、短いままにしてください。)")


if __name__ == '__main__':
    main()
