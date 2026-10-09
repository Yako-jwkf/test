#!/usr/bin/env python3
"""効果の評価で、/retest(run.py)の出力から、費用・長さ・道具の回数と、通常タスクの機械判定を数える台本。

使い方:
  python3 scripts/eval_metrics.py summary <出力フォルダ> <事例番号>
  python3 scripts/eval_metrics.py compare <変更前の出力フォルダ> <変更後の出力フォルダ> <事例番号> [条件]

- run.py の出力 <出力フォルダ>/<条件>-<回>/turnN.json と answers.md を読む。会話の記録は turn1.json の session_id から
  ~/.claude/projects/*/<id>.jsonl を探す(環境変数 EVAL_TRANSCRIPTS で探す場所を変えられる)。
- 機械判定は docs/knowledge/mistake-cases.md の事例の「- **機械判定**:」の行。書ける規則(、で区切る):
  含む:文字列 / 文字数<=N / 道具なし:名前|名前
- judge(AI の採点)は使わない。既知・未知の事例の条件は /retest の手順4で採点する。この台本は数えるだけで、
  結果は人が原因の表の「効果の結果」「副作用」に書く。
- 差の決め方は、測る前に決めた effect-evaluation.md と同じ(下の定数)。読むだけで、どのファイルも書き換えない。
"""
import glob
import json
import os
import re
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "docs/knowledge/mistake-cases.md")
# 差の決め方(effect-evaluation.md の「決め方」と同じ。変えるときは両方を直す)
MIN_RUNS = 5          # これより少ない回数では、改善・悪化を言わない
PASS_DIFF = 2         # 合格の回数がこれ以上変わったら改善・悪化
RATIO = 1.3           # 費用・長さ・道具の回数の中央値がこれを超えて増えたら「悪化の疑い」
WEB_TOOLS = ("WebSearch", "WebFetch")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_rules(case_no, cases_path=CASES):
    text = read(cases_path)
    m = re.search(rf"^## 事例{case_no}\n(.*?)(?=^## 事例|\Z)", text, re.S | re.M)
    if not m:
        return []
    r = re.search(r"^- \*\*機械判定\*\*:\s*(.+)$", m.group(1), re.M)
    return [s.strip() for s in r.group(1).split("、")] if r else []


def check_rules(rules, answer, tools):
    """戻り値: (合格か, 外れた規則)。規則がなければ (None, [])。"""
    if not rules:
        return None, []
    fails = []
    for rule in rules:
        kind, _, arg = rule.partition(":")
        if kind == "含む":
            ok = arg in answer
        elif rule.startswith("文字数<="):
            ok = len(answer) <= int(rule.split("<=")[1])
        elif kind == "道具なし":
            ok = not any(tools.get(t) for t in arg.split("|"))
        else:
            ok = False
            rule = f"知らない規則 {rule}"
        if not ok:
            fails.append(rule)
    return not fails, fails


