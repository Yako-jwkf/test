# 全分野の道具(外から入れたスキル)と、試験の凍結

要約: 2026-10-12、あらゆる種類の問題に備える道具として、外の公式のスキル22個を `.claude/skills/` に入れた。試験の条件を変えないよう、目印 `TOOLKIT.md` のあるフォルダは、試験の台本が試験用のフォルダに写さない(利用者「干渉する部分だけリポジトリ凍結して入れればいい」)。選んだ理由の全体は、この会話で作った選別の文書(全315件の判定)にある。

## 凍結の仕組み
- 目印: 外から入れたスキルのフォルダに `TOOLKIT.md` を置く。出どころ・コミット・ライセンスもそこに書く
- `run.py`(`/retest`): 目印のあるフォルダを `LEAK_DIRS` に足す。保護ファイルなので、`control-plane-v3.md` の「保護ファイルを変える手順」(deny を外す → 直す → 戻す)で直した
- `stress.py`(`/stress`): 同じ計算を足した
- `improve_loop.py`: `run.py` の `LEAK_DIRS` を読むので、変更なしで同じになる
- 確かめたこと(2026-10-12): 入れる前と後で、`improve_loop.py` の写し(68件)と `stress.py` の写し(78件)の一覧が一致。`check_boundary.py` OK、`mistake_check.py` OK、単体試験(4本)OK。`run.py` の `fill_workspace` そのものは直接は流していない(同じ `LEAK_DIRS` を `improve_loop.py` 経由で確かめた)
- 凍結していないもの: claude.ai のアカウントで有効にしたスキル(2026-10-12 時点で13個)は、クラウドの会話に同期される(公式の文書)。`claude -p` の試験に入るかは未確認。入るなら3条件に同じようにかかる(推測)。止めるなら、試験用のフォルダに `.claude/settings.local.json` で `syncClaudeAiSkills: false` を置く(この設定はプロジェクトの settings.json には書けず、user・local・managed だけ)。過去の測定と条件が変わるので、まだ入れていない

## 入れたもの(22個。どれも Apache License 2.0)
| 出どころ | スキル |
|---|---|
| anthropics/skills | frontend-design・webapp-testing・internal-comms・theme-factory・web-artifacts-builder・canvas-design・algorithmic-art・slack-gif-creator |
| anthropics/claude-plugins-official の plugin-dev | agent-development・command-development・hook-development・mcp-integration・plugin-settings・plugin-structure・skill-development |
| 同 mcp-server-dev | build-mcp-app・build-mcp-server・build-mcpb |
| 同 project-artifact・claude-code-setup | project-artifact・claude-automation-recommender |
| GoogleChrome/modern-web-guidance | modern-web-guidance・chrome-extensions |

## 入れなかったもの(理由)
- `doc-coauthoring`: フォルダにライセンスの表示がない
- `academy-guide`: 説明文が「Claude の使い方の質問には、どの返答の前にもこのスキルを確かめる」形で、この作業場の会話はほとんどがその種類
- `discernment-nudge`: 中身のある答えの後に毎回動く形で、すべての答えの形が変わる
- `brand-guidelines`(Anthropic 自身のブランド用)、`mcp-builder`(`build-mcp-server` などと同じ役)
- plugin-dev の3つのエージェントと1つのコマンド、`code-modernization`: `.claude/agents/`・`.claude/commands/` に入る。凍結はスキルのフォルダだけに効くので、入れていない
- 毎ターン動く仕組みを持つプラグイン(security-guidance・learning-output-style・remember・claude-security): 使うときだけ入れる
- `duckdb-skills`: `query`・`read-file` など一般的な名前のスキルが、この作業場のスキルと同じ段に並ぶ。初めて使うときに実行ファイルを入れる
- `context7`(Context7 へのログインが要る MCP)、`Learn with Coursera`(Coursera のコネクタが要る): アカウントの操作が要る
- 公式の文書の書き方では、claude.ai で入れたプラグインが読み込まれるのは手元のターミナルと Cowork で、クラウドの会話は挙がっていない(2026-10-12)。クラウドで使うものは、このリポジトリに入れる必要がある

## 出どころ
- https://github.com/anthropics/skills 、https://github.com/anthropics/claude-plugins-official 、https://github.com/GoogleChrome/modern-web-guidance(コミットは各 `TOOLKIT.md`)
- 公式の文書: settings-reference(syncClaudeAiSkills・syncClaudeAiPlugins)、cloud-environments(What carries over)
