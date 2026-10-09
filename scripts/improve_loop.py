#!/usr/bin/env python3
"""自己改善ループの外側の制御。反復・予算・時間・停止・撤回・再開・履歴を、LLM ではなくこの台本が持つ。

使い方:
  python3 scripts/improve_loop.py run --state DIR [--max-iter N] [--budget USD] [--timeout-min M]
                                      [--scenario FILE] [--repo DIR]
  python3 scripts/improve_loop.py status --state DIR

1反復の流れ:
  停止条件を確かめる → 対象と変更の種類を選ぶ(見直し候補と、過去の反復の履歴) → 作業用フォルダ(git worktree)で
  1つだけ変更する → 評価の基準を変える差分なら棄却 → 評価して変更前の基準値と比べる → 決まりで採否を決める →
  履歴に追記する → 作業用フォルダを片付ける(採用候補は、取り込まないブランチとして残す)。

- 採用しても main には入れない。採用候補のブランチ名を履歴に残し、取り込むかは利用者が決める。
- 反復を数えるのはこの台本。評価の結果がそろわない反復は「評価不能」として止める(成功に数えない)。
- 履歴(episodes.jsonl)は追記だけ。状態(state.json)は再開のためのもので、書き換える。
- 実装役と評価役は差し替えられる。いまあるのは、制御を確かめるための偽物(scenario で結果を決める)だけ。
  本物(claude -p で実装し、run.py と judge で評価する)は未実装(2026-10-09)。費用の了承を得てから作って測る。
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 差の決め方は scripts/eval_metrics.py と同じ(effect-evaluation.md の「決め方」)
MIN_RUNS, PASS_DIFF, RATIO = 5, 2, 1.3
# 変更の種類の順。外す・まとめるを先に試し、足すのは最後(足すだけのループにしないため)
KINDS = ("外す", "まとめる", "直す", "足す")
REDUCING = ("外す", "まとめる")
# 実装役が変えてはいけないファイル(評価の基準)。変えた差分は評価せずに棄却する
EVAL_PATHS = ("docs/knowledge/mistake-cases.md", "docs/knowledge/mistake-causes.md", "docs/knowledge/retest-log.md",
              "scripts/mistake_check.py", "scripts/eval_metrics.py", "scripts/improve_loop.py", "scripts/test_",
              ".claude/skills/retest/", ".claude/agents/judge.md", ".claude/hooks/guard.py", ".claude/settings.json")


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=check).stdout.strip()


# ---------- 状態と履歴 ----------

class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)
        self.state_file = os.path.join(path, "state.json")
        self.history_file = os.path.join(path, "episodes.jsonl")

    def load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        return {"created": now(), "iteration": 0, "spent_usd": 0.0, "baseline": None, "in_progress": None, "stopped": None}

    def save_state(self, state):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_file)

    def history(self):
        if not os.path.exists(self.history_file):
            return []
        with open(self.history_file, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def append(self, episode):
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(episode, ensure_ascii=False) + "\n")


# ---------- 対象を選ぶ ----------

def choose(candidates, history):
    """見直し候補の順に、まだ試していない (対象, 変更の種類) を選ぶ。

    過去に採用候補・棄却・撤回・評価不能になった組は、理由なく繰り返さない。「中断」は案の結果ではないので、もう一度試す。
    同じ原因で「足す」が2回棄却・撤回されたら、その原因には「足す」を出さない(足し続けずに、原因の仮説か介入の種類を変える)。
    戻り値: (候補, 種類, 選んだ理由, 参照した過去の反復) か None。
    """
    tried = {(e["target"], e["kind"]) for e in history if e["decision"] != "中断"}
    failed_adds = {}
    for e in history:
        if e["kind"] == "足す" and e["decision"] in ("棄却", "撤回"):
            for c in e.get("causes", []):
                failed_adds[c] = failed_adds.get(c, 0) + 1
    for cand in candidates:
        for kind in KINDS:
            if kind not in cand.get("kinds", KINDS):
                continue
            if (cand["id"], kind) in tried:
                continue
            if kind == "足す" and any(failed_adds.get(c, 0) >= 2 for c in cand.get("causes", [])):
                continue
            seen = [e["episode_id"] for e in history if e["target"] == cand["id"] or set(e.get("causes", [])) & set(cand.get("causes", []))]
            reason = f"見直し候補の順で、まだ試していない組。同じ対象・原因の過去の反復: {', '.join(seen) or 'なし'}"
            return cand, kind, reason, seen
    return None


# ---------- 採否の決まり ----------

def decide(kind, base, cand):
    """変更前(base)と変更後(cand)の評価から、採否を決まりで決める。戻り値: (決定, 理由)。

    評価の形: {"ok": bool, "cases": {名前: [合格, 回数]}, "normal": {名前: {"passed", "n", "cost", "chars"}}}
    足す・直す: 改善(合格が2回以上増えた事例がある)で、悪化も副作用もなければ採用候補。差なしは棄却。
    外す・まとめる: 悪化も副作用もなければ採用候補(減らす変更は、悪くならないことが目標)。
    悪化か副作用があれば撤回。評価がそろわなければ評価不能(成功に数えない)。
    """
    if not cand or not cand.get("ok") or not base or not base.get("ok"):
        return "評価不能", "評価の結果がそろわない(評価環境が壊れたか、途中で止まった)"
    keys = set(base["cases"]) | set(base.get("normal", {}))
    if keys - set(cand["cases"]) - set(cand.get("normal", {})):
        return "評価不能", "変更前にあった事例の結果が、変更後にない"
    short = [k for k, (p, n) in cand["cases"].items() if n < MIN_RUNS]
    if short:
        return "評価不能", f"回数が {MIN_RUNS} 未満の事例がある: {', '.join(sorted(short))}"
    worse, better = [], []
    for k, (bp, bn) in base["cases"].items():
        cp, cn = cand["cases"][k]
        if cp - bp <= -PASS_DIFF:
            worse.append(f"{k} {bp}/{bn}→{cp}/{cn}")
        elif cp - bp >= PASS_DIFF:
            better.append(f"{k} {bp}/{bn}→{cp}/{cn}")
    side = []
    for k, b in base.get("normal", {}).items():
        c = cand["normal"][k]
        if c["passed"] - b["passed"] <= -PASS_DIFF:
            side.append(f"{k} 正答 {b['passed']}→{c['passed']}")
        for m, label in (("cost", "費用"), ("chars", "長さ")):
            if b.get(m) and c.get(m, 0) / b[m] > RATIO:
                side.append(f"{k} {label} {c[m] / b[m]:.2f}倍")
    if worse or side:
        return "撤回", "悪化: " + "、".join(worse + side)
    if kind in REDUCING:
        return "採用候補", "悪化・副作用なし(減らす変更なので、悪くならないことで採用候補)" + (f"。改善: {'、'.join(better)}" if better else "")
    if better:
        return "採用候補", "改善: " + "、".join(better)
    return "棄却", "差なし(決めた目安の範囲)。改善と言えない変更は採用しない"


# ---------- 実装役と評価役(偽物) ----------

class FakeImplementer:
    """制御を確かめるための偽の実装役。対象の印の行を、作業用フォルダのファイルから外す・書き換える・足す。"""

    def __init__(self, scenario):
        self.scenario = scenario

    def implement(self, worktree, cand, kind, history_seen, iteration):
        if self.scenario.get("crash_after_implement") == iteration:
            raise RuntimeError("試験のための中断(実装の後)")
        path = os.path.join(worktree, cand["file"])
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
        if kind == "外す":
            lines = [l for l in lines if cand["marker"] not in l]
        elif kind == "足す":
            lines.append(f"- {cand['id']} の追加の注意(偽物の変更、反復 {iteration})")
        else:
            lines = [l.replace(cand["marker"], cand["marker"] + "(直した)") for l in lines]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        for extra in self.scenario.get("touch_eval_files", {}).get(str(iteration), []):
            with open(os.path.join(worktree, extra), "a", encoding="utf-8") as f:
                f.write("\n基準を書き換えた(偽物)\n")
        return {"hypothesis": f"{cand['id']} を{kind}と、{'、'.join(cand.get('causes', []))} の失敗が減るか、悪くならない",
                "cost_usd": self.scenario.get("implement_cost", 0.5)}


class FakeEvaluator:
    """制御を確かめるための偽の評価役。scenario の outcomes[反復] で、変更後の結果を決める。

    improve: 対象の事例が +3 / same: 差なし / worse: 対象の事例が -3 / side_effect: 通常タスクの費用が2倍 /
    broken: 評価が壊れた(ok=False)。反復0は変更前の基準値。
    """

    def __init__(self, scenario):
        self.scenario = scenario

    def evaluate(self, worktree, iteration):
        cost = self.scenario.get("evaluate_cost", 1.0)
        base = {"ok": True, "cases": {"事例8-1": [1, 5], "事例2-1": [3, 5]},
                "normal": {"事例11-1": {"passed": 5, "n": 5, "cost": 0.2, "chars": 300}}}
        outcome = self.scenario.get("outcomes", {}).get(str(iteration), "same") if iteration else "baseline"
        r = json.loads(json.dumps(base))
        if outcome == "improve":
            r["cases"]["事例8-1"] = [4, 5]
        elif outcome == "worse":
            r["cases"]["事例2-1"] = [0, 5]
        elif outcome == "side_effect":
            r["normal"]["事例11-1"]["cost"] = 0.4
        elif outcome == "broken":
            r = {"ok": False}
        r["cost_usd"] = cost
        r["outcome"] = outcome
        return r


# ---------- ループ ----------

def changed_files(worktree):
    out = git(worktree, "status", "--porcelain")
    return [line[3:] for line in out.split("\n") if line.strip()]


def touches_eval(files):
    return [f for f in files if f.startswith(EVAL_PATHS)]


def cleanup(repo, worktree, keep_branch=None):
    if worktree and os.path.exists(worktree):
        if keep_branch:
            git(worktree, "checkout", "-q", "-b", keep_branch)
            git(worktree, "add", "-A")
            git(worktree, "-c", "user.name=improve-loop", "-c", "user.email=improve-loop@local",
                "commit", "-q", "-m", f"採用候補 {keep_branch}(取り込みは利用者が決める)")
        git(repo, "worktree", "remove", "--force", worktree, check=False)
    git(repo, "worktree", "prune", check=False)


def run(args, implementer=None, evaluator=None, out=print):
    store = Store(args.state)
    state = store.load_state()
    scenario = {}
    if getattr(args, "scenario", None):
        with open(args.scenario, encoding="utf-8") as f:
            scenario = json.load(f)
    implementer = implementer or FakeImplementer(scenario)
    evaluator = evaluator or FakeEvaluator(scenario)
    candidates = scenario.get("candidates", [])
    start = time.time()
    state["stopped"] = None

    # 再開: 途中で止まった反復があれば、作業用フォルダを片付けて「中断」として履歴に残す
    if state.get("in_progress"):
        ip = state["in_progress"]
        cleanup(args.repo, ip.get("worktree"))
        store.append({"episode_id": f"E{ip['iteration']:03d}", "iteration": ip["iteration"], "target": ip["target"],
                      "kind": ip["kind"], "causes": ip.get("causes", []), "decision": "中断",
                      "reason": "前の実行が途中で止まった。作業用フォルダを片付けて、次の反復へ進んだ", "time": now()})
        out(f"再開: 反復 {ip['iteration']} は途中で止まっていたので「中断」として記録した")
        # 中断した反復の番号は使い終わったものとして進める(履歴の番号を重ねない)
        state["iteration"] = max(state["iteration"], ip["iteration"])
        state["in_progress"] = None
        store.save_state(state)

    if state.get("baseline") is None:
        if state["spent_usd"] + scenario.get("evaluate_cost", 1.0) > args.budget:
            state["stopped"] = f"予算 ${args.budget:.2f} では変更前の評価(基準値)も流せない"
            store.save_state(state)
            out(f"停止: {state['stopped']}")
            return state
        base = evaluator.evaluate(args.repo, 0)
        state["spent_usd"] += base.get("cost_usd", 0)
        if not base.get("ok"):
            state["stopped"] = "変更前の評価(基準値)がそろわない。評価環境を直すまで止める"
            store.save_state(state)
            out(f"停止: {state['stopped']}")
            return state
        state["baseline"] = base
        store.save_state(state)
        out(f"基準値を取った(費用 ${base.get('cost_usd', 0):.2f})")

    while True:
        it = state["iteration"] + 1
        history = store.history()
        est = scenario.get("implement_cost", 0.5) + scenario.get("evaluate_cost", 1.0)
        if state["iteration"] >= args.max_iter:
            state["stopped"] = f"反復の上限 {args.max_iter} に達した"
        elif state["spent_usd"] + est > args.budget:
            state["stopped"] = f"予算 ${args.budget:.2f} を超える見込み(使った ${state['spent_usd']:.2f}、次の反復の見込み ${est:.2f})"
        elif (time.time() - start) / 60 > args.timeout_min:
            state["stopped"] = f"時間の上限 {args.timeout_min} 分に達した"
        if state["stopped"]:
            break
        pick = choose(candidates, history)
        if not pick:
            state["stopped"] = "試していない (対象, 種類) の組がない"
            break
        cand, kind, why, seen = pick
        worktree = tempfile.mkdtemp(prefix=f"improve-{it}-")
        os.rmdir(worktree)
        git(args.repo, "worktree", "add", "-q", "--detach", worktree, "HEAD")
        state["in_progress"] = {"iteration": it, "target": cand["id"], "kind": kind, "causes": cand.get("causes", []),
                                "worktree": worktree, "time": now()}
        store.save_state(state)

        impl = implementer.implement(worktree, cand, kind, seen, it)
        files = changed_files(worktree)
        episode = {"episode_id": f"E{it:03d}", "iteration": it, "target": cand["id"], "kind": kind,
                   "causes": cand.get("causes", []), "why_chosen": why, "history_seen": seen,
                   "hypothesis": impl.get("hypothesis"), "changed_files": files,
                   "diff": git(worktree, "diff"), "baseline": state["baseline"], "time": now()}
        cost = impl.get("cost_usd", 0)
        bad = touches_eval(files)
        if not files:
            decision, reason, result = "棄却", "実装役が何も変えなかった", None
        elif bad:
            decision, reason, result = "棄却", "評価の基準のファイルを変えた: " + "、".join(bad), None
        else:
            result = evaluator.evaluate(worktree, it)
            cost += result.get("cost_usd", 0)
            decision, reason = decide(kind, state["baseline"], result)
        branch = f"improve/E{it:03d}-{cand['id']}" if decision == "採用候補" else None
        cleanup(args.repo, worktree, keep_branch=branch)
        episode.update({"candidate_result": result, "decision": decision, "reason": reason,
                        "cost_usd": round(cost, 4), "branch": branch})
        store.append(episode)
        state["spent_usd"] = round(state["spent_usd"] + cost, 4)
        state["iteration"] = it
        state["in_progress"] = None
        store.save_state(state)
        out(f"反復 {it}: {cand['id']} を{kind} → {decision}({reason})。費用 ${cost:.2f}、累計 ${state['spent_usd']:.2f}")
        if decision == "評価不能":
            state["stopped"] = "評価不能(評価環境が壊れた可能性)。成功に数えず止める"
            break

    store.save_state(state)
    out(f"停止: {state['stopped']}")
    return state


def status(args, out=print):
    store = Store(args.state)
    state = store.load_state()
    out(f"反復 {state['iteration']} / 累計 ${state['spent_usd']:.2f} / 停止の理由: {state.get('stopped') or 'なし'}"
        + (f" / 途中の反復: {state['in_progress']['iteration']}" if state.get("in_progress") else ""))
    for e in store.history():
        out(f"{e['episode_id']} {e['target']} を{e['kind']}: {e['decision']}({e['reason']})"
            + (f" ブランチ {e['branch']}" if e.get("branch") else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--state", required=True)
    r.add_argument("--repo", default=ROOT)
    r.add_argument("--max-iter", type=int, default=3)
    r.add_argument("--budget", type=float, default=0.0, help="ドル。0 だと1反復も始めない(上限は必ず決める)")
    r.add_argument("--timeout-min", type=float, default=60.0)
    r.add_argument("--scenario", help="偽の実装役・評価役の結果を決める JSON(制御の試験用)")
    s = sub.add_parser("status")
    s.add_argument("--state", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "status":
        status(args)
        return 0
    if not args.scenario:
        sys.exit("本物の実装役・評価役は未実装(2026-10-09)。いまは --scenario で偽物を使い、制御だけを確かめられる")
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