def find_transcript(session_id, transcripts_root=None):
    root = transcripts_root or os.environ.get("EVAL_TRANSCRIPTS") or os.path.expanduser("~/.claude/projects")
    hits = glob.glob(os.path.join(root, "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def transcript_counts(path):
    """会話の記録から、道具の名前ごとの回数と、停止フックに止められた回数を数える。"""
    tools, blocks = {}, 0
    for line in read(path).splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        msg = r.get("message") or {}
        content = msg.get("content")
        if r.get("type") == "user" and isinstance(content, str) and content.startswith("Stop hook feedback"):
            blocks += 1
        if r.get("type") == "assistant" and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tools[c.get("name", "?")] = tools.get(c.get("name", "?"), 0) + 1
    return tools, blocks


def answer_text(run_dir):
    path = os.path.join(run_dir, "answers.md")
    if not os.path.exists(path):
        return ""
    return re.sub(r"^## ターン\d+\n\n", "", read(path), flags=re.M).strip()


def run_metrics(run_dir, rules, transcripts_root=None):
    cost, dur, turns, sid, error = 0.0, 0, 0, None, False
    for p in sorted(glob.glob(os.path.join(run_dir, "turn*.json"))):
        try:
            d = json.loads(read(p))
        except ValueError:
            error = True
            continue
        cost += d.get("total_cost_usd") or 0
        dur += d.get("duration_ms") or 0
        turns += d.get("num_turns") or 0
        sid = sid or d.get("session_id")
        error = error or bool(d.get("is_error"))
    answer = answer_text(run_dir)
    error = error or "(失敗:" in answer
    tr = find_transcript(sid, transcripts_root) if sid else None
    tools, blocks = transcript_counts(tr) if tr else ({}, 0)
    ok, fails = check_rules(rules, answer, tools)
    return {"name": os.path.basename(run_dir), "cost": cost, "duration_s": dur / 1000, "turns": turns,
            "chars": len(answer), "tools": tools, "tool_calls": sum(tools.values()),
            "web_calls": sum(tools.get(t, 0) for t in WEB_TOOLS), "hook_blocks": blocks,
            "transcript": bool(tr), "error": error, "pass": ok, "fails": fails}


def collect(out_dir, case_no, cases_path=CASES, transcripts_root=None):
    rules = load_rules(case_no, cases_path)
    groups = {}
    for d in sorted(glob.glob(os.path.join(out_dir, "*-*"))):
        m = re.fullmatch(r"(.+)-(\d+)", os.path.basename(d))
        if os.path.isdir(d) and m:
            groups.setdefault(m.group(1), []).append(run_metrics(d, rules, transcripts_root))
    return groups


def aggregate(runs):
    ok_runs = [r for r in runs if not r["error"]]
    judged = [r for r in ok_runs if r["pass"] is not None]
    med = (lambda k: statistics.median(r[k] for r in ok_runs)) if ok_runs else (lambda k: 0)
    return {"n": len(runs), "errors": len(runs) - len(ok_runs),
            "passed": sum(1 for r in judged if r["pass"]), "judged": len(judged),
            "cost": med("cost"), "chars": med("chars"), "tool_calls": med("tool_calls"),
            "web_calls": sum(r["web_calls"] for r in ok_runs), "hook_blocks": sum(r["hook_blocks"] for r in ok_runs),
            "cost_range": (min((r["cost"] for r in ok_runs), default=0), max((r["cost"] for r in ok_runs), default=0)),
            "missing_transcripts": sum(1 for r in ok_runs if not r["transcript"])}


def fmt(a):
    passed = f"{a['passed']}/{a['judged']}" if a["judged"] else "機械判定なし"
    return (f"回数 {a['n']}(失敗 {a['errors']}) | 機械判定 {passed} | 費用の中央値 ${a['cost']:.3f}"
            f"(${a['cost_range'][0]:.3f}〜${a['cost_range'][1]:.3f}) | 長さの中央値 {a['chars']:.0f}字 | "
            f"道具の回数の中央値 {a['tool_calls']:.0f} | 検索 {a['web_calls']} | フックの停止 {a['hook_blocks']}"
            + (f" | 会話の記録なし {a['missing_transcripts']}" if a["missing_transcripts"] else ""))


def verdicts(before, after):
    """先に決めた決め方で、変更前と変更後を比べる。戻り値: 文の一覧。"""
    out = []
    n = min(before["n"] - before["errors"], after["n"] - after["errors"])
    if before["judged"] and after["judged"]:
        diff = after["passed"] - before["passed"]
        if n < MIN_RUNS:
            out.append(f"機械判定: 差 {diff:+d}。回数 {n} は {MIN_RUNS} 未満で、判定保留(回数を増やす)")
        elif diff >= PASS_DIFF:
            out.append(f"機械判定: 改善({before['passed']}→{after['passed']})")
        elif diff <= -PASS_DIFF:
            out.append(f"機械判定: 悪化({before['passed']}→{after['passed']})")
        else:
            out.append(f"機械判定: 差なし(差 {diff:+d} は誤差の範囲)")
    for key, label in (("cost", "費用"), ("chars", "長さ"), ("tool_calls", "道具の回数")):
        b, a = before[key], after[key]
        if b and a / b > RATIO:
            out.append(f"{label}: 悪化の疑い(中央値 {b:g}→{a:g}、{a / b:.2f}倍)")
    if before["web_calls"] == 0 and after["web_calls"] > 0:
        out.append(f"検索: 悪化の疑い(0→{after['web_calls']})")
    return out or ["差なし(決めた目安の範囲)"]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 3 and argv[0] == "summary":
        for cond, runs in collect(argv[1], argv[2]).items():
            print(f"{cond}: {fmt(aggregate(runs))}")
            for r in runs:
                if r["fails"] or r["error"]:
                    print(f"  {r['name']}: {'失敗の回' if r['error'] else '外れ: ' + '、'.join(r['fails'])}(答えを開いて確かめる)")
        return 0
    if len(argv) >= 4 and argv[0] == "compare":
        before, after = collect(argv[1], argv[3]), collect(argv[2], argv[3])
        conds = [argv[4]] if len(argv) > 4 else sorted(set(before) & set(after))
        for cond in conds:
            if cond not in before or cond not in after:
                print(f"{cond}: 片方の出力がない")
                continue
            b, a = aggregate(before[cond]), aggregate(after[cond])
            print(f"{cond} 変更前: {fmt(b)}\n{cond} 変更後: {fmt(a)}")
            for v in verdicts(b, a):
                print(f"  - {v}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
