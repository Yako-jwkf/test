#!/usr/bin/env python3
"""会話の記録(Claude Code の .jsonl)から、利用者の発言と、画面に出た返答の本文を、言い換えずに抜き出す台本。

継続学習の段1(会話の中の振り返り)の部品。docs/knowledge/continual-learning-design.md。

使い方:
  python3 scripts/harvest_extract.py <会話の記録.jsonl> <出力.md>

- 今の会話の記録(~/.claude/projects/<作業フォルダ>/<会話番号>.jsonl。フックには transcript_path で渡る)を読む。
  サーバーからの読み出し(list_events)は使わない。2026-10-11 の試験では、過去の会話4件で読み出しが136回かかった。
- 1ターン = 人の発言(origin が human)から次の人の発言まで。そのあいだの返答の本文(type が text)をつなげる。
  停止フックの差し戻し(「Stop hook feedback」で始まる発言)はターンの区切りにせず、その返事の本文も同じターンに入れて
  「(停止フックへの返事)」と印を付ける。利用者の画面には、直前の返答の続きとして出るため。
- 取らないもの: 道具の呼び出しと結果、思考、スキルの本文などの差し込み(isMeta)、添付。
- 鍵・トークン・メールらしい文字列は伏せる。出力は scratchpad に置き、リポジトリ(公開)には入れない。
"""
import json
import re
import sys

HOOK = "Stop hook feedback"
MASKS = [(re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[メール]"),
         (re.compile(r"(sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})"), "[鍵]"),
         (re.compile(r"(?i)\b(token|secret|password|api[_-]?key)\s*[:=]\s*\S+"), r"\1=[伏せ字]")]


def mask(text):
    for rx, rep in MASKS:
        text = rx.sub(rep, text)
    return text


def prompt_text(entry):
    """人の発言なら本文を、停止フックの差し戻しなら HOOK を、それ以外は None を返す。"""
    if entry.get("type") != "user":
        return None
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        if any(c.get("type") == "tool_result" for c in content):
            return None
        content = "".join(c.get("text", "") for c in content if c.get("type") == "text")
    if not isinstance(content, str):
        return None
    # 停止フックの差し戻しは isMeta: true で記録される(2026-10-11 にこの会話の記録で確かめた)
    if content.startswith(HOOK):
        return HOOK
    if not entry.get("isMeta") and (entry.get("origin") or {}).get("kind") == "human":
        return content
    return None


def turns(path):
    """[{"time", "prompt", "answer": [(種類, 本文)]}] を返す。種類は "本文" か "停止フックへの返事"。"""
    out, kind = [], "本文"
    with open(path, encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    for entry in entries:
        text = prompt_text(entry)
        if text == HOOK:
            kind = "停止フックへの返事"
            continue
        if text is not None:
            out.append({"time": entry.get("timestamp", ""), "prompt": text, "answer": []})
            kind = "本文"
            continue
        if entry.get("type") == "assistant" and out:
            body = "".join(c.get("text", "") for c in entry.get("message", {}).get("content", [])
                           if c.get("type") == "text")
            if body.strip():
                out[-1]["answer"].append((kind, body))
    return out


def render(items):
    lines = []
    for n, t in enumerate(items, 1):
        lines += [f"## ターン{n}({t['time'][:16]} UTC)", "### 利用者", mask(t["prompt"]), "### Claude の返答"]
        for kind, body in t["answer"]:
            lines.append(mask(body) if kind == "本文" else f"(停止フックへの返事)\n{mask(body)}")
        lines.append("")
    return "\n".join(lines)


def main(src, dst):
    items = turns(src)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(render(items))
    hooks = sum(1 for t in items for kind, _ in t["answer"] if kind != "本文")
    print(f"ターン {len(items)}、停止フックへの返事 {hooks}、出力 {dst}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
