#!/usr/bin/env python3
"""scripts/mistake_check.py の試験。python3 scripts/test_mistake_check.py で動く。

- 読み取り・判定の決まり・判定結果の書き換えを、一時フォルダの小さな置き場と原因の表で試す。
- 「動く」「記録がある」「既知の失敗例に合格」を効果(ミスが減った)と取り違えないかを試す(2026-10-09、A1)。
- 対策ごとの状態(未評価・評価中・効果確認済み・効果不十分・副作用あり・撤回済み)と見直し候補(C1)を試す。
- 擬似解消関数が「直ったら合格に変わる」「フックが落ちたら不合格になる」ことを、フックの写しを一時フォルダで
  変えて試す(本物のフックは変えない)。
- 本物のリポジトリで、書き方の誤りがないことと、本物の状態ファイルに触れないことを試す。
"""
import datetime
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


def measure(mid="M01", **kw):
    base = {"id": mid, "name": "試験の対策", **{f: "" for f in mc.MEASURE_FIELDS}}
    base.update({"原因": "C01", "導入日": "2026-10-02"})
    base.update(kw)
    return base


def event(date, causes):
    return {"date": date, "what": "x", "found": "x", "measure": "x", "causes": causes}


ALL_KINDS = "既知:事例2-1>=2/3、未知:事例8-1>=4/5、通常:事例9-1>=4/5"
ALL_PASS = "既知:事例2-1=3/3(2026-10-03)、未知:事例8-1=5/5(2026-10-03)、通常:事例9-1=5/5(2026-10-03)"


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

    def test_parse_measures_reads_only_measure_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "causes.md")
            mc.write_text(path, "## 原因\n\n### C01 試験\n- 対策: x\n- 対策日: 2026-10-02\n\n## 対策の一覧\n\n"
                                "### M01 行\n- 原因: C01、C02\n- 導入日: 2026-10-02\n- 効果の基準: 既知:事例2-1>=2/3\n\n"
                                "## 判定結果\n- 導入日: 2099-01-01\n")
            ms = mc.parse_measures(path)
            self.assertEqual([m["id"] for m in ms], ["M01"])
            self.assertEqual(ms[0]["原因"], "C01、C02")
            self.assertEqual(ms[0]["導入日"], "2026-10-02")
            self.assertEqual([c["id"] for c in mc.parse_causes(path)], ["C01"])

    def test_parse_results_marks_old_form(self):
        rs, errs = mc.parse_results("既知:事例2-1=3/3(2026-10-02、前の形: 直す前)、未知:事例8-1=1/5(2026-10-10)")
        self.assertEqual(errs, [])
        self.assertTrue(rs[0]["old_form"])
        self.assertFalse(rs[1]["old_form"])


