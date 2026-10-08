#!/usr/bin/env python3
"""狙ったミスを起こさせる試験(ストレステスト)を、リポジトリのコピーで `claude -p` に流し、判定で数える。

使い方:
  python3 stress.py calibrate <候補.json> <出力フォルダ> <回数> [候補id,...]
      フックなし(before)だけで流し、候補ごとの失敗率を調べる(下調べ)。
  python3 stress.py compare <候補.json> <出力フォルダ> <回数> <試す仕組み.py> [候補id,...]
      before(いまの仕組み)/ after(+試す仕組み)/ sham(+同じ場面で中身のない文だけ返す)で流して比べる。
      試す仕組みは Stop フックで、環境変数 STRESS_MODE=real|sham に従うこと。

- 候補.json は [{"id", "lever", "prompt", "grader"}] の配列。grader は graders/ の下のモジュール名(例: unread_change)。
- <出力フォルダ>/plan.md(計画)がないと動かない。書式は run.py と同じ4行(測りたいもの・使う条件・費用の見込み・結果で決めること)。
- 安全策は `/retest` の run.py をまねる: 毎回専用フォルダにリポジトリをコピーし、答え合わせになるファイルとこのスキルの
  フォルダは置かない。本体・出力先・候補.json を読めないように止め、Bash も止める。会話番号は毎回新しくする。
"""
import glob, importlib.util, json, os, re, shutil, subprocess, sys, time, uuid
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
MODEL = os.environ.get("STRESS_MODEL", "claude-opus-5-5")
BUDGET = os.environ.get("STRESS_MAX_BUDGET_USD", "1.0")
# 答え合わせになるファイル(run.py の LEAK_FILES と同じ)と、この会話の状態ファイル。このスキルのフォルダも置かない
LEAK = {"docs/knowledge/mistake-cases.md", "docs/knowledge/mistakes.md", "docs/knowledge/retest-log.md",
        "docs/knowledge/mistake-inbox.md", ".claude/reasoning/state.md", ".claude/reasoning/turn.json",
        ".claude/reasoning/turn-state.json"}
LEAK_DIRS = (".claude/skills/stress/",)
PLAN_KEYS = ["測りたいもの:", "使う条件:", "費用の見込み:", "結果で決めること:"]


def check_plan(out_dir):
    path = os.path.join(out_dir, "plan.md")
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    missing = [k for k in PLAN_KEYS if not re.search(rf"^{re.escape(k)}\s*\S", text, re.M)]
    if missing:
        sys.exit(f"{path} に計画を書いてから動かしてください。足りない行: {' '.join(missing)}")
    print(text, flush=True)


def grader(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "graders", f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def build(cond, ws, out_dir, cases_path, hook):
    if os.path.exists(ws):
        shutil.rmtree(ws)
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO,
                           capture_output=True, text=True, check=True).stdout.split("\n")
    for f in filter(None, files):
        if f in LEAK or f.startswith(LEAK_DIRS) or not os.path.exists(os.path.join(REPO, f)):
            continue
        os.makedirs(os.path.dirname(os.path.join(ws, f)) or ws, exist_ok=True)
        shutil.copy2(os.path.join(REPO, f), os.path.join(ws, f))
    sp = os.path.join(ws, ".claude/settings.json")
    st = json.load(open(sp))
    # 境界(権限の規則・guard.py)は測る対象ではないので外す(run.py と同じ)。本体・出力先・候補を読めないように止める
    st["permissions"] = {"deny": ["Bash"]}
    for p in (REPO + "/**", out_dir + "/**", cases_path):
        for tool in ("Read", "Grep", "Glob", "Edit", "Write"):
            st["permissions"]["deny"].append(f"{tool}(/{p})")
    hooks = st.get("hooks", {})
    for ev in list(hooks):
        hooks[ev] = [g for g in hooks[ev] if "hooks/guard.py" not in json.dumps(g)]
        if not hooks[ev]:
            del hooks[ev]
    if cond in ("after", "sham"):
        shutil.copy2(hook, os.path.join(ws, ".claude/hooks/stress-under-test.py"))
        mode = "real" if cond == "after" else "sham"
        hooks.setdefault("Stop", []).append({"hooks": [{"type": "command",
            "command": f"STRESS_MODE={mode} python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/stress-under-test.py\""}]})
    st["hooks"] = hooks
    json.dump(st, open(sp, "w"), ensure_ascii=False, indent=2)


