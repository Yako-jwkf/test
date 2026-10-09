#!/usr/bin/env python3
"""ミスの原因ごとに、解消したかを判定する台本(擬似解消関数の実行と集計)。

- 置き場 docs/knowledge/mistake-inbox.md(出来事)と、原因の表 docs/knowledge/mistake-causes.md を読む。
- 原因ごとに、対策日より後の出来事(再発)を数え、擬似解消関数を動かし、解消の状態を決める。
  状態の決め方は mistake-causes.md の「解消の状態の決め方」の表と同じ。
- 「対策を置いたか」(置き場の「対策」の列)と「直ったか」(この台本の判定)を分けるために作った(2026-10-06)。

使い方:
  python3 scripts/mistake_check.py            判定結果を表示する
  python3 scripts/mistake_check.py --write    原因の表の「判定結果」を書き換える
  python3 scripts/mistake_check.py --root DIR リポジトリの写し(例: フックを直した試しの版)を調べる

読むだけ。フックは一時フォルダに写して動かし、本物の状態ファイル・置き場には書かない(--write の判定結果を除く)。
/retest は流さない(費用がかかる)。再テストの結果は原因の表の「最後の再テスト」から読む。
終了コード: 置き場の行に原因がない・表にない番号がある、など書き方の誤りがあれば 1。未解消があっても 0。
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from fractions import Fraction

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = "docs/knowledge/mistake-inbox.md"
CAUSES = "docs/knowledge/mistake-causes.md"
BEGIN, END = "<!-- 判定結果 ここから -->", "<!-- ここまで -->"
FIELDS = ("原因の推測", "対策", "対策日", "擬似解消関数", "最後の再テスト")
NOT_A_MISTAKE, UNCLASSIFIED = "対象外", "未分類"


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ---------- 読み取り ----------

def split_top(text, sep="、"):
    """sep で区切る。ただし括弧の中の sep では区切らない。"""
    out, cur, depth = [], "", 0
    for ch in text:
        if ch in "((":
            depth += 1
        elif ch in "))":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def table_cells(line):
    """Markdown の表の1行を、エスケープした \\| では切らずにセルへ分ける。"""
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def parse_inbox(path):
    events = []
    for line in read_text(path).splitlines():
        if not re.match(r"^\| \d{4}-\d{2}-\d{2} \|", line):
            continue
        cells = table_cells(line)
        causes = split_top(cells[4]) if len(cells) >= 5 and cells[4] else [UNCLASSIFIED]
        events.append({"date": cells[0], "what": cells[1], "found": cells[2],
                       "measure": cells[3] if len(cells) > 3 else "", "causes": causes})
    return events


def parse_causes(path):
    causes, cur = [], None
    for line in read_text(path).splitlines():
        m = re.match(r"^### (C\d+) (.+)$", line.rstrip())
        if m:
            cur = {"id": m.group(1), "name": m.group(2), **{f: "" for f in FIELDS}}
            causes.append(cur)
            continue
        if line.startswith("## "):
            cur = None
        m = re.match(r"^- (" + "|".join(FIELDS) + r"):\s?(.*)$", line.rstrip())
        if cur is not None and m:
            cur[m.group(1)] = m.group(2).strip()
    return causes


# ---------- フックを一時フォルダで動かす ----------

def hook_commands(root, event):
    try:
        settings = json.loads(read_text(os.path.join(root, ".claude", "settings.json")))
    except (OSError, ValueError):
        return []
    cmds = []
    for group in settings.get("hooks", {}).get(event, []):
        for h in group.get("hooks", []):
            if h.get("type") == "command":
                cmds.append([h["command"]] + list(h.get("args", [])))
    return cmds


def run_hook(cmd, payload, project_dir):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": project_dir}
    if len(cmd) == 1:
        p = subprocess.run(cmd[0], shell=True, input=json.dumps(payload, ensure_ascii=False),
                           capture_output=True, text=True, env=env, cwd=project_dir)
    else:
        argv = [os.path.expandvars(a.replace("${CLAUDE_PROJECT_DIR}", project_dir)) for a in cmd]
        p = subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False),
                           capture_output=True, text=True, env=env, cwd=project_dir)
    blocked = p.returncode == 2
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else {}
        blocked = blocked or out.get("decision") == "block"
    except ValueError:
        pass
    return blocked, (p.stdout + p.stderr).strip()


def simulate_turn(root, prompt, reply, touch_state=True, touch_inbox=False):
    """利用者の発言 → (状態ファイル・置き場の更新) → 返答の終わり、を一時フォルダで再現し、止めたフックを返す。"""
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(os.path.join(root, ".claude", "hooks"), os.path.join(tmp, ".claude", "hooks"))
        os.makedirs(os.path.join(tmp, ".claude", "reasoning"))
        os.makedirs(os.path.join(tmp, "docs", "knowledge"))
        state = os.path.join(tmp, ".claude", "reasoning", "state.md")
        inbox = os.path.join(tmp, INBOX)
        for path, body in ((state, "## 本題\n- 会話全体の目的: 試験\n"), (inbox, "# 置き場\n")):
            write_text(path, body)
            past = time.time() - 3600
            os.utime(path, (past, past))
        for cmd in hook_commands(root, "UserPromptSubmit"):
            run_hook(cmd, {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": tmp}, tmp)
        later = time.time() + 1
        if touch_state:
            os.utime(state, (later, later))
        if touch_inbox:
            os.utime(inbox, (later, later))
        hits = []
        for cmd in hook_commands(root, "Stop"):
            blocked, out = run_hook(cmd, {"hook_event_name": "Stop", "stop_hook_active": False,
                                          "last_assistant_message": reply, "cwd": tmp}, tmp)
            if blocked:
                hits.append(os.path.basename(cmd[0].split()[-1].strip('"')) + ": " + out[:80])
        return hits


def expect_turns(root, cases):
    """cases: (説明, 発言, 返答, 止まるべきか, 状態ファイルを更新したか, 置き場を更新したか)"""
    misses = []
    for name, prompt, reply, want, ts, ti in cases:
        got = bool(simulate_turn(root, prompt, reply, ts, ti))
        if got != want:
            misses.append(f"{name}(期待: {'止める' if want else '通す'})")
    return not misses, f"{len(cases) - len(misses)}/{len(cases)} 一致" + (("。外れ: " + "、".join(misses)) if misses else "")


# ---------- 擬似解消関数(振る舞い) ----------

def b_audit_opinion(root, cause, events):
    return expect_turns(root, [
        ("人についての一般化", "どう思う？", "人間も知識の大半は報告で持っている。", True, True, False),
        # 数字の前が名詞のときだけ外した(2026-10-09)ので、割合の一般化は止めたままかを見る
        ("割合での一般化", "どう思う？", "2人に1人は説明を読まずに従う。", True, True, False),
    ])


def b_audit_assumption(root, cause, events):
    return expect_turns(root, [
        ("「しかない」の言い切り", "どう渡せばいい？", "サブエージェントには本文として渡すしかない。", True, True, False),
        ("前提を書かない決定", "つまりどうする？", "まとめの方針のまま、if文で作ります。", True, True, False),
    ])


def b_judge_words(root, cause, events):
    return expect_turns(root, [
        ("サブエージェントの報告文", "Another Claude session sent a message: ミスがあります", "受け取りました。", False, True, False),
        ("貼られた文の「ちゃんと」", "この式をちゃんと計算すると？", "計算しました。", False, True, False),
        ("「本人は」の「人は」", "続けて", "本人は「企画書」の箱を選んでいるのに、表示が違う。", False, True, False),
        ("特定の人を数えた「著者2人は」(2026-10-08)", "続けて",
         "著者2人は、この仕組みを作る会社の共同創業者で、論文にそう明記している。", False, True, False),
    ])


def b_inbox_guard(root, cause, events):
    return expect_turns(root, [
        ("指摘のターンで置き場を更新しない", "それは違う、やり直して", "直しました。", True, True, False),
        ("指摘のターンで置き場を更新した", "それは違う、やり直して", "直しました。", False, True, True),
    ])


def b_state_guard(root, cause, events):
    # 短い発言は1行目に原文を入れる(state-guard の (d))ので、返答の1行目に「評価して」を入れておく
    return expect_turns(root, [
        ("「本題:」なのに状態ファイルを更新しない", "評価して", "本題: 試験。今回はその「評価して」の部分\n評価します。", True, False, False),
        ("「本題:」で状態ファイルを更新した", "評価して", "本題: 試験。今回はその「評価して」の部分\n評価します。", False, True, False),
    ])


def b_question_quote(root, cause, events):
    # 2026-10-06 のボスAIの会話の問いを、推測の本題に合わせて1行目で言い換えた形(C02)
    return expect_turns(root, [
        ("問いを言い換えた1行目", "うん、結局どうするの？",
         "本題: 個人制作のボスAIを決める。今回はその前提を確かめる部分\nif文で作ります。", True, True, False),
        ("問いを原文のまま入れた1行目", "うん、結局どうするの？",
         "本題: ボスAIを決める。今回はその「うん、結局どうするの？」の部分\n答え: 3段に分けて作ります。", False, True, False),
    ])


GUARD_CASES = [
    # (説明, ツール, 入力, 期待)  期待: 通す / 止める / 確認画面
    ("for の中の読むだけの git show(2026-10-02)", "Bash",
     {"command": "for f in .claude/hooks/guard.py scripts/check_boundary.py; do git show HEAD:$f | head -3; done"}, "通す"),
    ("引用符の中の \\|(2026-10-02)", "Bash",
     {"command": "grep '^### \\|^#### ' docs/knowledge/retest-log.md"}, "通す"),
    ("衝突の目印だけを消す編集(2026-10-02)", "Edit",
     {"file_path": "docs/knowledge/retest-log.md", "old_string": "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> x", "new_string": "A\nB"}, "通す"),
    ("git reset --hard(2026-10-02)", "Bash", {"command": "git reset --hard"}, "確認画面"),
    ("git commit -F - のヒアドキュメント(2026-10-02)", "Bash",
     {"command": "git commit -F - <<'EOF'\nmsg\n- scripts/check_boundary.py と CI を足す\nEOF"}, "通す"),
    ("cat のヒアドキュメント(PR #9)", "Bash",
     {"command": "cat > /tmp/x/t.sh <<'EOF'\nsee docs/knowledge/retest-log.md\nEOF"}, "通す"),
    ("run.py の出力を保護外へリダイレクト(PR #7)", "Bash",
     {"command": "python3 .claude/skills/retest/run.py plan.md > /tmp/x/run.log"}, "通す"),
    ("git -C で読むだけ(PR #9)", "Bash",
     {"command": "git -C /home/user/test ls-files .claude/agents/judge.md"}, "通す"),
    ("python にヒアドキュメントで書き込み(止まるべき)", "Bash",
     {"command": "python3 - <<'EOF'\nopen('.claude/agents/judge.md', 'w').write('x')\nEOF"}, "止める"),
    ("bash にヒアドキュメントで書き込み(止まるべき)", "Bash",
     {"command": "bash <<'EOF'\necho x > .claude/agents/judge.md\nEOF"}, "止める"),
    ("python -c に保護ファイル名(設計どおり)", "Bash",
     {"command": "python3 -c \"print(open('.claude/agents/judge.md').read())\""}, "止める"),
    ("シェル関数の引数に保護ファイル名(設計どおり、2026-10-06)", "Bash",
     {"command": "run '{\"command\":\"cat .claude/agents/judge.md\"}' label"}, "止める"),
    ("保護ファイルの上書き(止まるべき)", "Bash", {"command": "echo hi > docs/knowledge/retest-log.md"}, "止める"),
]


def b_guard_cases(root, cause, events):
    guard = os.path.join(root, ".claude", "hooks", "guard.py")
    misses = []
    for name, tool, tool_input, want in GUARD_CASES:
        p = subprocess.run([sys.executable, guard], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                           capture_output=True, text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": root}, cwd=root)
        got = "止める" if p.returncode == 2 else ("確認画面" if "permissionDecision" in p.stdout else "通す")
        if got != want:
            misses.append(f"{name}(期待: {want}、実際: {got})")
    n = len(GUARD_CASES)
    return not misses, f"{n - len(misses)}/{n} 一致" + (("。外れ: " + "、".join(misses)) if misses else "")


def b_session_start_hook(root, cause, events):
    cmds = hook_commands(root, "SessionStart")
    if not cmds:
        return False, "SessionStart のフックがない"
    # MISTAKE_CHECK_RUNNING: フックがこの台本を呼び返さないようにする(呼び合って 60 秒止まった、2026-10-06)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": root, "CLAUDE_CODE_REMOTE": "true", "MISTAKE_CHECK_RUNNING": "1"}
    outs = []
    for cmd in cmds:
        p = subprocess.run(cmd[0] if len(cmd) == 1 else cmd, shell=len(cmd) == 1, capture_output=True, text=True,
                           input=json.dumps({"hook_event_name": "SessionStart"}), env=env, cwd=root, timeout=60)
        if p.returncode != 0:
            return False, f"SessionStart のフックが失敗した(終了コード {p.returncode})"
        outs.append(p.stdout)
    if not any("main に入っていない claude/*" in o for o in outs):
        return False, "SessionStart のフックが、main に入っていないブランチの点検を出さない"
    return True, "SessionStart のフックが、main に入っていないブランチの点検を出す(会話に届くかは見ていない)"


def b_heading_rule(root, cause, events):
    since = cause.get("対策日") or "2026-10-06"
    bad = [e["date"] for e in events if e["date"] >= since and re.match(r"^`[^`]+` の誤検知", e["what"])]
    return not bad, f"{since} 以降で、見出しに「誤検知」と書いた行: {len(bad)}"


BEHAVIORS = {
    "audit_opinion": b_audit_opinion,
    "audit_assumption": b_audit_assumption,
    "judge_words": b_judge_words,
    "inbox_guard": b_inbox_guard,
    "state_guard": b_state_guard,
    "question_quote": b_question_quote,
    "guard_cases": b_guard_cases,
    "session_start_hook": b_session_start_hook,
    "heading_rule": b_heading_rule,
}


# ---------- 判定 ----------

def run_check(spec, cause, root, events):
    """1つの擬似解消関数を動かす。戻り値: (種類, 結果 True/False/None, 説明)。None は判定できない。"""
    kind, _, rest = spec.partition(":")
    if spec.startswith("なし"):
        return "なし", None, spec
    if kind == "振る舞い":
        fn = BEHAVIORS.get(rest)
        if not fn:
            return "振る舞い", False, f"台本に関数 {rest} がない"
        ok, detail = fn(root, cause, events)
        return "振る舞い", ok, f"{rest}: {detail}"
    if kind == "再テスト":
        m = re.fullmatch(r"事例(\d+)-(\d+)>=(\d+)/(\d+)", rest)
        if not m:
            return "再テスト", False, f"書き方の誤り: {spec}"
        case, cond, k, n = m.groups()
        r = re.search(rf"事例{case}-{cond}=(\d+)/(\d+)(?:\((\d{{4}}-\d{{2}}-\d{{2}})\))?", cause.get("最後の再テスト", ""))
        if not r:
            return "再テスト", None, f"事例{case}-{cond}: 未測定"
        got = Fraction(int(r.group(1)), int(r.group(2)))
        ok = got >= Fraction(int(k), int(n))
        return "再テスト", ok, f"事例{case}-{cond}: {r.group(1)}/{r.group(2)}(基準 {k}/{n}、{r.group(3) or '日付なし'})"
    if kind == "残存":
        path, _, text = rest.partition(":")
        try:
            ok = text in read_text(os.path.join(root, path))
        except OSError:
            ok = False
        return "残存", ok, f"{path} に「{text}」が{'ある' if ok else 'ない'}"
    return kind, False, f"知らない種類: {spec}"


def judge(cause, events, root):
    mine = [e for e in events if cause["id"] in e["causes"]]
    since = cause.get("対策日", "")
    recur = [e for e in mine if since and e["date"] > since]
    checks = [run_check(s, cause, root, events) for s in split_top(cause.get("擬似解消関数", "")) if s]
    if cause.get("対策", "").startswith("見送り"):
        status = "見送り"
    elif not since:
        status = "未対策"
    elif recur:
        status = "再発あり"
    elif any(ok is False for _, ok, _ in checks):
        status = "不合格"
    elif any(ok for kind, ok, _ in checks if kind in ("振る舞い", "再テスト")):
        status = "合格(暫定)"
    elif any(ok for _, ok, _ in checks):
        status = "合格(残存のみ)"
    else:
        status = "未判定"
    return {"id": cause["id"], "name": cause["name"], "events": len(mine), "recur": recur,
            "checks": checks, "status": status}


def structure_errors(events, causes):
    ids = {c["id"] for c in causes}
    errs = []
    for e in events:
        for c in e["causes"]:
            if c == UNCLASSIFIED:
                errs.append(f"原因が未分類: {e['date']} {e['what'][:40]}")
            elif c != NOT_A_MISTAKE and c not in ids:
                errs.append(f"原因の表にない番号 {c}: {e['date']} {e['what'][:40]}")
    for c in causes:
        if c["対策日"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", c["対策日"]):
            errs.append(f"{c['id']} の対策日の書き方: {c['対策日']}")
    return errs


def render(results, events, errors):
    mark = {True: "合格", False: "不合格", None: "判定なし"}
    lines = ["| 原因 | 出来事 | 対策後の再発 | 擬似解消関数の結果 | 解消の状態 |", "|---|---|---|---|---|"]
    for r in results:
        recur = "、".join(e["date"] for e in r["recur"]) if r["recur"] else "なし"
        checks = "<br>".join(f"{kind} {mark[ok]}: {d}" for kind, ok, d in r["checks"]) or "なし"
        lines.append(f"| {r['id']} {r['name']} | {r['events']} | {recur} | {checks} | **{r['status']}** |")
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    out_of_scope = sum(NOT_A_MISTAKE in e["causes"] for e in events)
    summary = "、".join(f"{k} {v}" for k, v in counts.items())
    head = [f"判定日: {time.strftime('%Y-%m-%d')} / 置き場の行: {len(events)}(うち対象外 {out_of_scope}) / 原因: {len(results)}({summary})"]
    if errors:
        head.append("書き方の誤り: " + " / ".join(errors))
    return "\n".join(head + [""] + lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    events = parse_inbox(os.path.join(args.root, INBOX))
    causes = parse_causes(os.path.join(args.root, CAUSES))
    errors = structure_errors(events, causes)
    results = [judge(c, events, args.root) for c in causes]
    text = render(results, events, errors)
    print(text)
    if args.write:
        path = os.path.join(args.root, CAUSES)
        body = read_text(path)
        if BEGIN not in body or END not in body:
            sys.exit(f"{path} に判定結果の印がありません")
        pre, rest = body.split(BEGIN, 1)
        _, post = rest.split(END, 1)
        write_text(path, pre + BEGIN + "\n" + text + "\n" + END + post)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
