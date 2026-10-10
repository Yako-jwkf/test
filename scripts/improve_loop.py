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
- 実装役と評価役は差し替えられる。--scenario は制御を確かめるための偽物(scenario で結果を決める)。
  --config は本物(2026-10-09): 実装役は `claude -p` に読むだけで案を出させ、台本が作業用フォルダに当てる。
  評価役は run.py で答えさせ、judge の指示で `claude -p` に採点させ、eval_metrics.py で数える。
- 変更前の評価も、反復の作業用フォルダも、最初に決めた同じコミット(base_commit)から作る。作業中の編集で条件を変えないため。
"""
import argparse
import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_metrics as em  # noqa: E402  評価の基準のファイル(EVAL_PATHS)なので、反復の中では変わらない

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 差の決め方は scripts/eval_metrics.py と同じ(effect-evaluation.md の「決め方」)
MIN_RUNS, PASS_DIFF, RATIO = 5, 2, 1.3
# 変更の種類の順。外す・まとめるを先に試し、足すのは最後(足すだけのループにしないため)
KINDS = ("外す", "まとめる", "直す", "足す")
REDUCING = ("外す", "まとめる")
# 実装役が変えてはいけないファイル(評価の基準)。変えた差分は評価せずに棄却する
EVAL_PATHS = ("docs/knowledge/mistake-cases.md", "docs/knowledge/mistake-causes.md", "docs/knowledge/retest-log.md",
              "docs/knowledge/improve-loop/",
              "scripts/mistake_check.py", "scripts/eval_metrics.py", "scripts/improve_loop.py", "scripts/test_",
              ".claude/skills/retest/", ".claude/agents/judge.md", ".claude/hooks/guard.py", ".claude/settings.json")
RUN_PY = ".claude/skills/retest/run.py"
JUDGE_MD = ".claude/agents/judge.md"
CASES_MD = "docs/knowledge/mistake-cases.md"
CAUSES_MD = "docs/knowledge/mistake-causes.md"
VERDICTS = ("合格", "不合格", "該当なし")


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

    過去に採用候補・棄却・撤回になった組は、理由なく繰り返さない。「中断」と「評価不能」(利用上限・評価環境の故障)は
    案の結果ではないので、もう一度試す(評価不能ではループが止まるので、繰り返すのは次に動かしたときだけ)。
    同じ原因で「足す」が2回棄却・撤回されたら、その原因には「足す」を出さない(足し続けずに、原因の仮説か介入の種類を変える)。
    戻り値: (候補, 種類, 選んだ理由, 参照した過去の反復) か None。
    """
    tried = {(e["target"], e["kind"]) for e in history if e["decision"] not in ("中断", "評価不能")}
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

    評価の形: {"ok": bool, "cases": {名前: [合格, 採点した回数] か [合格, 採点した回数, 流した回数]},
              "normal": {名前: {"passed", "n", "cost", "chars"}}}
    足す・直す: 改善(合格が2回以上増えた事例がある)で、悪化も副作用もなければ採用候補。差なしは棄却。
    外す・まとめる: 悪化も副作用もなければ採用候補(減らす変更は、悪くならないことが目標)。
    悪化か副作用があれば撤回。評価がそろわなければ評価不能(成功に数えない)。
    流した回数は足りるが、judge の「該当なし」で採点した回数が足りない条件は、比べずに保留として理由に書く。
    """
    if not cand or not cand.get("ok") or not base or not base.get("ok"):
        return "評価不能", "評価の結果がそろわない(評価環境が壊れたか、途中で止まった)"
    keys = set(base["cases"]) | set(base.get("normal", {}))
    if keys - set(cand["cases"]) - set(cand.get("normal", {})):
        return "評価不能", "変更前にあった事例の結果が、変更後にない"
    short = [k for k, v in cand["cases"].items() if v[-1] < MIN_RUNS]
    if short:
        return "評価不能", f"回数が {MIN_RUNS} 未満の事例がある: {', '.join(sorted(short))}"
    held = sorted(k for k in base["cases"] if min(base["cases"][k][1], cand["cases"][k][1]) < MIN_RUNS)
    note = f"。保留(該当なしが多く比べない): {'、'.join(held)}" if held else ""
    worse, better = [], []
    for k, b in base["cases"].items():
        if k in held:
            continue
        bp, bn, cp, cn = b[0], b[1], cand["cases"][k][0], cand["cases"][k][1]
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
        return "撤回", "悪化: " + "、".join(worse + side) + note
    if kind in REDUCING:
        return "採用候補", ("悪化・副作用なし(減らす変更なので、悪くならないことで採用候補)"
                        + (f"。改善: {'、'.join(better)}" if better else "") + note)
    if better:
        return "採用候補", "改善: " + "、".join(better) + note
    return "棄却", "差なし(決めた目安の範囲)。改善と言えない変更は採用しない" + note


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


# ---------- 実装役と評価役(本物、2026-10-09) ----------

def claude_json(prompt, cwd, model, extra=(), timeout=1800):
    """`claude -p` を1回動かし、JSON の結果を返す。CLAUDE.md・フック・スキルは --safe-mode で読ませない。"""
    cmd = ["claude", "-p", prompt, "--model", model, "--safe-mode", "--output-format", "json",
           "--session-id", str(uuid.uuid4()), *extra]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    try:
        d = json.loads(p.stdout)
    except ValueError:
        return {"is_error": True, "result": f"exit={p.returncode} {p.stderr[:300]}", "total_cost_usd": 0}
    # 利用上限に達すると、エラーではなく通常の返事としてこの文が返る(run.py と同じ。2026-10-01)
    if "hit your session limit" in (d.get("result") or ""):
        d["is_error"] = True
    return d


def leak_lists(repo):
    """答え合わせになるファイルの一覧は run.py のものを使う(実装役にも事例の条件を見せない)。"""
    spec = importlib.util.spec_from_file_location("retest_run", os.path.join(repo, RUN_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.LEAK_FILES), tuple(mod.LEAK_DIRS)


def copy_without_leaks(src, dst):
    files, dirs = leak_lists(src)
    for f in filter(None, git(src, "ls-files", "--cached", "--others", "--exclude-standard").split("\n")):
        if f in files or f.startswith(dirs) or not os.path.isfile(os.path.join(src, f)):
            continue
        os.makedirs(os.path.dirname(os.path.join(dst, f)) or dst, exist_ok=True)
        shutil.copy2(os.path.join(src, f), os.path.join(dst, f))


def measure_entry(repo, mid):
    """原因の表の対策の節を、そのまま文で返す(実装役に渡す)。"""
    text = open(os.path.join(repo, CAUSES_MD), encoding="utf-8").read()
    m = re.search(rf"^### {mid} .*?(?=^### |^## |\Z)", text, re.S | re.M)
    return m.group(0).strip() if m else ""


def apply_edits(worktree, edits):
    """実装役の案(file・old・new)を当てる。1つでも当てられなければ、どれも当てない。戻り値: 誤りの一覧。"""
    errors, planned = [], {}
    for e in edits:
        rel = os.path.normpath(e.get("file", "")).lstrip("/")
        path = os.path.join(worktree, rel)
        if rel.startswith("..") or not os.path.isfile(path):
            errors.append(f"{rel}: 作業用フォルダにないファイル")
            continue
        text = planned.get(path) or open(path, encoding="utf-8").read()
        k = text.count(e.get("old", "")) if e.get("old") else 0
        if k != 1:
            errors.append(f"{rel}: 置き換える文が {k} か所(1か所でない)")
            continue
        planned[path] = text.replace(e["old"], e.get("new", ""), 1)
    if errors:
        return errors
    for path, text in planned.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return []


IMPLEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "edits": {"type": "array", "items": {"type": "object", "properties": {
            "file": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
            "required": ["file", "old", "new"]}},
        "why_different": {"type": "string"},
    },
    "required": ["hypothesis", "edits"],
}

KIND_HELP = {
    "外す": "対策の「場所」にある、その対策の文や処理だけを取り除く。取り除いたせいで宙に浮く参照(番号や「上の」など)があれば、それも最小限に直す。ほかの規則は変えない。",
    "まとめる": "対策の文を、同じ場所の近い規則と1つにまとめて短くする。働きは変えない。",
    "直す": "対策の文を最小限に書き換えて、副作用の疑いを起こしにくくする。対策が狙う原因への働きは残す。新しい規則を足さない。",
    "足す": "原因に作用する規則か処理を1つだけ足す。",
}


class ClaudeImplementer:
    """本物の実装役。`claude -p` は読むだけで、変える案を返す。作業用フォルダへは台本が当てる。

    答え合わせになるファイル(run.py の LEAK_FILES・LEAK_DIRS)を除いた写しの中で動かす。事例の入力や採点の条件を見せないため。
    """

    def __init__(self, config):
        self.model = config.get("implement_model", "claude-opus-5-5")
        self.max_usd = config.get("implement_cost", 2.0)

    def prompt(self, worktree, cand, kind, seen):
        past = []
        for e in seen:
            res = e.get("candidate_result") or {}
            past.append(f"- {e['episode_id']}: {e['target']} を{e['kind']} → {e['decision']}({e['reason']})\n"
                        f"  仮説: {e.get('hypothesis')}\n  差分:\n{(e.get('diff') or '')[:3000]}\n"
                        f"  評価: {json.dumps(res.get('cases', {}), ensure_ascii=False)}")
        return f"""あなたは、Claude Code の設定(CLAUDE.md・推論の手順・フックなど)を直す実装役です。ファイルは読むだけで、書き換えません。変える案を、決められた形で返してください。台本がその案を作業用の写しに当て、別の評価役が測ります。