def run_one(case, cond, n, out_dir, cases_path, hook):
    name = f"{case['id']}-{cond}-{n}"
    ws = os.path.join("/tmp/stress-sandboxes", os.path.basename(out_dir.rstrip("/")), name)
    build(cond, ws, out_dir, cases_path, hook)
    sid = str(uuid.uuid4())
    t0 = time.time()
    p = subprocess.run(["claude", "-p", case["prompt"], "--model", MODEL, "--permission-mode", "bypassPermissions",
                        "--output-format", "json", "--session-id", sid, "--max-budget-usd", BUDGET],
                       cwd=ws, capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
    wall = round(time.time() - t0, 1)
    rd = os.path.join(out_dir, name)
    os.makedirs(rd, exist_ok=True)
    open(os.path.join(rd, "result.json"), "w").write(p.stdout)
    tr = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl"))
    fails = []
    if tr:
        shutil.copy2(tr[0], os.path.join(rd, "transcript.jsonl"))
        fails = grader(case.get("grader", "unread_change")).failures(tr[0], ws)
    try:
        d = json.loads(p.stdout)
    except ValueError:
        d = {}
    return dict(name=name, case=case["id"], cond=cond, cost=d.get("total_cost_usd"), wall=wall,
                turns=d.get("num_turns"), error=d.get("is_error"), failed=bool(fails), fails=fails)


def summarize(rows):
    by = {}
    for r in rows:
        by.setdefault((r["case"], r["cond"]), []).append(r)
    print("候補 | 条件 | 失敗 | 費用(平均) | 時間(平均)")
    for (c, cond), xs in sorted(by.items()):
        costs = [x["cost"] or 0 for x in xs]
        print(f"{c} | {cond} | {sum(x['failed'] for x in xs)}/{len(xs)} | ${sum(costs) / len(xs):.3f} | "
              f"{sum(x['wall'] for x in xs) / len(xs):.1f}s")
    print(f"合計費用: ${sum((r['cost'] or 0) for r in rows):.2f}")
    print("注意: 判定が「失敗」とした回は、答えの文と読んだファイルを開いて確かめること(判定の誤検知は 2026-10-08 に3件あった)。")


def main():
    mode, cases_path, out_dir, k = sys.argv[1], os.path.abspath(sys.argv[2]), os.path.abspath(sys.argv[3]), int(sys.argv[4])
    hook = None
    rest = sys.argv[5:]
    if mode == "compare":
        hook, rest = os.path.abspath(rest[0]), rest[1:]
    elif mode != "calibrate":
        sys.exit("calibrate か compare を指定してください")
    os.makedirs(out_dir, exist_ok=True)
    check_plan(out_dir)
    cases = json.load(open(cases_path, encoding="utf-8"))
    if rest:
        cases = [c for c in cases if c["id"] in rest[0].split(",")]
    conds = ["before"] if mode == "calibrate" else ["before", "after", "sham"]
    jobs = [(c, cond, n) for n in range(1, k + 1) for c in cases for cond in conds]
    rows = []
    with ThreadPoolExecutor(max_workers=int(os.environ.get("STRESS_WORKERS", "6"))) as ex:
        for r in ex.map(lambda j: run_one(*j, out_dir, cases_path, hook), jobs):
            rows.append(r)
            print(json.dumps({x: r[x] for x in ("name", "cost", "wall", "failed")}, ensure_ascii=False), flush=True)
            open(os.path.join(out_dir, "runs.jsonl"), "a").write(json.dumps(r, ensure_ascii=False) + "\n")
    summarize(rows)


if __name__ == "__main__":
    main()
