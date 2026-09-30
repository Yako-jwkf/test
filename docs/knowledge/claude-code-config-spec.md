# Claude Code の設定の仕様(CLAUDE.md・settings・フック)

要約: CLAUDE.md は「お願い」、settings の permissions とフックは「仕組みで守らせる」もの。クラウドセッションではリポジトリの `.claude/settings.json` しか効かない。

確認日: 2026-09-30(公式ドキュメントは随時更新されるため、古くなっている可能性がある)

## CLAUDE.md
- セッション開始時に自動で読み込まれる。システムプロンプトの後に、ユーザーメッセージとして渡される。強制力はない。
- 置き場所: 組織用(Linux は `/etc/claude-code/CLAUDE.md`)、自分用(`~/.claude/CLAUDE.md`)、プロジェクト用(`./CLAUDE.md` または `./.claude/CLAUDE.md`)、自分だけのプロジェクト用(`./CLAUDE.local.md`)。
- 作業フォルダとその上のフォルダにあるものは、開始時にすべて読まれて連結される(どれかが他を上書きするわけではない)。下のサブフォルダにあるものは、そこのファイルを読んだときに読まれる。
- `@パス` で別ファイルを取り込める。CLAUDE.md がなければ `AGENTS.md` が読まれる。`/compact` の後も読み直される。
- 推奨の長さは200行以内。

## settings.json
- 優先順(上ほど強い): 組織の管理設定 > コマンドライン > `.claude/settings.local.json` > `.claude/settings.json` > `~/.claude/settings.json`。`permissions` のような一覧の項目は上書きではなく合算される。
- **クラウドセッションでは `~/.claude/settings.json` と `settings.local.json` は読まれない。** 効くのは、リポジトリにコミットした `.claude/settings.json` と、組織のサーバー管理設定だけ。
- `permissions` のルールは deny → ask → allow の順に判定される。
- 読むだけの git コマンド(status・diff・log など)は、最初から許可なしで動く。allow に入れる必要はない。
- Bash のルールは書き方を変えるとすり抜けられる(例: `Bash(git push *)` は `git -C . push` に当たらない)。ドキュメント自身が「安全を保証する壁ではない」としている。確実に止めたいなら sandbox を使う。

## フック
- 種類は5つ: command・http・mcp_tool・prompt(AI に判断させる)・agent(サブエージェントに確認させる。実験的機能)。
- 反応できる出来事は30種類以上ある(PreToolUse、PostToolUse、Stop、SessionStart、InstructionsLoaded など)。PreToolUse や Stop などは、操作を止めることもできる。
- 設定できる場所は settings ファイルのほか、スキルやサブエージェントのファイルの先頭部分(frontmatter)。

## このリポジトリで確かめたこと
- 自動メモリ(Claude が自分で取るメモ)は、そのコンピューターの中にしか残らない。クラウドでは次のセッションに引き継がれないので、知識はこの `docs/knowledge/` にコミットして残す。
- ターンの終わりに「コミットして push して」と促すフック(`~/.claude/stop-hook-git-check.sh`)は、このリポジトリではなく、クラウド環境の起動設定(`~/.claude/launcher-settings.json`)から来ている。
- `.claude/settings.json` の `permissions.ask` に `Bash(git push *)` を入れたところ、クラウドセッションでも push の前に承認画面が出た(2026-09-30 確認)。

## 根拠
- https://code.claude.com/docs/en/memory
- https://code.claude.com/docs/en/settings
- https://code.claude.com/docs/en/permissions
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/best-practices
- `.claude/settings.json`(このリポジトリ)