## 変えるもの
対策 {cand['id']} を「{kind}」。{KIND_HELP[kind]}
1回の案で変えるのは、この対策1つだけです。

## 対策の記録(原因の表 {CAUSES_MD} から。この記録のファイルは変えない)
{measure_entry(worktree, cand['id'])}

## 同じ対象・同じ原因の過去の反復
{chr(10).join(past) or 'なし'}
過去に棄却・撤回された案と同じ案を出すなら、前と何が違うから今度は違う結果になると見るのかを why_different に書く。書けないなら別の案にする。

## 決まり
- 変えてはいけないファイル(評価の基準): {', '.join(EVAL_PATHS)}。これらに触れる案は評価されずに捨てられる。
- edits の各要素は、file(リポジトリからの相対パス)・old(今のファイルにある文。ファイルの中で1か所だけに当たる長さにする)・new(置き換えた後の文)。取り除くときは new を空か、前後の文だけにする。
- 案を出す前に、場所のファイルを Read で開き、old をファイルの文字どおりに写す。
- hypothesis には、この変更で何が変わり、どんな依頼の答えがどちらの向きに変わると見るかを1〜3文で書く(測る前に書く)。
"""

    def implement(self, worktree, cand, kind, seen, iteration):
        sandbox = tempfile.mkdtemp(prefix=f"improve-impl-{iteration}-")
        try:
            copy_without_leaks(worktree, sandbox)
            d = claude_json(self.prompt(worktree, cand, kind, seen), sandbox, self.model,
                            ["--tools", "Read,Grep,Glob", "--json-schema", json.dumps(IMPLEMENT_SCHEMA),
                             "--max-budget-usd", str(self.max_usd)])
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
        cost = d.get("total_cost_usd") or 0
        out = d.get("structured_output")
        if d.get("is_error") or not isinstance(out, dict):
            return {"hypothesis": None, "cost_usd": cost, "note": f"実装役が案を返さなかった: {str(d.get('result'))[:200]}"}
        errors = apply_edits(worktree, out.get("edits", []))
        return {"hypothesis": out.get("hypothesis"), "why_different": out.get("why_different"), "cost_usd": cost,
                "proposal": out, "note": ("案を当てられなかった: " + "、".join(errors)) if errors else None}


def case_conditions(repo, case):
    """事例の「良い答えの条件」を {番号: 文} で返す。"""
    text = open(os.path.join(repo, CASES_MD), encoding="utf-8").read()
    m = re.search(rf"^## 事例{case}\n(.*?)(?=^## 事例|\Z)", text, re.S | re.M)
    if not m:
        return {}
    body = m.group(1).split("- **良い答えの条件**", 1)
    if len(body) < 2:
        return {}
    conds = {}
    for line in body[1].split("\n")[1:]:
        if line.startswith("- **") or line.startswith("## "):
            break
        c = re.match(r"^\s+(\d+)\.\s+(.+)$", line)
        if c:
            conds[int(c.group(1))] = c.group(2).strip()
    return conds


def parse_judge_table(text, numbers):
    got = {}
    for line in (text or "").split("\n"):
        m = re.match(r"^\|\s*(\d+)\s*\|\s*(" + "|".join(VERDICTS) + r")\s*\|", line.strip())
        if m and int(m.group(1)) in numbers:
            got[int(m.group(1))] = m.group(2)
    return got if set(got) == set(numbers) else None


def majority(votes):
    for v in VERDICTS:
        if votes.count(v) >= 2:
            return v
    return "割れ"


class Judge:
    """judge の指示(.claude/agents/judge.md の本文)で、`claude -p` に採点させる。/retest の手順4と同じく、
    答え1つに2体、条件で割れたら3体目を足して多数決。答えには版も条件の出どころも書かない。"""

    def __init__(self, repo, model="sonnet"):
        text = open(os.path.join(repo, JUDGE_MD), encoding="utf-8").read()
        self.system = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S).strip()
        self.model = model
        self.cwd = tempfile.mkdtemp(prefix="improve-judge-")  # 空のフォルダで動かす

    def once(self, answer, conds):
        prompt = "## 答え\n" + answer + "\n\n## 良い答えの条件\n" + "\n".join(f"{k}. {v}" for k, v in sorted(conds.items()))
        for _ in range(2):  # 表が読めなければ1回だけ流し直す
            d = claude_json(prompt, self.cwd, self.model, ["--tools", "", "--system-prompt", self.system], timeout=600)
            got = None if d.get("is_error") else parse_judge_table(d.get("result"), set(conds))
            if got:
                return got, d.get("total_cost_usd") or 0
        return None, d.get("total_cost_usd") or 0

    def score(self, answers, conds):
        """戻り値: (答えごとの {条件: 判定}, 費用)。採点できない答えがあれば最初の要素は None。"""
        cost = 0.0
        with ThreadPoolExecutor(max_workers=8) as ex:
            pairs = list(ex.map(lambda a: [self.once(a, conds), self.once(a, conds)], answers))
        final = []
        for a, ((v1, c1), (v2, c2)) in zip(answers, pairs):
            cost += c1 + c2
            if v1 is None or v2 is None:
                return None, cost
            split = [k for k in conds if v1[k] != v2[k]]
            v3 = {}
            if split:
                v3, c3 = self.once(a, conds)
                cost += c3
                if v3 is None:
                    return None, cost
            final.append({k: (majority([v1[k], v2[k], v3[k]]) if k in split else v1[k]) for k in conds})
        return final, cost


class ClaudeEvaluator:
    """本物の評価役。設定の suite の事例を、作業用フォルダの run.py で mech として流し、採点・計測する。

    judge つきの事例は条件ごとに [合格, 採点した回数, 流した回数]、機械判定の事例は通常タスクとして、正答・費用・長さを返す。
    1回でも答えが失敗(利用上限など)なら ok=False(評価不能)。
    """

    def __init__(self, config, out_root):
        self.suite = config["suite"]
        self.runs = config.get("runs", MIN_RUNS)
        self.judge_model = config.get("judge_model", "sonnet")
        self.out_root = out_root

    def evaluate(self, worktree, iteration):
        res = {"ok": True, "cases": {}, "normal": {}, "cost_usd": 0.0, "dirs": [], "costs": {}}
        judge = Judge(worktree, self.judge_model)
        fp = fingerprint(worktree)
        for item in self.suite:
            case = str(item["case"])
            out = os.path.join(self.out_root, f"E{iteration:03d}-c{case}")
            done = os.path.join(out, "case_result.json")
            if os.path.exists(done):
                # 前の実行で、同じ版のこの事例を流し終えていれば使い回す(利用上限で止まった後の再開で、費用を重ねない)
                with open(done, encoding="utf-8") as f:
                    prev = json.load(f)
                if prev.get("fingerprint") == fp:
                    res["cases"].update(prev["cases"])
                    res["normal"].update(prev["normal"])
                    res["dirs"].append(out)
                    res["costs"][f"事例{case}"] = 0.0
                    res.setdefault("reused", []).append(f"事例{case}")
                    continue
            shutil.rmtree(out, ignore_errors=True)
            os.makedirs(out)
            before = (dict(res["cases"]), dict(res["normal"]))
            with open(os.path.join(out, "plan.md"), "w", encoding="utf-8") as f:
                f.write(f"測りたいもの: 自己改善ループの反復 {iteration} の評価(0 は変更前の基準値)\n"
                        f"使う条件: 事例{case} の全条件(mech、{self.runs} 回)\n"
                        f"費用の見込み: ループの設定の evaluate_cost の一部。上限は improve_loop.py の --budget\n"
                        f"結果で決めること: improve_loop.py の decide の決まりで採否を決める\n")
            p = subprocess.run([sys.executable, os.path.join(worktree, RUN_PY), case, str(self.runs), out, "mech"],
                               cwd=worktree, capture_output=True, text=True, timeout=3600, stdin=subprocess.DEVNULL)
            runs = em.collect(out, case, cases_path=os.path.join(worktree, CASES_MD)).get("mech", [])
            spent = sum(r["cost"] for r in runs)
            res["dirs"].append(out)
            bad = [r["name"] for r in runs if r["error"]]
            if p.returncode or len(runs) < self.runs or bad:
                res["cost_usd"] += spent
                res.update(ok=False, why=f"事例{case}: 終了コード {p.returncode}、答え {len(runs)}/{self.runs}、失敗 {bad} {p.stderr[-300:]}")
                return res
            if item.get("judge"):
                conds = case_conditions(worktree, case)
                scored, jcost = judge.score([em.answer_text(os.path.join(out, r["name"])) for r in runs], conds)
                spent += jcost
                if scored is None:
                    res["cost_usd"] += spent
                    res.update(ok=False, why=f"事例{case}: judge の表が読めなかった")
                    return res
                with open(os.path.join(out, "judged.json"), "w", encoding="utf-8") as f:
                    json.dump({r["name"]: s for r, s in zip(runs, scored)}, f, ensure_ascii=False, indent=1)
                for k in conds:
                    vs = [s[k] for s in scored]
                    res["cases"][f"事例{case}-{k}"] = [vs.count("合格"), vs.count("合格") + vs.count("不合格"), len(runs)]
            else:
                a = em.aggregate(runs)
                res["normal"][f"事例{case}-1"] = {"passed": a["passed"], "n": a["judged"], "cost": round(a["cost"], 4),
                                                 "chars": a["chars"]}
            res["costs"][f"事例{case}"] = round(spent, 4)
            res["cost_usd"] += spent
            with open(done, "w", encoding="utf-8") as f:
                json.dump({"fingerprint": fp, "cost_usd": round(spent, 4),
                           "cases": {k: v for k, v in res["cases"].items() if k not in before[0]},
                           "normal": {k: v for k, v in res["normal"].items() if k not in before[1]}},
                          f, ensure_ascii=False, indent=1)
        res["cost_usd"] = round(res["cost_usd"], 4)
        return res


# ---------- ループ ----------

def changed_files(worktree):
    # git() は前後の空白を削るので使わない。" M CLAUDE.md" の頭の空白が消えると、1文字目が欠けて
    # 評価の基準のファイルの判定をすり抜ける(2026-10-10、履歴に "LAUDE.md" と残った)
    out = subprocess.run(["git", "-C", worktree, "status", "--porcelain", "-z", "--untracked-files=all"],
                         capture_output=True, text=True, check=True).stdout
    return [e[3:] for e in out.split("\0") if len(e) > 3]


def touches_eval(files):
    return [f for f in files if f.startswith(EVAL_PATHS)]


def fingerprint(worktree):
    """評価する版の印。コミットと、作業用フォルダの差分(実装役の変更)から作る。"""
    import hashlib
    return hashlib.sha1((git(worktree, "rev-parse", "HEAD") + git(worktree, "diff")).encode()).hexdigest()


def new_worktree(repo, commit, label):
    path = tempfile.mkdtemp(prefix=f"improve-{label}-")
    os.rmdir(path)
    git(repo, "worktree", "add", "-q", "--detach", path, commit)
    return path


def causes_of(repo, commit, mid):
    text = git(repo, "show", f"{commit}:{CAUSES_MD}", check=False)
    m = re.search(rf"^### {mid} [^\n]*\n(?:(?!^### )[^\n]*\n)*?- 原因:([^\n]*)", text, re.M)
    return re.findall(r"C\d+", m.group(1)) if m else []


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
    real = bool(getattr(args, "config", None))
    path = args.config if real else getattr(args, "scenario", None)
    if path:
        with open(path, encoding="utf-8") as f:
            scenario = json.load(f)
    if real:
        implementer = implementer or ClaudeImplementer(scenario)
        evaluator = evaluator or ClaudeEvaluator(scenario, scenario.get("runs_dir") or os.path.join(args.state, "runs"))
    implementer = implementer or FakeImplementer(scenario)
    evaluator = evaluator or FakeEvaluator(scenario)
    # 変更前の評価と、すべての反復の作業用フォルダを、同じコミットから作る(作業中の編集で条件を変えない)
    state.setdefault("base_commit", None)
    state["base_commit"] = state["base_commit"] or git(args.repo, "rev-parse", "HEAD")
    candidates = scenario.get("candidates", [])
    if real:
        candidates = [{**c, "causes": c.get("causes") or causes_of(args.repo, state["base_commit"], c["id"])}
                      for c in candidates]
    start = time.time()
    state["stopped"] = None
    if state.get("baseline_worktree"):
        cleanup(args.repo, state.pop("baseline_worktree"))

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
        state["baseline_worktree"] = new_worktree(args.repo, state["base_commit"], "base")
        store.save_state(state)
        base = evaluator.evaluate(state["baseline_worktree"], 0)
        cleanup(args.repo, state.pop("baseline_worktree"))
        state["spent_usd"] = round(state["spent_usd"] + base.get("cost_usd", 0), 4)
        if not base.get("ok"):
            state["stopped"] = "変更前の評価(基準値)がそろわない。評価環境を直すまで止める" + (f"({base['why']})" if base.get("why") else "")
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
        worktree = new_worktree(args.repo, state["base_commit"], str(it))
        state["in_progress"] = {"iteration": it, "target": cand["id"], "kind": kind, "causes": cand.get("causes", []),
                                "worktree": worktree, "time": now()}
        store.save_state(state)

        impl = implementer.implement(worktree, cand, kind, [e for e in history if e["episode_id"] in seen], it)
        files = changed_files(worktree)
        episode = {"episode_id": f"E{it:03d}", "iteration": it, "target": cand["id"], "kind": kind,
                   "causes": cand.get("causes", []), "why_chosen": why, "history_seen": seen,
                   "hypothesis": impl.get("hypothesis"), "why_different": impl.get("why_different"),
                   "base_commit": state["base_commit"], "changed_files": files,
                   "diff": git(worktree, "diff"), "baseline": state["baseline"], "time": now()}
        cost = impl.get("cost_usd", 0)
        bad = touches_eval(files)
        if not files:
            decision, reason, result = "棄却", "実装役が何も変えなかった" + (f"({impl['note']})" if impl.get("note") else ""), None
        elif bad:
            decision, reason, result = "棄却", "評価の基準のファイルを変えた: " + "、".join(bad), None
        else:
            result = evaluator.evaluate(worktree, it)
            cost += result.get("cost_usd", 0)
            decision, reason = decide(kind, state["baseline"], result)
            if decision == "評価不能" and result.get("why"):
                reason += f"({result['why']})"
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
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--scenario", help="偽の実装役・評価役の結果を決める JSON(制御の試験用)")
    g.add_argument("--config", help="本物の設定の JSON(candidates・suite・runs・implement_cost・evaluate_cost など)。費用がかかる")
    s = sub.add_parser("status")
    s.add_argument("--state", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "status":
        status(args)
        return 0
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