class JudgeRuleTest(unittest.TestCase):
    """mistake-causes.md の「原因の状態の決め方」の表どおりに判定するか。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        mc.write_text(os.path.join(self.tmp, "rule.md"), "対策の文がある")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def status(self, c, events=()):
        return mc.judge(c, list(events), self.tmp)["status"]

    def test_postponed(self):
        self.assertEqual(self.status(cause("C01", 対策="見送り(理由)")), mc.S_POSTPONED)

    def test_no_measure(self):
        self.assertEqual(self.status(cause("C01", 対策="なし")), mc.S_NO_MEASURE)

    def test_recurrence_after_measure_date(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="残存:rule.md:対策の文")
        self.assertEqual(self.status(c, [event("2026-10-03", ["C01"])]), mc.S_RECUR)

    def test_same_day_event_is_not_recurrence(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="残存:rule.md:対策の文")
        self.assertEqual(self.status(c, [event("2026-10-02", ["C01"])]), mc.S_TEXT_ONLY)

    def test_other_cause_events_do_not_count(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02")
        self.assertEqual(self.status(c, [event("2026-10-05", ["C02"])]), mc.S_UNEVALUATED)

    def test_retest_below_threshold_is_known_failure_not_broken(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="再テスト:事例2-5>=3/3",
                  最後の再テスト="事例2-5=1/3(2026-10-02)")
        self.assertEqual(self.status(c), mc.S_KNOWN_FAIL)

    def test_retest_pass_is_only_a_known_case_pass(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="再テスト:事例2-1>=3/3、残存:rule.md:対策の文",
                  最後の再テスト="事例2-1=3/3(2026-10-02)")
        self.assertEqual(self.status(c), mc.S_KNOWN_PASS)

    def test_retest_before_measure_date_is_not_used(self):
        # C04・C06 は対策日 2026-10-06 なのに 10-02 の再テストで判定されていた(2026-10-09)
        c = cause("C01", 対策="ルール", 対策日="2026-10-06", 擬似解消関数="再テスト:事例2-1>=3/3",
                  最後の再テスト="事例2-1=3/3(2026-10-02)")
        r = mc.judge(c, [], self.tmp)
        self.assertEqual(r["status"], mc.S_UNEVALUATED)
        self.assertIn("古い", r["checks"][0][2])

    def test_unmeasured_retest_is_not_a_pass(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="再テスト:事例9-1>=3/3")
        self.assertEqual(self.status(c), mc.S_UNEVALUATED)

    def test_missing_rule_text_is_broken(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="残存:rule.md:消えた文")
        self.assertEqual(self.status(c), mc.S_BROKEN)

    def test_unknown_behavior_is_broken(self):
        c = cause("C01", 対策="ルール", 対策日="2026-10-02", 擬似解消関数="振る舞い:ない関数")
        self.assertEqual(self.status(c), mc.S_BROKEN)

    def test_no_status_name_claims_an_effect(self):
        # 原因の状態には「合格」「解消」の語を使わない。効果を確かめるのは対策ごとの判定だけ(A1)
        names = [mc.S_KNOWN_PASS, mc.S_RUNS_ONLY, mc.S_TEXT_ONLY, mc.S_UNEVALUATED]
        for n in names:
            self.assertNotIn("解消", n)
        self.assertIn("未評価", mc.S_RUNS_ONLY)
        self.assertIn("未評価", mc.S_TEXT_ONLY)

    def test_structure_errors(self):
        errs = mc.structure_errors([event("2026-10-03", [mc.UNCLASSIFIED]), event("2026-10-03", ["C99"]),
                                    event("2026-10-03", [mc.NOT_A_MISTAKE])], [cause("C01")])
        self.assertEqual(len(errs), 2)

    def test_structure_errors_for_measures(self):
        bad = [measure(原因="C99"), measure("M02", 導入日="10月2日"), measure("M03", 効果の基準="既知:事例2>=2/3"),
               measure("M04", 効果の結果="既知:事例2-1=3/3"), measure("M05", 副作用="悪化した")]
        errs = mc.structure_errors([], [cause("C01")], bad)
        for mid in ("M01", "M02", "M03", "M04", "M05"):
            self.assertTrue(any(e.startswith(mid) for e in errs), mid)
        self.assertEqual(mc.structure_errors([], [cause("C01")], [measure(効果の基準=ALL_KINDS, 効果の結果=ALL_PASS)]), [])


class MeasureStateTest(unittest.TestCase):
    """対策ごとの状態。動作・再生・残存は効果に数えず、3種類の評価と本番の再発で決める。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        mc.write_text(os.path.join(self.tmp, "rule.md"), "対策の文がある")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def judge(self, m, events=()):
        return mc.judge_measure(m, list(events), self.tmp)

    def test_unevaluated_even_when_it_runs(self):
        r = self.judge(measure(動作確認="残存:rule.md:対策の文"))
        self.assertEqual(r["operation"], mc.OP_OK)
        self.assertEqual(r["state"], mc.M_UNEVALUATED)

    def test_broken_operation_is_separate_from_effect(self):
        r = self.judge(measure(動作確認="残存:rule.md:消えた文"))
        self.assertEqual(r["operation"], mc.OP_BROKEN)
        self.assertEqual(r["state"], mc.M_UNEVALUATED)

    def test_evaluating_when_some_kinds_are_missing(self):
        r = self.judge(measure(効果の基準=ALL_KINDS, 効果の結果="既知:事例2-1=3/3(2026-10-03)"))
        self.assertEqual(r["state"], mc.M_EVALUATING)

    def test_confirmed_needs_all_three_kinds(self):
        self.assertEqual(self.judge(measure(効果の基準=ALL_KINDS, 効果の結果=ALL_PASS))["state"], mc.M_CONFIRMED)
        only_known = measure(効果の基準="既知:事例2-1>=2/3", 効果の結果="既知:事例2-1=3/3(2026-10-03)")
        self.assertEqual(self.judge(only_known)["state"], mc.M_EVALUATING)
        no_unknown_case = measure(効果の基準="既知:事例2-1>=2/3、未知:なし(まだ事例がない)、通常:事例9-1>=4/5",
                                  効果の結果="既知:事例2-1=3/3(2026-10-03)、通常:事例9-1=5/5(2026-10-03)")
        self.assertEqual(self.judge(no_unknown_case)["state"], mc.M_EVALUATING)

    def test_recurrence_in_production_beats_passing_tests(self):
        m = measure(効果の基準=ALL_KINDS, 効果の結果=ALL_PASS)
        r = self.judge(m, [event("2026-10-05", ["C01"])])
        self.assertEqual(r["state"], mc.M_INSUFFICIENT)
        self.assertNotIn(mc.M_CONFIRMED, r["state"])

    def test_known_or_unknown_failure_is_insufficient(self):
        m = measure(効果の基準=ALL_KINDS, 効果の結果="既知:事例2-1=3/3(2026-10-03)、未知:事例8-1=1/5(2026-10-03)")
        self.assertEqual(self.judge(m)["state"], mc.M_INSUFFICIENT)

    def test_normal_task_failure_is_a_side_effect(self):
        m = measure(効果の基準=ALL_KINDS, 効果の結果="既知:事例2-1=3/3(2026-10-03)、通常:事例9-1=2/5(2026-10-03)")
        self.assertEqual(self.judge(m)["state"], mc.M_SIDE)

    def test_confirmed_side_effect_but_not_suspicion(self):
        self.assertEqual(self.judge(measure(副作用="確認: 通常タスクの費用が1.5倍"))["state"], mc.M_SIDE)
        r = self.judge(measure(副作用="疑い: 事例1-1 が下がった(前の形)"))
        self.assertEqual(r["state"], mc.M_UNEVALUATED)
        self.assertEqual(len(r["suspected"]), 1)

    def test_retracted_wins(self):
        r = self.judge(measure(撤回日="2026-10-09", 副作用="確認: x"), [event("2026-10-05", ["C01"])])
        self.assertEqual(r["state"], mc.M_RETRACTED)

    def test_old_form_and_pre_introduction_results_are_ignored(self):
        old = measure(効果の基準=ALL_KINDS, 効果の結果=ALL_PASS.replace("(2026-10-03)", "(2026-10-03、前の形: 直す前)"))
        self.assertEqual(self.judge(old)["state"], mc.M_UNEVALUATED)
        early = measure(導入日="2026-10-06", 効果の基準=ALL_KINDS, 効果の結果=ALL_PASS)
        r = self.judge(early)
        self.assertEqual(r["state"], mc.M_UNEVALUATED)
        self.assertTrue(all("今の形では未測定" in d for _, d, _ in r["evals"]))

    def test_recurrence_counts_only_target_causes_after_introduction(self):
        m = measure(原因="C01、C02")
        evs = [event("2026-10-02", ["C01"]), event("2026-10-03", ["C03"]), event("2026-10-04", ["C02"])]
        self.assertEqual(len(self.judge(m, evs)["recur"]), 1)


