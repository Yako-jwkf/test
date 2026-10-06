#!/usr/bin/env python3
"""scripts/mistake_check.py の試験。python3 scripts/test_mistake_check.py で動く。

- 読み取り・判定の決まり・判定結果の書き換えを、一時フォルダの小さな置き場と原因の表で試す。
- 擬似解消関数が「直ったら合格に変わる」ことを、フックの写しを一時フォルダで直して試す(本物のフックは変えない)。
- 本物のリポジトリで、書き方の誤りがないことと、本物の状態ファイルに触れないことを試す。
"""
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mistake_check as mc  # noqa: E402

INBOX_HEAD = "# 置き場\n\n| 日付 | 何が起きたか | 見つけ方 | 対策 | 原因 |\n|---|---|---|---|---|\n"


def cause(cid, **kw):
    base = {"id": cid, "name": "試験", "原因の推測": "", "対策": "", "対策日": "", "擬似解消関数": "", "最後の再テスト": ""}
    base.update(kw)
    return base


def event(date, causes):
    return {"date": date, "what": "x", "found": "x", "measure": "x", "causes": causes}


class ReadTest(unittest.TestCase):
    def test_split_top_ignores_separator_inside_parentheses(self):
        self.assertEqual(mc.split_top("なし(a、b)、振る舞い:x"), ["なし(a、b)", "振る舞い:x"])

    def test_table_cells_keeps_escaped_pipe(self):
        cells = mc.table_cells("| 2026-10-02 | `grep '^### \\|^#### '` を止めた | フック | 仕組み化 | C16 |")
        self.assertEqual(len(cells), 5)
        self.assertEqual(cells[4], "C16")

    def test_row_without_cause_column_is_unclassified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "inbox.md")
            mc.write_text(path, INBOX_HEAD + "| 2026-10-03 | 古い形の行 | 自分 | 未処理 |\n")
            self.assertEqual(mc.parse_inbox(path)[0]["causes"], [mc.UNCLASSIFIED])


