#!/usr/bin/env python3
"""Stop フック(試す仕組みの候補。2026-10-08 の v6): 変える案の目的語になっているリポジトリのファイルを、この会話で一度も読んでいなければ止める。

- 数えるのは「X に…足す」「X を…変える」の形で、変える言葉の目的語になっているファイルだけ(道具として名前を出しただけの文は除く)。
- 同じ文に「未確認」「開いていない」などの断りがあるとき、または「ファイルは開いていません」のような全体の断りがあるときは通す。
- 「読んだ」= Read の file_path、Grep/Glob の path、Bash の命令(ヒアドキュメントの本文は除く)、Write/Edit の file_path に、そのファイルの相対パスがあること。
- STRESS_MODE=sham のときは、同じ場面で止めるが、中身のない文だけを返す(偽の対照)。
"""
import json, os, re, sys, time

MODE = os.environ.get('STRESS_MODE', 'real')
AUTOLOADED = {'CLAUDE.md'}
PATH = re.compile(r"(?:\.claude|docs|scripts|prompts)/[\w./-]+|\b[\w-]+\.(?:md|py|sh|json)\b")
CHANGE = r"(?<![一-鿿])(足す|足し|加え|変え|直す|直し|書き換え|削る|削り|消す|消し|入れる|置き換え|外す|まとめる)"
# ファイル名の直後から、変える言葉までの形(「の○○に」「を」などをはさんでよい)
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
    """変える案の目的語になっているファイルを、(相対パス, 文) の組で返す。断りのあるものは除く。"""
    names = skill_names(root)
    pats = [PATH] + ([re.compile(r"(?<![\w/])/(" + "|".join(map(re.escape, names)) + r")\b")] if names else [])
    text = re.sub(r"```.*?```", "", reply, flags=re.S)
    text = re.sub(r"「[^「」]*」", "「」", text)
    text = "\n".join(l for l in text.split("\n") if not l.lstrip("*# ").startswith("本題"))
    sents = [s for s in re.split(r"(?<=[。\n])", text) if s.strip()]
    global_hedges = [s for s in sents if GLOBAL_HEDGE.search(s)]
    out = []
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
                # 全体の断り: ファイル名を挙げていない断りはすべてに、挙げている断りはそのファイルにだけ効く
                if any(not PATH.search(g) or base in g for g in global_hedges):
                    continue
                out.append(p)
    return sorted(set(out))


def read_blob(path):
    out = []
    try:
        f = open(path, encoding='utf-8')
    except OSError:
        return ''
    for line in f:
        if '"tool_result"' in line and ('"Grep"' in line or '"Glob"' in line or 'toolUseResult' in line):
            try:
                o = json.loads(line)
                r = o.get('toolUseResult')
                if isinstance(r, dict) and 'filenames' in r:  # Grep・Glob の結果だけ(Write の中身は数えない)
                    out.append(json.dumps(r, ensure_ascii=False))
            except ValueError:
                pass
            continue
        if '"tool_use"' not in line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get('type') != 'assistant':
            continue
        for x in o['message'].get('content', []):
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


def check(reply, transcript, root):
    cands = targets(reply, root)
    if not cands:
        return []
    miss = [p for p in cands if p not in read_blob(transcript)]
    if miss:  # 記録は非同期に書かれるので、1回だけ待って読み直す(公式ドキュメント hooks の transcript_path の注意)
        time.sleep(0.3)
        blob = read_blob(transcript)
        miss = [p for p in miss if p not in blob]
    return miss


def main():
    d = json.load(sys.stdin)
    if d.get('stop_hook_active'):
        return
    root = os.environ.get('CLAUDE_PROJECT_DIR') or d.get('cwd') or '.'
    miss = check(d.get('last_assistant_message') or '', d.get('transcript_path') or '', root)
    if not miss:
        return
    if MODE == 'sham':
        reason = "点検: 終える前に、返答を見直してください。"
    else:
        reason = ("確認の点検: 変える案の対象にした次のファイルを、この会話で読んでいません: " + "、".join(miss[:8])
                  + "。読んでから案を出し直すか、その文に「未確認(読んでいない)」と書いてください。")
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


if __name__ == '__main__':
    main()
