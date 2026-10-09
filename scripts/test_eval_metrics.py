#!/usr/bin/env python3
"""scripts/eval_metrics.py の試験。python3 scripts/test_eval_metrics.py で動く。

run.py の出力の形(<条件>-<回>/turnN.json・answers.md)と会話の記録(.jsonl)を一時フォルダに作り、数え方と比べ方を試す。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_metrics as em  # noqa: E402

CASES = ("# 事例\n\n## 事例9\n\n- **機械判定**: 含む:focus-tracking-design.md\n\n"
         "## 事例12\n\n- **機械判定**: 文字数<=20、道具なし:Edit|Write\n\n## 事例13\n\n- **良い答えの条件**: x\n")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cases = os.path.join(self.tmp, "cases.md")
        em_write(self.cases, CASES)
        self.tr = os.path.join(self.tmp, "projects", "ws")
        os.makedirs(self.tr)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_dir(self, out, name, answer, cost=0.2, tools=(), blocks=0, error=False):
        d = os.path.join(self.tmp, out, name)
        os.makedirs(d)
        sid = f"{out}-{name}"
        em_write(os.path.join(d, "turn1.json"), json.dumps({"total_cost_usd": cost, "duration_ms": 1000, "num_turns": 1,
                                                             "session_id": sid, "is_error": error}))
        em_write(os.path.join(d, "answers.md"), f"## ターン1\n\n{answer}\n\n")
        lines = [{"type": "user", "message": {"content": "質問"}}]
        lines += [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": t}]}} for t in tools]
        lines += [{"type": "user", "message": {"content": "Stop hook feedback: x"}}] * blocks
        em_write(os.path.join(self.tr, f"{sid}.jsonl"), "\n".join(json.dumps(x, ensure_ascii=False) for x in lines))
        return d


def em_write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class RuleTest(Fixture):
    def test_load_rules(self):
        self.assertEqual(em.load_rules(9, self.cases), ["含む:focus-tracking-design.md"])
        self.assertEqual(em.load_rules(12, self.cases), ["文字数<=20", "道具なし:Edit|Write"])
        self.assertEqual(em.load_rules(13, self.cases), [])

    def test_check_rules(self):
        self.assertEqual(em.check_rules(["含む:a"], "xa", {}), (True, []))
        self.assertEqual(em.check_rules(["文字数<=3"], "abcd", {}), (False, ["文字数<=3"]))
        self.assertEqual(em.check_rules(["道具なし:Edit|Write"], "", {"Write": 1}), (False, ["道具なし:Edit|Write"]))
        self.assertEqual(em.check_rules([], "x", {}), (None, []))
        self.assertFalse(em.check_rules(["ない形:x"], "x", {})[0])


class MetricsTest(Fixture):
    def test_run_metrics_counts_tools_blocks_and_answer(self):
        d = self.run_dir("out", "mech-1", "答えは focus-tracking-design.md", tools=("Read", "WebSearch", "Read"), blocks=1)
        r = em.run_metrics(d, em.load_rules(9, self.cases), os.path.dirname(self.tr))
        self.assertTrue(r["pass"])
        self.assertEqual(r["tool_calls"], 3)
        self.assertEqual(r["web_calls"], 1)
        self.assertEqual(r["hook_blocks"], 1)
        self.assertEqual(r["chars"], len("答えは focus-tracking-design.md"))
        self.assertTrue(r["transcript"])

    def test_failed_run_is_not_judged(self):
        self.run_dir("out", "mech-1", "(失敗: limit)")
        self.run_dir("out", "mech-2", "focus-tracking-design.md")
        a = em.aggregate(em.collect(os.path.join(self.tmp, "out"), 9, self.cases, os.path.dirname(self.tr))["mech"])
        self.assertEqual((a["n"], a["errors"], a["passed"], a["judged"]), (2, 1, 1, 1))


class CompareTest(Fixture):
    def groups(self, out, answers, cost=0.2, tools=()):
        for i, ans in enumerate(answers, 1):
            self.run_dir(out, f"mech-{i}", ans, cost=cost, tools=tools)
        return em.aggregate(em.collect(os.path.join(self.tmp, out), 9, self.cases, os.path.dirname(self.tr))["mech"])

    def test_improvement_needs_enough_runs_and_margin(self):
        good, bad = "focus-tracking-design.md", "わかりません"
        before = self.groups("b", [bad] * 4 + [good])
        after = self.groups("a", [good] * 4 + [bad])
        self.assertIn("改善", em.verdicts(before, after)[0])

    def test_small_difference_is_noise(self):
        good, bad = "focus-tracking-design.md", "わかりません"
        before = self.groups("b", [good] * 3 + [bad] * 2)
        after = self.groups("a", [good] * 4 + [bad])
        self.assertIn("差なし", em.verdicts(before, after)[0])

    def test_too_few_runs_is_on_hold(self):
        before = self.groups("b", ["わかりません"] * 3)
        after = self.groups("a", ["focus-tracking-design.md"] * 3)
        self.assertIn("判定保留", em.verdicts(before, after)[0])

    def test_cost_and_search_increase_are_flagged(self):
        before = self.groups("b", ["focus-tracking-design.md"] * 5, cost=0.2)
        after = self.groups("a", ["focus-tracking-design.md"] * 5, cost=0.4, tools=("WebSearch",))
        v = " ".join(em.verdicts(before, after))
        self.assertIn("費用: 悪化の疑い", v)
        self.assertIn("検索: 悪化の疑い", v)

    def test_tool_call_increase_is_flagged(self):
        before = self.groups("b", ["focus-tracking-design.md"] * 5, tools=("Read",))
        after = self.groups("a", ["focus-tracking-design.md"] * 5, tools=("Read", "Read", "Glob"))
        self.assertIn("道具の回数: 悪化の疑い", " ".join(em.verdicts(before, after)))


class RealCasesTest(unittest.TestCase):
    def test_normal_task_cases_have_machine_rules(self):
        # 通常タスクの事例9〜12は、judge を使わず機械判定で採点する(effect-evaluation.md)
        for n in (9, 10, 11, 12):
            self.assertTrue(em.load_rules(n), f"事例{n} に機械判定がない")


if __name__ == "__main__":
    unittest.main(verbosity=2)
