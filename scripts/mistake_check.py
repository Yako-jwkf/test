#!/usr/bin/env python3
"""ミスの原因ごと・対策ごとに、動作と効果を分けて判定する台本(擬似解消関数の実行と集計)。

- 置き場 docs/knowledge/mistake-inbox.md(出来事)と、原因の表 docs/knowledge/mistake-causes.md を読む。
- 原因ごとに、対策日より後の出来事(本番の再発)を数え、擬似解消関数を動かし、原因の状態を決める。
- 原因の表の「対策の一覧」(### Mnn)にある対策ごとに、動作(動くか)と効果(ミスが減ったか)を分けて状態を決め、
  見直し候補を出す。状態の決め方は mistake-causes.md の表と同じ。
- 「対策を置いたか」(置き場の「対策」の列)と「直ったか」を分けるために作った(2026-10-06)。
  さらに「動く」「記録がある」「既知の失敗例に合格」「効いた」を分けた(2026-10-09、改良案 A1・C1)。

使い方:
  python3 scripts/mistake_check.py            判定結果を表示する
  python3 scripts/mistake_check.py --write    原因の表の「判定結果」を書き換える
  python3 scripts/mistake_check.py --root DIR リポジトリの写し(例: フックを直した試しの版)を調べる

読むだけ。フックは一時フォルダに写して動かし、本物の状態ファイル・置き場には書かない(--write の判定結果を除く)。
/retest は流さない(費用がかかる)。再テストの結果は原因の表の「最後の再テスト」「効果の結果」から読む。
終了コード: 置き場の行に原因がない・表にない番号がある、など書き方の誤りがあれば 1。未解消があっても 0。
"""
import argparse
import datetime
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
MEASURE_FIELDS = ("原因", "場所", "導入日", "作用の仮説", "動作確認", "再生", "効果の基準", "効果の結果", "副作用", "撤回日",
                  "決定", "証拠", "費用")
# 決定の欄の書き出し(採否の決定。状態とは別。2026-10-09)
DECISIONS = ("採用", "条件付き採用", "再設計", "撤回", "不採用", "残す", "未決定")
EVAL_KINDS = ("既知", "未知", "通常")
# 導入からこの日数たっても効果が確かめられていない対策は、見直し候補にする(改良案 C1)
REVIEW_DAYS = 14
# 効果の結果に「前の形」と書いた結果は、今の形の対策の効果に数えない(同じ日に対策を直したときに使う)
OLD_FORM = "前の形"
NOT_A_MISTAKE, UNCLASSIFIED = "対象外", "未分類"
DATE = r"\d{4}-\d{2}-\d{2}"


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
        if line.startswith("## ") or line.startswith("### "):
            cur = None
        m = re.match(r"^- (" + "|".join(FIELDS) + r"):\s?(.*)$", line.rstrip())
        if cur is not None and m:
            cur[m.group(1)] = m.group(2).strip()
    return causes


def parse_measures(path):
    """原因の表の「対策の一覧」の ### Mnn の節を読む。"""
    measures, cur = [], None
    for line in read_text(path).splitlines():
        m = re.match(r"^### (M\d+) (.+)$", line.rstrip())
        if m:
            cur = {"id": m.group(1), "name": m.group(2), **{f: "" for f in MEASURE_FIELDS}}
            measures.append(cur)
            continue
        if line.startswith("## ") or line.startswith("### "):
            cur = None
        m = re.match(r"^- (" + "|".join(MEASURE_FIELDS) + r"):\s?(.*)$", line.rstrip())
        if cur is not None and m:
            cur[m.group(1)] = m.group(2).strip()
    return measures


