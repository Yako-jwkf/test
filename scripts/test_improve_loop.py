#!/usr/bin/env python3
"""scripts/improve_loop.py の試験。python3 scripts/test_improve_loop.py で動く。

一時フォルダに小さな git リポジトリを作り、偽の実装役・評価役(scenario で結果を決める)でループを回す。
本物のリポジトリと `claude -p` は使わない(費用なし)。確かめること(依頼にあった7項目):
1. ループが実際に複数回の反復をする  2. 前の反復の結果が次の選択に使われる  3. 採用候補・棄却・撤回・評価不能の分岐
4. 失敗した変更を撤回できる(作業用フォルダを片付け、元のリポジトリは変わらない)  5. 停止条件(回数・予算・候補なし・評価不能)
6. 中断の後に再開できる  7. 既存の試験は別に流す(CI)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import improve_loop as il  # noqa: E402

CLAUDE_MD = "# 試験\n- 本題の行(M01 の印)\n- 選択肢の行(M10 の印)\n"
CANDIDATES = [
    {"id": "M01", "causes": ["C01"], "file": "CLAUDE.md", "marker": "M01 の印", "kinds": ["外す", "足す"]},
    {"id": "M10", "causes": ["C09"], "file": "CLAUDE.md", "marker": "M10 の印", "kinds": ["外す"]},
]


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class LoopFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "docs", "knowledge"))
        with open(os.path.join(self.repo, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write(CLAUDE_MD)
        with open(os.path.join(self.repo, "docs", "knowledge", "mistake-cases.md"), "w", encoding="utf-8") as f:
            f.write("# 事例\n")
        sh(self.repo, "git", "init", "-q")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        self.state = os.path.join(self.tmp, "state")
        self.lines = []

    def tearDown(self):
        subprocess.run(["git", "-C", self.repo, "worktree", "prune"], capture_output=True)
        shutil.rmtree(self.tmp)

    def run_loop(self, scenario, max_iter=3, budget=100.0):
        path = os.path.join(self.tmp, "scenario.json")
        scenario = {"candidates": CANDIDATES, **scenario}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scenario, f, ensure_ascii=False)
        args = SimpleNamespace(state=self.state, repo=self.repo, max_iter=max_iter, budget=budget,
                               timeout_min=10.0, scenario=path)
        return il.run(args, out=self.lines.append)

    def history(self):
        return il.Store(self.state).history()

    def worktrees(self):
        return [l for l in sh(self.repo, "git", "worktree", "list").split("\n") if l.strip()]


class LoopTest(LoopFixture):
    def test_iterates_and_uses_history(self):
        # 反復1: M01 を外す → 悪化で撤回 / 反復2: M01 は「外す」を試したので「足す」→ 差なしで棄却 / 反復3: M10 を外す → 採用候補
        state = self.run_loop({"outcomes": {"1": "worse", "2": "same", "3": "improve"}})
        h = self.history()
        self.assertEqual([e["iteration"] for e in h], [1, 2, 3])
        self.assertEqual([(e["target"], e["kind"], e["decision"]) for e in h],
                         [("M01", "外す", "撤回"), ("M01", "足す", "棄却"), ("M10", "外す", "採用候補")])
        self.assertIn("E001", h[1]["history_seen"])      # 前の反復の結果を見て選んだ
        self.assertNotIn("E001", h[2]["history_seen"])   # 別の対象・原因の履歴は混ぜない
        self.assertIn("反復の上限 3", state["stopped"])
        self.assertEqual(state["iteration"], 3)

    def test_every_iteration_records_evaluation(self):
        self.run_loop({"outcomes": {"1": "improve", "2": "same"}}, max_iter=2)
        for e in self.history():
            self.assertTrue(e["candidate_result"]["ok"])           # 評価を実際に流した結果がある
            self.assertTrue(e["baseline"]["ok"])
            self.assertTrue(e["diff"])                            # 変更の差分を残した

    def test_rollback_keeps_main_repo_unchanged(self):
        before = sh(self.repo, "git", "rev-parse", "HEAD")
        self.run_loop({"outcomes": {"1": "worse", "2": "side_effect", "3": "improve"}})
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")
        self.assertEqual(sh(self.repo, "git", "rev-parse", "HEAD"), before)
        self.assertEqual(len(self.worktrees()), 1)                # 作業用フォルダは片付いた
        branches = sh(self.repo, "git", "branch", "--list", "improve/*")
        h = self.history()
        self.assertEqual([e["decision"] for e in h], ["撤回", "撤回", "採用候補"])
        self.assertIn("費用 2.00倍", h[1]["reason"])               # 副作用(通常タスクの費用)で撤回
        self.assertIn(h[2]["branch"], branches)                   # 採用候補だけブランチに残す(main には入れない)
        self.assertNotIn("E001", branches)

    def test_broken_evaluation_stops_without_success(self):
        state = self.run_loop({"outcomes": {"1": "broken"}})
        h = self.history()
        self.assertEqual(h[0]["decision"], "評価不能")
        self.assertIn("評価不能", state["stopped"])
        self.assertEqual(len(h), 1)

    def test_budget_and_candidates_stop_the_loop(self):
        state = self.run_loop({"evaluate_cost": 1.0, "implement_cost": 0.5}, budget=2.0)
        self.assertEqual(len(self.history()), 0)                  # 基準値で 1.0 使い、次の反復の見込み 1.5 が予算を超える
        self.assertIn("予算", state["stopped"])
        shutil.rmtree(self.state)
        state = self.run_loop({"outcomes": {}}, max_iter=10)
        self.assertEqual(len(self.history()), 3)                  # (M01,外す)(M01,足す)(M10,外す) の3組で尽きる
        self.assertIn("試していない", state["stopped"])

    def test_resume_after_interruption(self):
        with self.assertRaises(RuntimeError):
            self.run_loop({"crash_after_implement": 1, "outcomes": {"2": "improve"}})
        state = il.Store(self.state).load_state()
        self.assertEqual(state["in_progress"]["iteration"], 1)
        self.assertEqual(len(self.worktrees()), 2)                # 中断した反復の作業用フォルダが残っている
        state = self.run_loop({"outcomes": {"2": "improve"}}, max_iter=2)
        h = self.history()
        self.assertEqual([e["decision"] for e in h], ["中断", "採用候補"])
        self.assertEqual([e["episode_id"] for e in h], ["E001", "E002"])   # 番号を重ねない
        self.assertEqual((h[1]["target"], h[1]["kind"]), ("M01", "外す"))  # 中断は案の結果ではないので、もう一度試す
        self.assertIsNone(state["in_progress"])
        self.assertEqual(len(self.worktrees()), 1)
        self.assertEqual(state["iteration"], 2)

    def test_changes_to_evaluation_files_are_rejected(self):
        self.run_loop({"outcomes": {"1": "improve"}, "touch_eval_files": {"1": ["docs/knowledge/mistake-cases.md"]}},
                      max_iter=1)
        e = self.history()[0]
        self.assertEqual(e["decision"], "棄却")
        self.assertIn("評価の基準", e["reason"])
        self.assertIsNone(e["candidate_result"])                  # 評価する前に棄却した

    def test_history_is_append_only_across_runs(self):
        self.run_loop({"outcomes": {"1": "same"}}, max_iter=1)
        first = open(il.Store(self.state).history_file, encoding="utf-8").read()
        self.run_loop({"outcomes": {"2": "same"}}, max_iter=2)
        second = open(il.Store(self.state).history_file, encoding="utf-8").read()
        self.assertTrue(second.startswith(first))


class DecideTest(unittest.TestCase):
    BASE = {"ok": True, "cases": {"a": [1, 5], "b": [3, 5]}, "normal": {"n": {"passed": 5, "n": 5, "cost": 0.2, "chars": 300}}}

    def cand(self, a=1, b=3, cost=0.2, n=5):
        return {"ok": True, "cases": {"a": [a, n], "b": [b, n]}, "normal": {"n": {"passed": 5, "n": n, "cost": cost, "chars": 300}}}

    def test_adding_needs_improvement(self):
        self.assertEqual(il.decide("足す", self.BASE, self.cand(a=4))[0], "採用候補")
        self.assertEqual(il.decide("足す", self.BASE, self.cand(a=2))[0], "棄却")

    def test_reducing_needs_only_no_harm(self):
        self.assertEqual(il.decide("外す", self.BASE, self.cand())[0], "採用候補")
        self.assertEqual(il.decide("外す", self.BASE, self.cand(b=1))[0], "撤回")

    def test_side_effect_and_broken(self):
        self.assertEqual(il.decide("足す", self.BASE, self.cand(a=4, cost=0.3))[0], "撤回")
        self.assertEqual(il.decide("足す", self.BASE, {"ok": False})[0], "評価不能")
        self.assertEqual(il.decide("足す", self.BASE, self.cand(a=4, n=3))[0], "評価不能")
        missing = {"ok": True, "cases": {"a": [4, 5]}, "normal": self.BASE["normal"]}
        self.assertEqual(il.decide("足す", self.BASE, missing)[0], "評価不能")

    def test_choose_does_not_repeat_failed_additions(self):
        h = [{"episode_id": f"E00{i}", "target": f"X{i}", "kind": "足す", "causes": ["C01"], "decision": "棄却"} for i in (1, 2)]
        cand = [{"id": "M01", "causes": ["C01"], "kinds": ["足す"]}]
        self.assertIsNone(il.choose(cand, h))                    # 同じ原因で足すが2回失敗したら、足すを出さない


class CliTest(unittest.TestCase):
    def test_real_components_are_not_implemented_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                il.main(["run", "--state", tmp])


class BudgetTest(LoopFixture):
    def test_no_budget_means_no_evaluation_at_all(self):
        # 予算 0 ドルでは、変更前の評価(本物なら費用がかかる)も流さずに止まる
        state = self.run_loop({"outcomes": {"1": "improve"}}, budget=0.0)
        self.assertIsNone(state["baseline"])
        self.assertEqual(state["spent_usd"], 0.0)
        self.assertEqual(self.history(), [])
        self.assertIn("予算", state["stopped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
