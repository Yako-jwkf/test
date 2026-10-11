#!/usr/bin/env python3
"""PreToolUse フック: permissions の deny だけでは防げない書き換えを止める(補助の層)。

1. Bash: 保護ファイル(settings.json の deny にある Edit(/...))を、読む以外のコマンドで扱ったら止める。
   deny は sed・tee・リダイレクトなどは止めるが、python や cp のように自分でファイルを開くものは止めない。
2. Edit / Write: 記録ファイル(APPEND_ONLY)は、既存の文を消したり変えたりする編集を止める。追記・挿入と、
   Git の衝突の目印(<<<<<<< ======= >>>>>>>)の行だけを消す編集は通す。
3. Bash: 作業中の変更を戻せない形で消す git 操作(reset --hard など)は、確認画面を出す(ask)。

止めるときは終了コード 2 で終わり、理由を stderr に書く(Claude に届く)。確認画面は JSON の permissionDecision で出す。
限界: 文字で判断するため、cd で移動してから相対パスで書く、変数でパスを組み立てる、手順をファイルに書いてから
実行する、などはすり抜ける(2026-10-02 に実際に起きた)。確実に止めるには sandbox が要る(docs/knowledge/control-plane-v3.md)。

誤検知の直し(2026-10-10、next-steps 17・27、原因 C16):
- ヒアドキュメントの本文は、その行のコマンドがどれも本文を実行しないもの(cat・git commit など)で、終わりの印の行が
  あるときだけ調べない。python・bash などに渡す本文や、パイプでほかのコマンドに流す本文は、今までどおり1行ずつ調べる。
- リダイレクトは、書き先が保護ファイルのときだけ「書く」と見る。保護ファイルを読んで保護外へ書くのは通す。
- git の -C <パス>・-c <設定> を読み飛ばしてから、サブコマンドを決める(reset --hard などの確認画面にも効く)。
- python のヒアドキュメントや -c の本文に保護ファイル名があれば、読むだけでも止める(設計どおり。文字では読むか書くかを
  見分けられない)。
"""
import json
import os
import re
import shlex
import sys

APPEND_ONLY = ["docs/knowledge/retest-log.md"]

READ_ONLY_COMMANDS = {
    "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "wc",
    "ls", "stat", "file", "diff", "cmp", "sha256sum", "md5sum", "jq", "echo", "printf", "true",
}
READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "grep", "ls-files", "add", "commit"}
# 先頭にあっても命令ではない語。for の行は語の並びなので、読む扱いにする
LEADING_KEYWORDS = {"do", "then", "else", "elif", "while", "until", "if", "!", "time", "{", "("}
NON_COMMAND = {"for", "select", "case", "done", "fi", "esac", "}", ")", "in"}
CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})( .*)?$")
SEPARATOR = re.compile(r"^[;&|]+$")
SAFE_REDIRECT_TARGETS = {"/dev/null", "1", "2"}
# ヒアドキュメントの本文を実行しないコマンド。行のコマンドがすべてこの中なら、本文をコマンドとして調べない
HEREDOC_DATA_COMMANDS = READ_ONLY_COMMANDS | {"tee", "cd", "mkdir"}
HEREDOC_DATA_GIT = {"commit", "tag", "notes"}
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def project_dir():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def protected_paths(root):
    """settings.json の deny から Edit(/...) を取り出し、パスの前方一致用の文字列にする。"""
    try:
        with open(os.path.join(root, ".claude", "settings.json"), encoding="utf-8") as f:
            deny = json.load(f).get("permissions", {}).get("deny", [])
    except (OSError, ValueError):
        return []
    paths = []
    for rule in deny:
        m = re.fullmatch(r"Edit\(/(.+)\)", rule)
        if m:
            paths.append(re.sub(r"\*\*?$", "", m.group(1)))
    return paths


def relpath(path, root):
    return os.path.relpath(os.path.normpath(os.path.join(root, path)), root)


def block(reason):
    print(f"guard.py が止めました: {reason}", file=sys.stderr)
    sys.exit(2)


def ask(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                             "permissionDecisionReason": f"guard.py: {reason}"}}, ensure_ascii=False))
    sys.exit(0)


def split_line(line):
    """1行を ; && || | で区切る。引用符の中の記号は区切りにしない。"""
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return [line.split()]
    out, cur = [], []
    for t in tokens:
        if SEPARATOR.match(t):
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    out.append(cur)
    return [s for s in out if s]


def git_sub(words):
    """git の大域オプション(-C <パス> など)を読み飛ばし、(サブコマンド, 残りの語) を返す。"""
    i = 1
    while i < len(words):
        w = words[i]
        if w in GIT_OPTIONS_WITH_VALUE:
            i += 2
        elif w.startswith("-"):
            i += 1
        else:
            return w, words[i + 1:]
    return "", []


def body_is_data(header):
    """ヒアドキュメントの行のコマンドが、どれも本文を実行しないか。"""
    for words in split_line(header):
        words = strip_keywords(words)
        if not words:
            continue
        cmd = os.path.basename(words[0])
        if cmd == "git":
            if git_sub(words)[0] not in HEREDOC_DATA_GIT:
                return False
        elif cmd not in HEREDOC_DATA_COMMANDS:
            return False
    return True