def parse_criteria(text):
    """効果の基準。戻り値: (基準の一覧, 書き方の誤り)

    絶対の形: 既知:事例2-1>=2/3(3回中2回以上)。
    比べる形: 未知:事例8-3>=+2/5(対照より5回中2回以上多い)、通常:事例11-1>=-1/5(対照より2回以上は減らない)。
    対照は効果の結果の括弧に「対照 4/5」と書く(対策を外した版や CLAUDE.md なしの結果)。2026-10-09、事例8-1 が対策なしでも
    5/5 で、絶対の形では効果と課題の易しさを区別できなかったため。
    事例がまだなければ 未知:なし(理由)。
    """
    out, errs = [], []
    for s in split_top(text):
        if s.startswith("なし"):
            continue
        m = re.fullmatch(r"(既知|未知|通常):事例(\d+)-(\d+)>=([+-]?)(\d+)/(\d+)", s)
        n = re.fullmatch(r"(既知|未知|通常):なし.*", s)
        if m:
            sign = {"+": 1, "-": -1}.get(m.group(4))
            need = Fraction(int(m.group(5)), int(m.group(6))) * (sign or 1)
            out.append({"kind": m.group(1), "key": f"事例{m.group(2)}-{m.group(3)}", "rel": sign is not None,
                        "need": need, "spec": s})
        elif n:
            out.append({"kind": n.group(1), "key": None, "need": None, "spec": s})
        else:
            errs.append(f"効果の基準の書き方: {s}")
    return out, errs


def parse_results(text):
    """効果の結果。例: 既知:事例2-1=3/3(2026-10-02)。括弧の中に「前の形」とあれば、今の形の結果に数えない。"""
    out, errs = [], []
    for s in split_top(text):
        if s.startswith("なし"):
            continue
        m = re.fullmatch(r"(既知|未知|通常):事例(\d+)-(\d+)=(\d+)/(\d+)\((" + DATE + r")([^)]*)\)", s)
        if m:
            ref = re.search(r"対照[^0-9]*(\d+)/(\d+)", m.group(7))
            out.append({"kind": m.group(1), "key": f"事例{m.group(2)}-{m.group(3)}",
                        "got": Fraction(int(m.group(4)), int(m.group(5))), "raw": f"{m.group(4)}/{m.group(5)}",
                        "date": m.group(6), "old_form": OLD_FORM in m.group(7),
                        "ref": Fraction(int(ref.group(1)), int(ref.group(2))) if ref else None,
                        "ref_raw": f"{ref.group(1)}/{ref.group(2)}" if ref else None})
        else:
            errs.append(f"効果の結果の書き方: {s}")
    return out, errs


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
    """戻り値: (止めたか, 出力, 落ちたか)。終了コードが 0 と 2 以外なら、フックが落ちたとみなす。"""
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
    return blocked, (p.stdout + p.stderr).strip(), p.returncode not in (0, 2)


def hook_name(cmd):
    return os.path.basename(cmd[-1].split()[-1].strip('"'))


