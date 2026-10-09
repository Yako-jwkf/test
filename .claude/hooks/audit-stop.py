#!/usr/bin/env python3
"""④監査ゲート(Stop フック)。返答を終える前に、文字で確かめられる点検だけを行う。

いま調べるのは2つ。(1) 人についての一般化(「人間は/人間も/人は…」)を、「私の意見」などの断りなしに断定していないか。(2) 「しかない」「作れない」などの言い切りに、仮定や「推測」の断りがあるか。
- 2026-10-01 の再テスト(事例2・条件5)で、ルール「意見には『私の意見』と明記する」があっても 0/3 だったため、ルールではなく仕組みで止める。
- 「」の中と「>」の引用行(相手の文の引用)は調べない。見出しに「私の意見」があれば、その節の中は断りがあるとみなす。
- 見つけたら終了を止めて、該当する文を返す。止めるのは1回だけ(stop_hook_active が true なら何もしない)。
"""
import json, re, sys

# (e) 強い言い切り。広い語(できない・一番・すべて等)は過去の答えの半分で反応したため、狭い語だけにした(2026-10-02)。
STRONG = re.compile(r"(作れない|作れません|しかない|しかありません|ほかにない|絶対に|間違いなく)")
HEDGE = re.compile(r"(仮定|私の意見|推測|未確認|確かめていません|かもしれ|と思|と考え|見込み|なら|場合|報告|試して確かめ|確かめた)")
# 「人は」「人も」は、前が漢字なら外す(「本人は」「個人は」「100万人も」で止めた誤検知、2026-10-06)。
# 漢字・カタカナの名詞のすぐ後の数字(「著者2人は」「参加者25人は」)も、特定の人を数えた形なので外す(2026-10-09)。
# 「2人に1人は」のような割合は、数字の前が名詞ではないので止めたまま。
SUBJECT = re.compile(r"(人間|(?<![一-鿿])(?<![一-鿿ァ-ヶ][0-9０-９])(?<![一-鿿ァ-ヶ][0-9０-９]{2})人[はも]|人々|誰もが|多くの人|普通の人)")
# 「報告」は「〜と報告」「△の報告」の形だけを断りとみなす。語があるだけで断りとみなしたため、作るきっかけの
# 元の例文「人間も知識の大半は報告で持っている」を止めていなかった(2026-10-06、scripts/mistake_check.py で発見)。
LABEL = re.compile(r"(私の意見|推測|と思います|と考えます|かもしれません|と報告|の報告|未確認|確かめていません)")


def strip_quotes(s):
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"「[^「」]*」", "「」", s)
    return s


def findings(text, subject=None, label=None):
    subject = subject or SUBJECT
    label = label or LABEL
    out, section_ok, in_code = [], False, False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.lstrip().startswith("#"):
            section_ok = bool(label.search(line))
            continue
        if section_ok or line.lstrip().startswith(">"):
            continue
        # 引用の中に「。」があると文の区切りで引用が切れるので、先に行全体から引用を取り除く
        for sent in re.split(r"(?<=[。!?！？])", strip_quotes(line)):
            if subject.search(sent) and not label.search(sent):
                s = sent.strip().lstrip("-*0123456789. |").strip()
                if s:
                    out.append(s[:120])
    return out


def reason_for(hits):
    return ("監査ゲート: 人についての一般化を、断りなしに断定している文があります。"
            "根拠を示すか、「私の意見」「推測」と書くか、削ってください。"
            "相手の文の引用なら「」で囲んでください。\n" + "\n".join(f"- {h}" for h in hits[:8]))


def main():
    data = json.load(sys.stdin)
    if data.get("stop_hook_active"):
        return
    text = data.get("last_assistant_message") or ""
    parts = []
    hits = findings(text)
    if hits:
        parts.append(reason_for(hits))
    strong = findings(text, STRONG, HEDGE)
    if strong:
        parts.append("監査ゲート: 「しかない」「作れない」のような言い切りに、依存している仮定が書かれていません。"
                     "置いた仮定を書くか、「推測」と書くか、確かめた事実ならそう書いてください。\n"
                     + "\n".join(f"- {h}" for h in strong[:8]))
    if parts:
        print(json.dumps({"decision": "block", "reason": "\n\n".join(parts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