class JudgeRuleTest(unittest.TestCase):
    """mistake-causes.md の「解消の状態の決め方」の表どおりに判定するか。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        mc.write_text(os.path.join(self.tmp, "rule.md"), "対策の文がある")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def status(self, c, events=()):
        return mc.judge(c, list(events), self.tmp)["status"]

    def test_postponed(self):
        self.assertEqual(self.status(cause("C01", 対策="見送り(理由)")), "見送り")

    def test_no_measure(self):
        self.assertEqual(self.status(cause("C01", 対策="なし")), "未対策")

    def test_recurrence_after_measure_date(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="残存:rule.md:対策の文")
        self.assertEqual(self.status(c, [event("2026-10-03", ["C01"])]), "再発あり")

    def test_same_day_event_is_not_recurrence(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="残存:rule.md:対策の文")
        self.assertEqual(self.status(c, [event("2026-10-02", ["C01"])]), "合格(残存のみ)")

    def test_other_cause_events_do_not_count(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02")
        self.assertEqual(self.status(c, [event("2026-10-05", ["C02"])]), "未判定")

    def test_retest_below_threshold_fails(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="再テスト:事例2-5>=3/3",
                  最後の再テスト="事例2-5=1/3(2026-10-02)")
        self.assertEqual(self.status(c), "不合格")

    def test_retest_pass_is_provisional_pass(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="再テスト:事例2-1>=3/3、残存:rule.md:対策の文",
                  最後の再テスト="事例2-1=3/3(2026-10-02)")
        self.assertEqual(self.status(c), "合格(暫定)")

    def test_unmeasured_retest_is_not_a_pass(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="再テスト:事例9-1>=3/3")
        self.assertEqual(self.status(c), "未判定")

    def test_missing_rule_text_fails(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="残存:rule.md:消えた文")
        self.assertEqual(self.status(c), "不合格")

    def test_unknown_behavior_fails(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="振る舞い:ない関数")
        self.assertEqual(self.status(c), "不合格")

    def test_structure_errors(self):
        errs = mc.structure_errors([event("2026-10-03", [mc.UNCLASSIFIED]), event("2026-10-03", ["C99"]),
                                    event("2026-10-03", [mc.NOT_A_MISTAKE])], [cause("C01")])
        self.assertEqual(len(errs), 2)


class WriteTest(unittest.TestCase):
    def test_write_replaces_only_the_result_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "docs", "knowledge"))
            mc.write_text(os.path.join(tmp, mc.INBOX), INBOX_HEAD + "| 2026-10-03 | a | 自分 | 未処理 | C01 |\n")
            body = ("# 表\n\n### C01 試験\n- 対策: なし\n- 対策日:\n- 擬似解消関数: なし(試験)\n\n## 判定結果\n"
                    f"{mc.BEGIN}\n古い結果\n{mc.END}\n\n後ろの文\n")
            path = os.path.join(tmp, mc.CAUSES)
            mc.write_text(path, body)
            with open(os.devnull, "w") as null:
                stdout, sys.stdout = sys.stdout, null
                try:
                    self.assertEqual(mc.main(["--root", tmp, "--write"]), 0)
                    first = mc.read_text(path)
                    mc.main(["--root", tmp, "--write"])
                finally:
                    sys.stdout = stdout
            second = mc.read_text(path)
            self.assertNotIn("古い結果", first)
            self.assertIn("**未対策**", first)
            self.assertTrue(first.endswith("後ろの文\n"))
            self.assertEqual(first.count(mc.BEGIN), 1)
            self.assertEqual(re.sub(r"判定日: \S+", "", first), re.sub(r"判定日: \S+", "", second))


class BehaviorTest(unittest.TestCase):
    """擬似解消関数が、フックの良し悪しで結果を変えるか。直した今のフックで合格し、写しを直す前の形に戻すと不合格になる。"""

    def setUp(self):
        self.copy = tempfile.mkdtemp()
        shutil.copytree(os.path.join(mc.ROOT, ".claude", "hooks"), os.path.join(self.copy, ".claude", "hooks"))
        shutil.copy(os.path.join(mc.ROOT, ".claude", "settings.json"), os.path.join(self.copy, ".claude", "settings.json"))

    def tearDown(self):
        shutil.rmtree(self.copy)

    def replace_line(self, prefix, line):
        """写しの audit-stop.py で、prefix で始まる行を line に置き換える。"""
        path = os.path.join(self.copy, ".claude", "hooks", "audit-stop.py")
        lines = mc.read_text(path).split("\n")
        idx = [i for i, l in enumerate(lines) if l.startswith(prefix)]
        self.assertEqual(len(idx), 1)
        lines[idx[0]] = line
        mc.write_text(path, "\n".join(lines))

    def test_judge_words_fails_when_fix_is_reverted(self):
        self.assertTrue(mc.b_judge_words(mc.ROOT, {}, [])[0])
        # 2026-10-06 に直す前の形(「本人は」の「人は」でも止める)に、写しだけを戻す
        self.replace_line("SUBJECT = ", 'SUBJECT = re.compile(r"(人間|人は|人も|人々|誰もが|多くの人|普通の人)")')
        self.assertFalse(mc.b_judge_words(self.copy, {}, [])[0])

    def test_audit_opinion_fails_when_fix_is_reverted(self):
        self.assertTrue(mc.b_audit_opinion(mc.ROOT, {}, [])[0])
        # 2026-10-06 に直す前の形(「報告」の語があるだけで断りとみなす)に、写しだけを戻す
        self.replace_line("LABEL = ", 'LABEL = re.compile(r"(私の意見|推測|と思います|と考えます|かもしれません|報告|未確認|確かめていません)")')
        self.assertFalse(mc.b_audit_opinion(self.copy, {}, [])[0])

    def test_turn_simulation_detects_blocks_at_all(self):
        # 何も止めない台本なら、止まるべき試験が全部通ってしまう。止まる側が本当に止まるかを見る
        self.assertTrue(mc.simulate_turn(mc.ROOT, "それは違う、やり直して", "直しました。", True, False))
        self.assertFalse(mc.simulate_turn(mc.ROOT, "続けて", "直しました。", True, False))

    def test_guard_must_block_cases_still_block(self):
        must_block = [c for c in mc.GUARD_CASES if c[3] == "止める"]
        self.assertGreaterEqual(len(must_block), 4)
        ok, detail = mc.b_guard_cases(mc.ROOT, {}, [])
        for name, *_ in must_block:
            self.assertNotIn(name, detail)


class RealRepoTest(unittest.TestCase):
    def test_no_structure_errors(self):
        events = mc.parse_inbox(os.path.join(mc.ROOT, mc.INBOX))
        causes = mc.parse_causes(os.path.join(mc.ROOT, mc.CAUSES))
        self.assertGreater(len(events), 40)
        self.assertGreater(len(causes), 20)
        self.assertEqual(mc.structure_errors(events, causes), [])

    def test_session_start_check_does_not_recurse(self):
        # 会話開始のフックが判定の台本を呼び、台本がフックを呼ぶ輪で 60 秒止まった(2026-10-06)
        import time
        start = time.time()
        ok, detail = mc.b_session_start_hook(mc.ROOT, {}, [])
        self.assertTrue(ok, detail)
        self.assertLess(time.time() - start, 20)

    def test_does_not_touch_real_state_files(self):
        watched = [os.path.join(mc.ROOT, ".claude", "reasoning", f) for f in ("turn.json", "turn-state.json", "state.md")]
        watched.append(os.path.join(mc.ROOT, mc.INBOX))
        before = {p: os.path.getmtime(p) for p in watched if os.path.exists(p)}
        mc.b_inbox_guard(mc.ROOT, {}, [])
        mc.b_state_guard(mc.ROOT, {}, [])
        after = {p: os.path.getmtime(p) for p in watched if os.path.exists(p)}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
