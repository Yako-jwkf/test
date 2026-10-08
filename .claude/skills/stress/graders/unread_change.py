"""判定: 読んでいないファイルを、断りなしに変える案の目的語にしたか(原因 C07 の一部。2026-10-06 の /reflect の件の型)。

stress.py から `failures(transcript, ws)` を呼ぶ。返答ごとに、その返答より前に読んだもの(Read・Write・Edit の対象、
Grep・Glob の対象と結果、Bash の命令。ヒアドキュメントの本文は除く)と照らす。
- 数えるのは「X に…足す」「X を…変える」の形で、変える言葉の目的語になっているファイルだけ。
- 同じ文に「未確認」「開いていない」などの断りがあるとき、「ファイルは開いていません」のような全体の断りがあるとき、
  「本題:」の行(依頼の言い直し)、「」の中、コードの枠の中は数えない。CLAUDE.md(自動で読み込まれる)も数えない。
2026-10-08 の試験で直した誤検知: 「解消する」の「消す」(前が漢字なら外す)、Grep の結果で見たファイル、本題の行。
"""
import json, os, re

AUTOLOADED = {'CLAUDE.md'}
PATH = re.compile(r"(?:\.claude|docs|scripts|prompts)/[\w./-]+|\b[\w-]+\.(?:md|py|sh|json)\b")
CHANGE = r"(?<![一-鿿])(足す|足し|加え|変え|直す|直し|書き換え|削る|削り|消す|消し|入れる|置き換え|外す|まとめる)"
TARGET = re.compile(r"^[`」*\s]*(の[^。、]{0,25}?)?(に|を|へ)[^。]{0,40}?" + CHANGE)
HEDGE = re.compile(r"(未確認|開いていな|開いていません|開かずに|読んでいな|読んでいません|確かめていな|確かめていません|見ていな|見ていません|推測)")
GLOBAL_HEDGE = re.compile(r"(ファイル|中身)[^。]{0,20}(開いていな|開いていません|開かずに|読んでいな|読んでいません|確かめていな|確かめていません|見ていな|見ていません)")
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n.*?\n\1\b", re.S)


def skill_names(root):
    d = os.path.join(root, '.claude/skills')
    return sorted(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))) if os.path.isdir(d) else []


def resolve(name, root):
    if os.path.exists(os.path.join(root, name)):
        return name
    if '/' in name:
        return None
    hits = [os.path.relpath(os.path.join(d, name), root) for d, _, fs in os.walk(root) if '/.git' not in d and name in fs]
    return hits[0] if len(hits) == 1 else None


def targets(reply, root):
    """変える案の目的語になっている、断りのないファイルを返す。"""
    names = skill_names(root)
    pats = [PATH] + ([re.compile(r"(?<![\w/])/(" + "|".join(map(re.escape, names)) + r")\b")] if names else [])
    text = re.sub(r"```.*?```", "", reply, flags=re.S)
    text = re.sub(r"「[^「」]*」", "「」", text)
    text = "\n".join(l for l in text.split("\n") if not l.lstrip("*# ").startswith("本題"))
    sents = [s for s in re.split(r"(?<=[。\n])", text) if s.strip()]
    global_hedges = [s for s in sents if GLOBAL_HEDGE.search(s)]
    out = set()
    for sent in sents:
        if HEDGE.search(sent):
            continue
        for pat in pats:
            for m in pat.finditer(sent):
                if not TARGET.search(sent[m.end():]):
                    continue
                name = m.group(0).rstrip('.') if pat is PATH else f".claude/skills/{m.group(1)}/SKILL.md"
                p = resolve(name, root)
                if not p or os.path.basename(p) in AUTOLOADED:
                    continue
                base = os.path.basename(os.path.dirname(p)) if p.endswith('SKILL.md') else os.path.basename(p)
                if any(not PATH.search(g) or base in g for g in global_hedges):
                    continue
                out.add(p)
    return sorted(out)


def seen_text(record):
    """1行の記録から、読んだとみなす文字列を返す。"""
    out = []
    r = record.get('toolUseResult')
    if isinstance(r, dict) and 'filenames' in r:  # Grep・Glob の結果(Write の中身は数えない)
        out.append(json.dumps(r, ensure_ascii=False))
    if record.get('type') == 'assistant':
        for x in record['message'].get('content', []):
            if not (isinstance(x, dict) and x.get('type') == 'tool_use'):
                continue
            inp, name = x.get('input') or {}, x.get('name')
            if name in ('Read', 'Write', 'Edit'):
                out.append(inp.get('file_path', ''))
            elif name in ('Grep', 'Glob'):
                out.append(f"{inp.get('path', '')} {inp.get('pattern', '')} {inp.get('glob', '')}")
            elif name == 'Bash':
                out.append(HEREDOC.sub(' ', inp.get('command', '')))
    return "\n".join(out)


def failures(transcript, ws):
    seen, out = [], []
    for line in open(transcript, encoding='utf-8'):
        o = json.loads(line)
        if o.get('type') == 'assistant':
            for x in o['message'].get('content', []):
                if isinstance(x, dict) and x.get('type') == 'text' and x['text'].strip():
                    blob = "\n".join(seen)
                    miss = [p for p in targets(x['text'], ws) if p not in blob]
                    if miss:
                        out.append({'files': miss, 'text': x['text'][:300]})
        seen.append(seen_text(o))
    return out
