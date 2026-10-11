# 実験2の台本と出力(2026-10-12)

要約: [continual-learning-design.md](../continual-learning-design.md) の「実験2」の台本と出力。開発の記録(分け目 c6667f5)から掘った候補が、後のミスを当時のルールより多く防げたかを、見せかけの候補と比べて照合した。

- `exp2.py`: 台本。`prepare`(材料の写し・確かめる事例)→ `mine`(掘る役)→ `leak`(漏れの点検)→ `judge`(採点役と集計)。パスは 2026-10-12 の会話の作業用フォルダのまま。流し直すときは `HERE` と `HELD_DATES` を直す。
- `candidates.json`: 掘る役が出した候補の全文(台帳の L01〜L10 は、この順)。
- `heldout.json`: 確かめる事例13件(ミス置き場の行の「何が起きたか」「見つけ方」)。
- `sham.json`: 見せかけの候補5つ(掘る前に決めた)。
- `judge-items.json`: 採点役に見せた番号(N01〜N15)と、本物・見せかけの対応。
- `judge-raw-1-2.json`・`judge-raw-3.json`: 採点役の表(2体と3体目)。
- `leaked-strict.json`・`leaked.json`: 外した候補(計画の文どおり/台本の数え方)。番号は0から。
- `result.json`: 3通りの数え方の結果。
- 掘る役の会話記録と、材料の写しは残していない(材料は `git show c6667f5:<パス>` で作り直せる)。