def segments(command):
    """コマンドを ; && || | と改行で区切る。引用符の中の記号は区切りにしない。

    本文を実行しないコマンドのヒアドキュメントは、本文の行を飛ばす(本文は文書で、コマンドではない)。
    """
    out = []
    lines = command.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        out.extend(split_line(line))
        docs = HEREDOC.findall(line) if "<<<" not in line else []
        i += 1
        if docs and body_is_data(line):
            for dash, _, word in docs:
                # 終わりの印の行が見つかったときだけ飛ばす(引用符の中の "<<EOF" で後ろの行を全部飛ばさないため)
                end = next((j for j in range(i, len(lines))
                            if (lines[j].lstrip("\t") if dash else lines[j]) == word), None)
                if end is None:
                    break
                i = end + 1
    return out


def redirect_targets(words):
    """書き込みのリダイレクトの書き先。& での付け替えと /dev/null などは除く。"""
    out = []
    for i, w in enumerate(words):
        if w.startswith(">") or w in ("&>",):
            target = words[i + 1] if i + 1 < len(words) else ""
            if w.endswith("&") or target in SAFE_REDIRECT_TARGETS:
                continue
            out.append(target)
    return out


def without_redirects(words):
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
            continue
        if w.startswith(">") or w in ("&>",):
            skip = not w.endswith("&")
            continue
        out.append(w)
    return out


def strip_keywords(words):
    while words and words[0] in LEADING_KEYWORDS:
        words = words[1:]
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", words[0]):
        words = words[1:]
    return words


def is_read_only(words, targets):
    if "tee" in words or any(t in r for r in redirect_targets(words) for t in targets):
        return False
    words = strip_keywords(without_redirects(words))
    if not words or words[0] in NON_COMMAND:
        return True
    cmd = os.path.basename(words[0])
    if cmd in ("python", "python3", "bash", "sh") and len(words) >= 2 and not words[1].startswith("-"):
        # 保護されたスクリプトを「実行する」のは書き換えではない。ただし他の引数に保護パスがあれば止める。
        return not any(t in w for w in words[2:] for t in targets)
    if cmd == "git":
        return git_sub(words)[0] in READ_ONLY_GIT
    if cmd == "sed":
        return "-n" in words and not any(w.startswith("-i") for w in words)
    if cmd == "find":
        return not any(w in ("-delete", "-exec", "-execdir", "-ok", "-fprint") for w in words)
    return cmd in READ_ONLY_COMMANDS


def destructive_git(words):
    words = strip_keywords(words)
    if not words or os.path.basename(words[0]) != "git":
        return None
    sub, rest = git_sub(words)
    if sub == "reset" and "--hard" in rest:
        return "git reset --hard は作業中の変更を戻せない形で消します"
    if sub in ("checkout", "restore") and ("." in rest or "--" in rest):
        return f"git {sub} はファイルの作業中の変更を消します"
    if sub == "clean":
        return "git clean は追跡していないファイルを消します"
    return None


def check_bash(command, root):
    targets = [t for t in protected_paths(root) + APPEND_ONLY if t]
    reasons = []
    for words in segments(command):
        text = " ".join(words)
        hits = [t for t in targets if t in text]
        if hits and not is_read_only(words, targets):
            block(f"保護ファイル {hits[0]} を、読む以外のコマンドで扱っています: {text[:120]}")
        r = destructive_git(words)
        if r:
            reasons.append(r)
    if reasons:
        ask(reasons[0] + "。記録ファイル(retest-log.md など)の未コミットの追記も消えます")


def only_conflict_markers_removed(old, new):
    kept = [l for l in old.split("\n") if not CONFLICT_MARKER.match(l)]
    return len(kept) < len(old.split("\n")) and new.split("\n") == kept


def check_append_only(tool, tool_input, root):
    path = tool_input.get("file_path") or ""
    if relpath(path, root) not in APPEND_ONLY:
        return
    if tool == "Edit":
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if tool_input.get("replace_all") or not old:
            block(f"{path} は追記だけできます。replace_all は使えません。")
        if new.startswith(old) or new.endswith(old) or only_conflict_markers_removed(old, new):
            return
        block(f"{path} は追記だけできます。既存の文を残したまま、前か後ろに足してください。")
    elif tool == "Write":
        try:
            with open(os.path.join(root, path), encoding="utf-8") as f:
                current = f.read()
        except OSError:
            return
        content = tool_input.get("content", "")
        if not (content.startswith(current) or content.endswith(current)):
            block(f"{path} は追記だけできます。Write で書き直さず、Edit で足してください。")


def main():
    data = json.load(sys.stdin)
    root = project_dir()
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}
    if tool == "Bash":
        check_bash(tool_input.get("command", ""), root)
    elif tool in ("Edit", "Write"):
        check_append_only(tool, tool_input, root)
    sys.exit(0)


if __name__ == "__main__":
    main()