class ExtendedMeasureTest(unittest.TestCase):
    """比べる形の基準・決定の欄・未登録の原因から作る対策(2026-10-09)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def evals(self, m):
        return mc.judge_measure(m, [], self.tmp)["evals"]

    def test_relative_criterion_compares_with_control(self):
        crit = "未知:事例8-3>=+2/5"
        self.assertTrue(self.evals(measure(効果の基準=crit, 効果の結果="未知:事例8-3=4/5(2026-10-03、対照 1/5)"))[0][2])
        self.assertFalse(self.evals(measure(効果の基準=crit, 効果の結果="未知:事例8-3=5/5(2026-10-03、対照 5/5)"))[0][2])
        missing = self.evals(measure(効果の基準=crit, 効果の結果="未知:事例8-3=5/5(2026-10-03)"))[0]
        self.assertIsNone(missing[2])
        self.assertIn("対照の結果がない", missing[1])

    def test_non_inferiority_criterion(self):
        crit = "通常:事例11-1>=-1/5"
        self.assertTrue(self.evals(measure(効果の基準=crit, 効果の結果="通常:事例11-1=4/5(2026-10-03、対照 5/5)"))[0][2])
        self.assertFalse(self.evals(measure(効果の基準=crit, 効果の結果="通常:事例11-1=3/5(2026-10-03、対照 5/5)"))[0][2])

    def test_decision_is_validated_and_shown(self):
        self.assertEqual(mc.structure_errors([], [cause("C01")], [measure(決定="残す(理由、2026-10-09)")]), [])
        errs = mc.structure_errors([], [cause("C01")], [measure(決定="たぶん消す")])
        self.assertTrue(any("決定" in e for e in errs))
        r = mc.judge_measure(measure(決定="再設計(理由)"), [event("2026-10-05", ["C01"])], self.tmp)
        cands = mc.review_candidates([r], [], datetime.date(2026, 10, 9))
        self.assertIn("決定: 再設計(理由)", cands[0][1])

    def test_derived_measures_come_only_from_unregistered_causes(self):
        causes = [cause("C01", 対策="ルール", 対策日="2026-10-02"),
                  cause("C02", 対策="フック", 対策日="2026-10-06", 擬似解消関数="振る舞い:x、再テスト:事例2-6>=3/3",
                        最後の再テスト="事例2-6=2/3(2026-10-02)"),
                  cause("C03", 対策="見送り(理由)"), cause("C04", 対策="なし")]
        derived = mc.derived_measures(causes, [measure(原因="C01")])
        self.assertEqual([d["id"] for d in derived], ["C02*"])
        # 撤回済み(採らなかった介入)だけが付いた原因は、今の対策を作る
        with_retracted = mc.derived_measures(causes, [measure(原因="C01"), measure("M12", 原因="C02", 撤回日="2026-10-06")])
        self.assertEqual([d["id"] for d in with_retracted], ["C02*"])
        d = derived[0]
        self.assertEqual((d["導入日"], d["動作確認"], d["効果の基準"]), ("2026-10-06", "振る舞い:x", "既知:事例2-6>=3/3"))
        r = mc.judge_measure(d, [], self.tmp)
        self.assertIn("今の形では未測定", r["evals"][0][1])          # 対策日より前の再テストは使わない


class SameDayTest(unittest.TestCase):
    """同じ日の再発を、git で行を足した版に対策があったかを見て数える(2026-10-09)。"""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.repo, "docs", "knowledge"))
        self.git("init", "-q", "-b", "main")

    def tearDown(self):
        shutil.rmtree(self.repo)

    def git(self, *args, date="2026-10-02T10:00:00"):
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        import subprocess
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True, env=env)

    def commit(self, path, text, date="2026-10-02T10:00:00"):
        with open(os.path.join(self.repo, path), "a", encoding="utf-8") as f:
            f.write(text)
        self.git("add", "-A", date=date)
        self.git("commit", "-q", "-m", path, date=date)

    def test_counts_only_marked_rows_recorded_with_measure_in_place(self):
        self.commit("docs/knowledge/mistake-inbox.md", INBOX_HEAD)
        self.commit("rule.md", "選択肢の行\n")
        rows = ("| 2026-10-02 | 【前の行の再発。ルールがあっても起きた】2択で出した | 指摘 | 未処理 | C09 |\n"
                "| 2026-10-02 | 印のない同じ日の行 | 指摘 | 未処理 | C09 |\n")
        self.commit("docs/knowledge/mistake-inbox.md", rows)
        events = mc.parse_inbox(os.path.join(self.repo, mc.INBOX))
        got = mc.same_day_recurrences(self.repo, events, "2026-10-02", ["C09"], [("rule.md", "選択肢の行")], {})
        self.assertEqual([e["confirmed"] for e in got], [True, False])
        missing = mc.same_day_recurrences(self.repo, events, "2026-10-02", ["C09"], [("rule.md", "ない文")], {})
        self.assertEqual(missing, [])

    def test_row_recorded_later_is_not_confirmed(self):
        # 後の日の会話が足した行(言い換え・取り込み)は、起きた会話と別かもしれないので数えない
        self.commit("docs/knowledge/mistake-inbox.md", INBOX_HEAD)
        self.commit("rule.md", "選択肢の行\n")
        self.commit("docs/knowledge/mistake-inbox.md", "| 2026-10-02 | 【再発】後で足した行 | 指摘 | 未処理 | C09 |\n",
                    date="2026-10-06T10:00:00")
        events = mc.parse_inbox(os.path.join(self.repo, mc.INBOX))
        got = mc.same_day_recurrences(self.repo, events, "2026-10-02", ["C09"], [("rule.md", "選択肢の行")], {})
        self.assertEqual([e["confirmed"] for e in got], [False])

    def test_no_history_means_nothing_is_counted(self):
        self.commit("docs/knowledge/mistake-inbox.md", INBOX_HEAD + "| 2026-10-02 | 【再発】行 | 指摘 | 未処理 | C09 |\n")
        events = mc.parse_inbox(os.path.join(self.repo, mc.INBOX))
        self.assertEqual(mc.same_day_recurrences(self.repo, events, "2026-10-02", ["C09"], [("x", "y")], {}), [])


class ReviewTest(unittest.TestCase):
    """見直し候補(C1)。候補を出すだけで、何も消さない。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def names(self, measures, causes=(), events=(), today="2026-10-09"):
        mr = [mc.judge_measure(m, list(events), self.tmp) for m in measures]
        cr = [mc.judge(c, list(events), self.tmp) for c in causes]
        return [n for n, _ in mc.review_candidates(mr, cr, datetime.date.fromisoformat(today))]

    def test_insufficient_and_side_effects_are_candidates(self):
        got = self.names([measure("M01"), measure("M02", 副作用="確認: x"), measure("M03", 撤回日="2026-10-08")],
                         events=[event("2026-10-05", ["C01"])])
        self.assertTrue(any(n.startswith("M01") for n in got))
        self.assertTrue(any(n.startswith("M02") for n in got))
        self.assertFalse(any(n.startswith("M03") for n in got))

    def test_unevaluated_becomes_candidate_after_review_days(self):
        m = measure(導入日="2026-10-01")
        self.assertEqual(self.names([m], today="2026-10-14"), [])
        self.assertEqual(len(self.names([m], today="2026-10-15")), 1)

    def test_unregistered_cause_with_recurrence_is_candidate(self):
        c = cause("C05", 対策="ルール", 対策日="2026-10-02")
        got = self.names([], [c], [event("2026-10-04", ["C05"])])
        self.assertEqual(len(got), 1)
        self.assertIn("未登録", got[0])

    def test_render_says_candidates_are_not_deletions(self):
        text = mc.render([], [], [], [mc.judge_measure(measure(), [], self.tmp)], [("M01 x", ["理由"])],
                         datetime.date(2026, 10, 9))
        self.assertIn("候補になっただけでは消さない", text)
        self.assertIn("見直し候補: 1", text.split("\n")[0])


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

    def break_hook(self, name):
        path = os.path.join(self.copy, ".claude", "hooks", name)
        mc.write_text(path, mc.read_text(path) + "\nthis is a syntax error (\n")

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

    def test_broken_hook_fails_checks_that_expect_no_block(self):
        # 文法の誤りで動かない写しでも「通す」の試験が合格に見えた(2026-10-09 の置き場の行)
        self.assertTrue(mc.b_judge_words(mc.ROOT, {}, [])[0])
        self.break_hook("audit-stop.py")
        ok, detail = mc.b_judge_words(self.copy, {}, [])
        self.assertFalse(ok)
        self.assertIn("落ちた", detail)

    def test_broken_guard_is_not_counted_as_pass(self):
        self.break_hook("guard.py")
        ok, detail = mc.b_guard_cases(self.copy, {}, [])
        self.assertFalse(ok)
        self.assertIn("実際: 落ちた", detail)

    def test_replay_reports_coverage_without_claiming_effect(self):
        ok, detail = mc.b_audit_assumption_replay(mc.ROOT, {}, [])
        self.assertRegex(detail, r"^\d+/6 一致")

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
        measures = mc.parse_measures(os.path.join(mc.ROOT, mc.CAUSES))
        self.assertGreater(len(events), 40)
        self.assertGreater(len(causes), 20)
        self.assertEqual(mc.structure_errors(events, causes, measures), [])

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
