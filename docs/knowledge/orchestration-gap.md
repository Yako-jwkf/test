# オーケストレーター案と今ある仕組みの差分表(2026-10-12)

要約: 2026-10-12 に利用者が貼った別の AI の案(Planner など6つの役割、`orchestrator.py` ほか8つのモジュール、初版の完成条件8つ)を、今ある台本・スキル・フック・エージェントと突き合わせた。制御の役(反復・予算・停止・再開・履歴・評価の基準の保護)は `scripts/improve_loop.py` がすでに持っている。欠けているのは Learner(成功・開発履歴・生の会話から知識を掘る、候補の台帳、未使用の事例で確かめる)と、一般の作業の分解・依存関係。要件 v0.2 の D3 で、`orchestrator.py` は今は作らず、Learner の最小の部品から作ると決めた(利用者の委任で決めた。[要件 v0.2](../requirements/continual-learning-v0.2.yaml))。

## 役割ごとの差分
| 案の役割(モジュール) | 今あるもの | 足りないもの |
|---|---|---|
| Planner(task_schema.py・planner.py) | 計画がないと動かない台本: `run.py`・`stress.py` は `plan.md` の4行(測りたいもの・使う条件・費用の見込み・結果で決めること)を点検する。要件定義は `/requirements`(2026-10-12)と `prompts/requirement-agent-v1.4.md`。CLAUDE.md の「依頼の前に完了条件を1行」 | 一般の作業を小さく分け、依存関係と完了条件を機械で検証する形(スキーマ)はない |
| Dispatcher(scheduler.py) | `improve_loop.py` の choose(見直し候補と履歴から、対象と変更の種類を選ぶ。外す・まとめるを先に試す)。評価の並列(ThreadPoolExecutor、8本)。会話の中では Agent・Workflow の道具 | 依存関係に沿って順番と並列を決める部分はない。今の作業は一本道で、要る場面がまだない |
| Executor(executor.py) | `run.py`・`stress.py`(専用フォルダで `claude -p`。本体と出力先を読めないように止める)。`improve_loop.py` の実装役(読むだけで案を出させ、台本が当てる) | 大きな欠けはない |
| Verifier(verifier.py) | judge(AI。2体、割れたら3体目)、`eval_metrics.py`(数える・機械判定)、`mistake_check.py`(擬似解消関数)、停止フック(audit-stop・state-guard・inbox-guard)、CI(mistake-check・boundary) | `improve_loop.py` の decide は、judge で採点する事例の答えの長さとフックの停止を見ていない(2026-10-10 の M06 で分かった決まりの穴。next-steps 32) |
| recovery.py | `improve_loop.py`: 反復の上限・予算・時間・候補なし・評価不能で止める。評価不能は成功に数えない。利用上限で止まった組は次に動かしたときにもう一度試す | 予算の上限がドル(`total_cost_usd`)で、利用者によると会話の情報に出る料金は実態と違う(2026-10-12、next-steps 36)。回数で上限を持つ形に直す候補 |
| event_log.py | `episodes.jsonl`(追記だけ)、`state.json`(再開用)、`retest-log.md`(追記だけ)、ミス置き場、開発の記録の表 | 費用の欄は上と同じ問題 |
| Orchestrator(orchestrator.py) | `improve_loop.py`(自己改善ループ専用の状態遷移) | ループ以外の作業を回す汎用の制御はない |
| Learner | 失敗から: ミス置き場 → `/reflect` → 原因の表(対策ごとの状態)。人が書く: `/add-knowledge` | 成功・開発履歴・生の会話から掘る部分、候補の台帳(種別・反例・確信度)、未使用の事例で確かめる段。2026-10-11 の試験は掘るところまでだった([continual-learning-design.md](continual-learning-design.md)) |
| 自己改変の防止 | `.claude/settings.json` の deny(judge・`/retest`・guard.py・境界の試験)と ask、`guard.py`、`improve_loop.py` の EVAL_PATHS(評価の基準を変えた差分は評価せずに棄却)、`check_boundary.py` と CI | 大きな欠けはない |

## 案の「初版の完成条件」と今の improve_loop.py
| 完成条件(案) | 今 |
|---|---|
| 同じ入力と状態から、同じ計画・状態遷移を再現できる | 一部。比べる版を最初のコミット(base_commit)に固定している。評価役は AI なので、答えは毎回変わる |
| 依存関係を無視した実行や、禁止された操作を防げる | 禁止された操作は deny・ask・guard.py・EVAL_PATHS で防ぐ。依存関係は、扱う作業がない |
| 失敗時に無限再試行しない | 反復の上限・予算・時間で止める |
| テスト不合格を成功として報告しない | 評価不能は成功に数えずに止める |
| コスト・時間・再試行・最終結果を記録できる | 記録はする。費用の値は実態と違う疑い(上) |
| 中断後に保存済み状態から再開できる | 途中の反復を「中断」として記録して続ける |
| オーケストレーター自身や評価器を、通常のタスクから勝手に変えられない | EVAL_PATHS と deny・ask |
| 既存の評価セットで、変更前より悪化していないことを確かめられる | 変更前の基準値と比べる。ただし judge の事例の長さ・停止は見ていない(上) |

## 決めたこと(要件 v0.2 の D3。委任で決めた)
- `orchestrator.py` ほか8つのモジュールは、今は作らない。上の表のとおり制御は `improve_loop.py` にあり、作ると同じものが2つになる。足りないのは Learner。
- 次に作る候補(要件の C1「最初は設計と現状調査だけ」により、実験の計画を確かめた後): ①知識の台帳(1ファイル) ②掘る・確かめるの手順 ③改善ループの決まりの穴2つ(judge の事例の長さ・停止、ドルの上限)を直す。
- 汎用の制御は、自己改善ループのほかに「繰り返し回す作業」が出てきたときに見直す。

## 根拠
- 貼られた案: 2026-10-12 の会話(session_0184dhdYQ8dJGo19gdUBEQTL)
- `scripts/improve_loop.py`(説明・choose・decide・run・EVAL_PATHS)、`scripts/eval_metrics.py`、`.claude/skills/stress/stress.py`、`.claude/skills/retest/SKILL.md`、`.claude/settings.json`(どれも 2026-10-12 に開いた)
- [effect-evaluation.md](effect-evaluation.md)(M06 の副作用と決まりの穴)、[next-steps.md](next-steps.md) の 32・36
