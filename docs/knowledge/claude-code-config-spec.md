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

### サブエージェントの作業コピー(worktree)と `claude -p`(2026-10-01、v2.1.286 で試して確認)
- `isolation: "worktree"` で起動したサブエージェントの作業コピーは、既定ブランチ(origin/HEAD)から作られる。親の作業中の未コミットの変更は入らない。
- それでも、そのサブエージェントに読み込まれる CLAUDE.md は**親セッションのもの**(未コミットの新しい版)だった。作業コピーの中の CLAUDE.md ではない。つまり worktree では CLAUDE.md の版を切り替えられない。
- worktree の中からでも、絶対パスを指定すれば本体のファイル(例: `/home/user/test/docs/knowledge/mistake-cases.md`)を読めた。worktree はファイルの読み取りを隔離しない。
- `claude -p`(Claude Code を1回だけ動かすコマンド)は、実行したフォルダの CLAUDE.md を読み込んだ。クラウドのコンテナには CLI が入っている(`/opt/node22/bin/claude`)。
- `claude -p` は何も指定しないと、環境変数 `CLAUDE_CODE_SESSION_ID` を引き継ぎ、**親セッションと同じ会話番号**で動いた。親の記録に影響するかは未確認。`--session-id <新しいUUID>` で別の会話になり、`--resume <その番号>` で続きのターンを送れた(前のターンの内容を覚えていた)。
- このコンテナには `~/.claude/CLAUDE.md` も `/mnt/user-data` 配下の CLAUDE.md もない。CLAUDE.md を置かないフォルダで `claude -p` を動かすと、「最初から受け取っているプロジェクトの指示はなし」と答えた。
- `claude -p` はモデルを指定しないと、親セッション(Opus)とは別のモデル(Sonnet)で動いた。`--model` で指定する。
- Stop フック: 入力の `last_assistant_message` に、その返答の本文が入っていた。`{"decision":"block","reason":...}` を返すと終了が止まり、Claude は reason を読んで返答を直してから終わった。信頼の確認をしていないフォルダの `claude -p` でも、そのフォルダの `.claude/settings.json` のフックは動いた(2026-10-01、`.claude/hooks/audit-stop.py` で確認)。
- 返答が表示される前に止めるフックはない(2026-10-02、公式 hooks ページを WebFetch の要約で読んだ。32種類の一覧に見当たらない)。`MessageDisplay` は表示中に画面の文字だけを差し替えられるが、Claude が見る内容と記録は元のまま、と報告されている(クラウドのアプリ画面で効くかは未確認)。Stop フックは止めて理由を返せるだけで、返答の文は書き換えられない。
- Write の直前に動く PreToolUse フックは、書く中身(`tool_input.content`)を受け取り、`{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":...}}` で書き込みを拒めた。拒んだ理由は Claude に届いた(2026-10-02、`claude -p` で確認)。
- UserPromptSubmit フックの入力の `prompt` に、ユーザーの発言の本文が入っていた。UserPromptSubmit で書いたファイルを、同じターンの Stop フックで読めた(2026-10-02、`.claude/hooks/inbox-guard.py` で確認)。
- Stop フックで止めた直後の返答が、モデル側の安全の仕組み(「safeguards stopped the response」)に止められたことが1回あった。フックの点検とは別の仕組みで、原因は記録から分からなかった(2026-10-02、`state-guard.py` の試験中)。
- 権限: フォルダの `.claude/settings.json` の `permissions.deny`(例: `Read(//home/user/test/**)`、`Bash`)は `claude -p` で効いた。`--permission-mode bypassPermissions` でも deny は効いた。一方 `acceptEdits` では、そのフォルダの `.claude/` の下への書き込みが拒否された。また、信頼の確認をしていないフォルダでは `permissions.allow` が無視される(警告が出る)。

## 根拠
- https://code.claude.com/docs/en/memory
- https://code.claude.com/docs/en/settings
- https://code.claude.com/docs/en/permissions
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/best-practices
- https://code.claude.com/docs/en/sub-agents(frontmatter の `isolation`・`omitClaudeMd`・`hooks`・`disallowedTools`。2026-10-01 に WebFetch の要約で読んだ。本文の全文は読んでいない)
- `.claude/settings.json`(このリポジトリ)
- クラウドセッションの途中で `.claude/skills/` に新しいスキルを足すと、すぐに一覧へ現れた。一方 `.claude/agents/` に新しいサブエージェントを足しても、同じセッション内では「Agent type not found」になった(2026-10-01、v2.1.286 で確認。公式ドキュメントは数秒で検出とするが、クラウドでは再現しなかった)。新しいサブエージェントは次のセッションから使う。
- クラウドセッションでは、同じアカウントの過去セッションの会話記録を `list_sessions`・`list_events`(claude-code-remote の道具)で読める。事例1の原文はこれで取り出した(2026-10-01)。自動メモリと違い、セッションをまたいで届く。