def simulate_turn(root, prompt, reply, touch_state=True, touch_inbox=False, errors=None):
    """利用者の発言 → (状態ファイル・置き場の更新) → 返答の終わり、を一時フォルダで再現し、止めたフックを返す。

    errors に一覧を渡すと、落ちたフックをそこに足す。落ちたフックは止めないので、止めないことを期待する試験が
    「合格」に見える(2026-10-09、文法の誤りで動かない写しのフックで judge_words が 4/4 一致した)。
    """
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
            _, out, failed = run_hook(cmd, {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": tmp}, tmp)
            if failed and errors is not None:
                errors.append(f"{hook_name(cmd)} が落ちた: {out[-120:]}")
        later = time.time() + 1
        if touch_state:
            os.utime(state, (later, later))
        if touch_inbox:
            os.utime(inbox, (later, later))
        hits = []
        for cmd in hook_commands(root, "Stop"):
            blocked, out, failed = run_hook(cmd, {"hook_event_name": "Stop", "stop_hook_active": False,
                                                  "last_assistant_message": reply, "cwd": tmp}, tmp)
            if failed and errors is not None:
                errors.append(f"{hook_name(cmd)} が落ちた: {out[-120:]}")
            if blocked:
                hits.append(hook_name(cmd) + ": " + out[:80])
        return hits


def expect_turns(root, cases):
    """cases: (説明, 発言, 返答, 止まるべきか, 状態ファイルを更新したか, 置き場を更新したか)"""
    misses = []
    for name, prompt, reply, want, ts, ti in cases:
        errors = []
        got = bool(simulate_turn(root, prompt, reply, ts, ti, errors))
        if errors:
            misses.append(f"{name}(フックが落ちた: {errors[0]})")
        elif got != want:
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
    # 動作確認: 設計した語(しかない・作れない)を止めるか。設計の外の文は b_audit_assumption_replay で見る
    return expect_turns(root, [
        ("「しかない」の言い切り", "どう渡せばいい？", "サブエージェントには本文として渡すしかない。", True, True, False),
        ("「作れない」の言い切り", "どう作る？", "①は作れない。", True, True, False),
    ])


def b_audit_assumption_replay(root, cause, events):
    # 再生: C08 の置き場の行に引いた失敗の文(原文の一部)を、言い切りの点検が止めるか。効果ではなく、フック単体の届く範囲
    return expect_turns(root, [
        ("2026-10-01 しかない", "続けて", "本文として渡すしかない。", True, True, False),
        ("2026-10-02 作れない", "続けて", "①は作れない。", True, True, False),
        ("2026-10-02 一番効いた", "続けて", "③が一番効いた。", True, True, False),
        ("2026-10-06 前提を書かない決定", "つまりどうする？", "まとめの方針のまま、if文で作ります。", True, True, False),
        ("2026-10-03 強い前提", "続けて", "v0.3 を直してから。成功の基準がないと良し悪しを判定できない。", True, True, False),
        ("2026-10-08 1回の観察の一般化", "続けて",
         "設定を変えても、いま動いている会話には効きません(2026-10-06 に確かめた記録があります)。", True, True, False),
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
    # 2026-10-10 の直し(ヒアドキュメント・リダイレクト・git -C)で、新しい穴を作っていないかを見る
    ("git show を保護外へリダイレクト(2026-10-09)", "Bash",
     {"command": "git show HEAD:.claude/hooks/guard.py > /tmp/x/guard_copy.py"}, "通す"),
    ("cat のヒアドキュメントを python に流す(止まるべき)", "Bash",
     {"command": "cat <<'EOF' | python3\nopen('.claude/agents/judge.md','w')\nEOF"}, "止める"),
    ("git apply のヒアドキュメント(止まるべき)", "Bash",
     {"command": "git apply <<'EOF'\n+++ b/.claude/agents/judge.md\nEOF"}, "止める"),
    ("終わりの印のない <<EOF の後ろの行(止まるべき)", "Bash",
     {"command": "grep '<<EOF' a.txt\ncp /tmp/x .claude/agents/judge.md"}, "止める"),
    ("ヒアドキュメントの後ろの行(止まるべき)", "Bash",
     {"command": "cat > /tmp/x/t <<'EOF'\nmsg\nEOF\ncp /tmp/x .claude/agents/judge.md"}, "止める"),
    ("保護ファイルへの追記のリダイレクト(止まるべき)", "Bash", {"command": "echo x >> .claude/agents/judge.md"}, "止める"),
    ("git -C つきの reset --hard(確認画面)", "Bash", {"command": "git -C /home/user/test reset --hard"}, "確認画面"),
    ("git -C つきの checkout で保護ファイルを戻す(止まるべき)", "Bash",
     {"command": "git -C /home/user/test checkout -- .claude/agents/judge.md"}, "止める"),
]


def b_guard_cases(root, cause, events):
    guard = os.path.join(root, ".claude", "hooks", "guard.py")
    misses = []
    for name, tool, tool_input, want in GUARD_CASES:
        p = subprocess.run([sys.executable, guard], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                           capture_output=True, text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": root}, cwd=root)
        if p.returncode not in (0, 2):
            got = "落ちた"
        else:
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
    "audit_assumption_replay": b_audit_assumption_replay,
    "judge_words": b_judge_words,
    "inbox_guard": b_inbox_guard,
    "state_guard": b_state_guard,
    "question_quote": b_question_quote,
    "guard_cases": b_guard_cases,
    "session_start_hook": b_session_start_hook,
    "heading_rule": b_heading_rule,
}


# ---------- 原因ごとの判定 ----------

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
        since = cause.get("対策日", "")
        if since and (not r.group(3) or r.group(3) < since):
            # 対策を置く前の版で測った結果は、その対策の判定に使わない(2026-10-09、C04・C06 が 10-02 の結果で判定されていた)
            return "再テスト", None, f"事例{case}-{cond}: {r.group(1)}/{r.group(2)}({r.group(3) or '日付なし'})は対策日 {since} より前で古い"
        got = Fraction(int(r.group(1)), int(r.group(2)))
        ok = got >= Fraction(int(k), int(n))
        return "再テスト", ok, f"事例{case}-{cond}: {r.group(1)}/{r.group(2)}(基準 {k}/{n}、{r.group(3)})"
    if kind == "残存":
        path, _, text = rest.partition(":")
        try:
            ok = text in read_text(os.path.join(root, path))
        except OSError:
            ok = False
        return "残存", ok, f"{path} に「{text}」が{'ある' if ok else 'ない'}"
    return kind, False, f"知らない種類: {spec}"


# ---------- 同じ日の再発(git で、その版に対策があったかを調べる) ----------

def git_out(root, *args):
    try:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def markers_of(specs):
    """擬似解消関数・動作確認のうち、残存:ファイル:文字列 を (ファイル, 文字列) にする。"""
    out = []
    for s in split_top(specs or ""):
        if s.startswith("残存:"):
            path, _, text = s[len("残存:"):].partition(":")
            out.append((path, text))
    return out


def same_day_recurrences(root, events, since, causes, markers, cache):
    """対策を置いた日と同じ日の出来事のうち、対策が入った版で起きた疑いのあるものを返す。

    出来事の行を初めて足したコミットから、main の歴史に入っている一番近いコミット(会話が始まった版とみなす)を探し、
    そこに対策の文(残存の印)がそろっているかを見る。日付だけでは同じ日の前後が分からず、2026-10-02 の C09 の
    「ルールがあっても起きた」行を数えていなかった(2026-10-09)。
    ただし、行を足した会話とミスが起きた会話は同じとは限らない(後の会話での言い換え・取り込み。2026-10-09 に C04・M06 で
    取り違えを確かめた)。そこで、行を足したコミットの日が出来事の日と同じで、行の先頭の【】に「再発」と書いてあるものだけ
    confirmed にし、ほかは疑い(要確認)として返す。git の歴史がない(CI の浅い取得など)・印がないときは何も返さない。
    """
    if not since or not markers:
        return []
    if "main" not in cache:
        ref = next((r for r in ("origin/main", "main") if git_out(root, "rev-parse", "--verify", "-q", r)), None)
        cache["main"] = set(git_out(root, "rev-list", "--first-parent", ref).split("\n")) if ref else set()
    if not cache["main"] or not git_out(root, "rev-parse", "--verify", "-q", "HEAD~1"):
        return []
    out = []
    for e in events:
        if e["date"] != since or not set(e["causes"]) & set(causes):
            continue
        key = "row:" + re.sub(r"^【[^】]*】", "", e["what"])[:25]
        if key not in cache:
            line = git_out(root, "log", "--all", "--reverse", "--format=%H %ad", "--date=short", "-S" + key[4:], "--",
                           INBOX).split("\n")[0]
            first, _, day = line.partition(" ")
            base = next((c for c in git_out(root, "rev-list", "--topo-order", first).split("\n") if c in cache["main"]),
                        None) if first else None
            cache[key] = (base, day)
        base, day = cache[key]
        if base and all(text in git_out(root, "show", f"{base}:{path}") for path, text in markers):
            confirmed = day == e["date"] and bool(re.match(r"^【[^】]*再発[^】]*】", e["what"]))
            out.append({**e, "same_day": True, "confirmed": confirmed})
    return out


# 原因の状態。「動く」「記録がある」「既知の失敗例に合格」は効果ではないので、名前で分ける(2026-10-09、A1)
S_POSTPONED, S_NO_MEASURE, S_RECUR = "見送り", "未対策", "再発あり"
S_BROKEN, S_KNOWN_FAIL = "動作不良", "既知の失敗例で不合格"
S_KNOWN_PASS = "既知の失敗例で合格(未知・通常タスクは未評価)"
S_RUNS_ONLY = "動作のみ確認(効果は未評価)"
S_TEXT_ONLY = "記録のみ確認(効果は未評価)"
S_UNEVALUATED = "効果は未評価"


def judge(cause, events, root, cache=None):
    mine = [e for e in events if cause["id"] in e["causes"]]
    since = cause.get("対策日", "")
    recur = [e for e in mine if since and e["date"] > since]
    same = same_day_recurrences(root, events, since, [cause["id"]], markers_of(cause.get("擬似解消関数")),
                                cache if cache is not None else {})
    recur += [e for e in same if e["confirmed"]]
    possible = [e for e in same if not e["confirmed"]]
    checks = [run_check(s, cause, root, events) for s in split_top(cause.get("擬似解消関数", "")) if s]
    if cause.get("対策", "").startswith("見送り"):
        status = S_POSTPONED
    elif not since:
        status = S_NO_MEASURE
    elif recur:
        status = S_RECUR
    elif any(ok is False for kind, ok, _ in checks if kind != "再テスト"):
        status = S_BROKEN
    elif any(ok is False for kind, ok, _ in checks if kind == "再テスト"):
        status = S_KNOWN_FAIL
    elif any(ok for kind, ok, _ in checks if kind == "再テスト"):
        status = S_KNOWN_PASS
    elif any(ok for kind, ok, _ in checks if kind == "振る舞い"):
        status = S_RUNS_ONLY
    elif any(ok for kind, ok, _ in checks if kind == "残存"):
        status = S_TEXT_ONLY
    else:
        status = S_UNEVALUATED
    return {"id": cause["id"], "name": cause["name"], "events": len(mine), "recur": recur,
            "checks": checks, "status": status, "since": since, "possible": possible}


# ---------- 対策ごとの判定 ----------

M_RETRACTED, M_SIDE, M_INSUFFICIENT = "撤回済み", "副作用あり", "効果不十分"
M_CONFIRMED, M_EVALUATING, M_UNEVALUATED = "効果確認済み(評価の範囲)", "評価中", "未評価"
M_ORDER = (M_RETRACTED, M_SIDE, M_INSUFFICIENT, M_CONFIRMED, M_EVALUATING, M_UNEVALUATED)
OP_OK, OP_BROKEN, OP_NONE = "動く", "動作不良", "確認なし"


def judge_measure(m, events, root, cache=None):
    """対策1つの状態を決める。動作(動くか)と効果(ミスが減ったか)を別に出す。

    効果確認済みにするには、既知・未知・通常タスクの3種類の基準が今の形でそろって合格し、本番の再発もないこと。
    フックの動作確認・記録した失敗の文での再生・対策の文の残存は、効果に数えない。
    """
    since = m.get("導入日", "")
    targets = split_top(m.get("原因", ""))
    recur = [e for e in events if since and e["date"] > since and set(e["causes"]) & set(targets)]
    same = same_day_recurrences(root, events, since, targets, markers_of(m.get("動作確認")),
                                cache if cache is not None else {})
    recur += [e for e in same if e["confirmed"]]
    possible = [e for e in same if not e["confirmed"]]
    op = [run_check(s, m, root, events) for s in split_top(m.get("動作確認", "")) if s]
    replay = [run_check(s, m, root, events) for s in split_top(m.get("再生", "")) if s]
    criteria, _ = parse_criteria(m.get("効果の基準", ""))
    results, _ = parse_results(m.get("効果の結果", ""))
    evals = []  # (種類, 説明, True/False/None)
    for c in criteria:
        if c["key"] is None:
            evals.append((c["kind"], c["spec"], None))
            continue
        mine = [r for r in results if r["kind"] == c["kind"] and r["key"] == c["key"]]
        fresh = [r for r in mine if not r["old_form"] and since and r["date"] >= since]
        if not fresh:
            note = "未測定" if not mine else f"今の形では未測定(古い結果 {mine[-1]['raw']}、{mine[-1]['date']})"
            evals.append((c["kind"], f"{c['key']}: {note}", None))
            continue
        r = max(fresh, key=lambda x: x["date"])
        if c.get("rel"):
            if r["ref"] is None:
                evals.append((c["kind"], f"{c['key']}: {r['raw']}({r['date']})。比べる形の基準なのに対照の結果がない", None))
                continue
            ok = r["got"] - r["ref"] >= c["need"]
            evals.append((c["kind"], f"{c['key']}: {r['raw']}(対照 {r['ref_raw']}、基準 対照{c['spec'].split('>=')[1]}、"
                                     f"{r['date']})", ok))
            continue
        ok = r["got"] >= c["need"]
        evals.append((c["kind"], f"{c['key']}: {r['raw']}(基準 {c['spec'].split('>=')[1]}、{r['date']})", ok))
    sides = [s for s in split_top(m.get("副作用", "")) if s and not s.startswith("なし")]
    confirmed_side = [s for s in sides if s.startswith("確認:")] + [d for k, d, ok in evals if k == "通常" and ok is False]
    insufficient = bool(recur) or any(ok is False and k in ("既知", "未知") for k, _, ok in evals)
    kinds_done = {k for k, _, ok in evals if ok}
    complete = bool(evals) and all(ok for _, _, ok in evals) and set(EVAL_KINDS) <= kinds_done
    any_fresh = any(ok is not None for _, _, ok in evals)
    flags = []
    if m.get("撤回日"):
        flags.append(M_RETRACTED)
    if confirmed_side:
        flags.append(M_SIDE)
    if insufficient:
        flags.append(M_INSUFFICIENT)
    if complete:
        flags.append(M_CONFIRMED)
    if any_fresh:
        flags.append(M_EVALUATING)
    state = next((s for s in M_ORDER if s in flags), M_UNEVALUATED)
    if m.get("撤回日"):
        operation = "―"
    elif any(ok is False for _, ok, _ in op):
        operation = OP_BROKEN
    elif op and all(ok for _, ok, _ in op):
        operation = OP_OK
    else:
        operation = OP_NONE
    return {"id": m["id"], "name": m["name"], "causes": targets, "since": since, "recur": recur,
            "op": op, "operation": operation, "replay": replay, "evals": evals, "sides": sides,
            "suspected": [s for s in sides if not s.startswith("確認:")], "state": state, "flags": flags,
            "decision": m.get("決定", ""), "derived": m.get("derived", False), "possible": possible}


def derived_measures(causes, measures):
    """対策の一覧に登録していない原因から、原因の表の項目(対策・対策日・擬似解消関数・最後の再テスト)で対策を1つ作る。

    中身を推測で書き足さないため、作用の仮説などは空のまま。名前に「未登録」と付け、見直し候補でも区別する(2026-10-09)。
    """
    # 撤回済み・採らなかった介入は、その原因の今の対策ではないので数えない
    covered = {c for m in measures if not m.get("撤回日") for c in split_top(m.get("原因", ""))}
    out = []
    for c in causes:
        if c["id"] in covered or not c.get("対策日") or c.get("対策", "").startswith("見送り"):
            continue
        specs = split_top(c.get("擬似解消関数", ""))
        known = []
        for s in specs:
            m = re.fullmatch(r"再テスト:(事例\d+-\d+>=\d+/\d+)", s)
            if m:
                known.append("既知:" + m.group(1))
        results = [f"既知:{r.group(1)}={r.group(2)}({r.group(3)})" for r in
                   re.finditer(r"(事例\d+-\d+)=(\d+/\d+)\((" + DATE + r")\)", c.get("最後の再テスト", ""))]
        out.append({"id": c["id"] + "*", "name": f"{c['id']} の対策(未登録。原因の表から作った)",
                    **{f: "" for f in MEASURE_FIELDS}, "原因": c["id"], "導入日": c["対策日"],
                    "動作確認": "、".join(s for s in specs if s.startswith(("振る舞い:", "残存:"))),
                    "効果の基準": "、".join(known), "効果の結果": "、".join(results), "derived": True})
    return out


def review_candidates(measure_results, cause_results, today):
    """見直し候補(改良案 C1)。直す・撤回する・残す(理由)を決める候補で、候補になっただけでは消さない。"""
    out = []
    for r in measure_results:
        if r["state"] == M_RETRACTED:
            continue
        reasons = []
        if r["operation"] == OP_BROKEN:
            reasons.append("動作不良")
        if M_SIDE in r["flags"]:
            reasons.append("副作用あり")
        if M_INSUFFICIENT in r["flags"]:
            if r["recur"]:
                reasons.append(f"導入後に本番で再発 {len(r['recur'])} 件(最後 {max(e['date'] for e in r['recur'])})")
            else:
                reasons.append("既知・未知の失敗例で基準に届かない")
        if r["state"] in (M_UNEVALUATED, M_EVALUATING) and r["since"]:
            days = (today - datetime.date.fromisoformat(r["since"])).days
            if days >= REVIEW_DAYS:
                reasons.append(f"導入から {days} 日、効果が確かめられていない")
        if reasons:
            if r.get("decision"):
                reasons.append(f"決定: {r['decision']}")
            out.append((f"{r['id']} {r['name']}", reasons))
    covered = {c for r in measure_results for c in r["causes"]}
    for r in cause_results:
        if r["id"] in covered:
            continue
        if r["status"] in (S_RECUR, S_BROKEN, S_KNOWN_FAIL):
            out.append((f"{r['id']} {r['name']}(対策が一覧に未登録)", [r["status"]]))
        elif r["status"] in (S_RUNS_ONLY, S_TEXT_ONLY, S_UNEVALUATED, S_KNOWN_PASS) and r["since"]:
            days = (today - datetime.date.fromisoformat(r["since"])).days
            if days >= REVIEW_DAYS:
                out.append((f"{r['id']} {r['name']}(対策が一覧に未登録)", [f"対策から {days} 日、効果が確かめられていない"]))
    return out


# ---------- 書き方の点検と表示 ----------

def structure_errors(events, causes, measures=()):
    ids = {c["id"] for c in causes}
    errs = []
    for e in events:
        for c in e["causes"]:
            if c == UNCLASSIFIED:
                errs.append(f"原因が未分類: {e['date']} {e['what'][:40]}")
            elif c != NOT_A_MISTAKE and c not in ids:
                errs.append(f"原因の表にない番号 {c}: {e['date']} {e['what'][:40]}")
    for c in causes:
        if c["対策日"] and not re.fullmatch(DATE, c["対策日"]):
            errs.append(f"{c['id']} の対策日の書き方: {c['対策日']}")
    for m in measures:
        for c in split_top(m.get("原因", "")):
            if c not in ids:
                errs.append(f"{m['id']} の原因が原因の表にない: {c}")
        for f in ("導入日", "撤回日"):
            if m.get(f) and not re.fullmatch(DATE, m[f]):
                errs.append(f"{m['id']} の{f}の書き方: {m[f]}")
        if not m.get("導入日"):
            errs.append(f"{m['id']} に導入日がない")
        errs += [f"{m['id']} の{e}" for e in parse_criteria(m.get("効果の基準", ""))[1]]
        errs += [f"{m['id']} の{e}" for e in parse_results(m.get("効果の結果", ""))[1]]
        for s in split_top(m.get("副作用", "")):
            if s and not re.match(r"^(確認|疑い):|^なし", s):
                errs.append(f"{m['id']} の副作用は「確認:」「疑い:」「なし」で始める: {s[:30]}")
        if m.get("決定") and not m["決定"].startswith(DECISIONS):
            errs.append(f"{m['id']} の決定は「{'」「'.join(DECISIONS)}」のどれかで始める: {m['決定'][:30]}")
    return errs


def count(values):
    out = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return "、".join(f"{k} {v}" for k, v in out.items())


def render(results, events, errors, measure_results=(), candidates=(), today=None):
    mark = {True: "合格", False: "不合格", None: "判定なし"}
    out_of_scope = sum(NOT_A_MISTAKE in e["causes"] for e in events)
    head = (f"判定日: {(today or datetime.date.today()).isoformat()} / 置き場の行: {len(events)}(うち対象外 {out_of_scope})"
            f" / 原因: {len(results)}({count(r['status'] for r in results)})")
    if measure_results:
        derived = sum(1 for r in measure_results if r.get("derived"))
        decided = sum(1 for _, reasons in candidates if any(x.startswith("決定:") for x in reasons))
        head += (f" / 対策: {len(measure_results)}(うち未登録 {derived}。{count(r['state'] for r in measure_results)})"
                 f" / 見直し候補: {len(candidates)}(決定済み {decided})")
    lines = [head]
    if errors:
        lines.append("書き方の誤り: " + " / ".join(errors))
    lines += ["", "動作の確認・記録・既知の失敗例への合格は、効果(実際のミスが減ったか)とは別に数える。", "",
              "### 原因ごと", "",
              "| 原因 | 出来事 | 対策後の再発(本番) | 擬似解消関数の結果 | 原因の状態 |", "|---|---|---|---|---|"]
    for r in results:
        recur = "、".join(e["date"] + ("(同じ日。版で確認)" if e.get("same_day") else "") for e in r["recur"]) if r["recur"] else "なし"
        if r.get("possible"):
            recur += f"<br>同じ日の再発の疑い {len(r['possible'])} 件(要確認)"
        checks = "<br>".join(f"{kind} {mark[ok]}: {d}" for kind, ok, d in r["checks"]) or "なし"
        lines.append(f"| {r['id']} {r['name']} | {r['events']} | {recur} | {checks} | **{r['status']}** |")
    if measure_results:
        lines += ["", "### 対策ごと", "",
                  "| 対策 | 原因 | 導入日 | 動作 | 記録した失敗の文での再生(フック単体) | 効果の評価(既知・未知・通常) | 本番の再発(導入後) | 副作用 | 状態 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in measure_results:
            op = "<br>".join(f"{mark[ok]}: {d}" for _, ok, d in r["op"])
            replay = "<br>".join(d for _, _, d in r["replay"]) or "―"
            evals = "<br>".join(f"{k} {mark[ok]}: {d}" for k, d, ok in r["evals"]) or "基準なし"
            same = sum(1 for e in r["recur"] if e.get("same_day"))
            recur = (f"{len(r['recur'])} 件(最後 {max(e['date'] for e in r['recur'])}"
                     + (f"、うち同じ日 {same}" if same else "") + ")") if r["recur"] else "なし"
            if r.get("possible"):
                recur += f"<br>同じ日の再発の疑い {len(r['possible'])} 件(要確認)"
            sides = "<br>".join(r["sides"]) or "記録なし"
            state = f"**{r['state']}**" + (f"<br>決定: {r['decision']}" if r.get("decision") else "")
            lines.append(f"| {r['id']} {r['name']} | {'、'.join(r['causes'])} | {r['since']} | {r['operation']}"
                         f"{'<br>' + op if op else ''} | {replay} | {evals} | {recur} | {sides} | {state} |")
        lines += ["", "### 見直し候補", "",
                  "直す・撤回する・残す(理由)を決める候補。候補になっただけでは消さない。消す前に、必要性・副作用・代わりの手を確かめる。", ""]
        lines += [f"- {name}: {'、'.join(reasons)}" for name, reasons in candidates] or ["- なし"]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    today = datetime.date.today()
    events = parse_inbox(os.path.join(args.root, INBOX))
    causes = parse_causes(os.path.join(args.root, CAUSES))
    measures = parse_measures(os.path.join(args.root, CAUSES))
    errors = structure_errors(events, causes, measures)
    cache = {}
    results = [judge(c, events, args.root, cache) for c in causes]
    measure_results = [judge_measure(m, events, args.root, cache) for m in measures + derived_measures(causes, measures)]
    candidates = review_candidates(measure_results, results, today)
    text = render(results, events, errors, measure_results, candidates, today)
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
