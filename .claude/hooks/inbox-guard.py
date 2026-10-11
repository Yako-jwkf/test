#!/usr/bin/env python3
"""ミス置き場への書き漏れを防ぐフック(e)。

- UserPromptSubmit: ユーザーの発言に指摘らしい言葉があれば、そのターンの開始時刻とともに .claude/reasoning/turn.json に記録する。
- Stop: 指摘ありのターンなのに docs/knowledge/mistake-inbox.md がそのターン中に更新されていなければ、1回だけ止めて書かせる。
  指摘ではなかったときは、返答に「指摘ではない」と書けば止めない。
置き場への記録が「お願い」(.claude/reasoning/README.md の手順3)だけだったため、仕組みで守る(2026-10-02)。
"""
import json, os, re, sys, time

# 呼び名の2語は、記録では「ポンコツ」「もの忘れ」に言い換えた(2026-10-09、利用者の依頼)。
# 利用者が元の語で書いても拾えるように、元の2語は文字を見せずに文字コードから組み立てる。
OLD_NAMES = chr(0x6C60) + chr(0x6CBC) + "|" + chr(0x75F4) + chr(0x5446)
MARKERS = re.compile(
    r"(違う|ちがう|やり直|ミス|間違|誤り|おかしい|なんの話|何の話|なの[？?]|では[？?]|ではない[？?]|じゃない[？?]"
    r"|回収|読めて|論理的に|ポンコツ|ベルトコンベア|もの忘れ|" + OLD_NAMES + ")"
)


def paths(data):
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or "."
    return (os.path.join(root, ".claude/reasoning/turn.json"),
            os.path.join(root, "docs/knowledge/mistake-inbox.md"))


def main():
    data = json.load(sys.stdin)
    turn_file, inbox = paths(data)
    event = data.get("hook_event_name")
    if event == "UserPromptSubmit":
        prompt = data.get("prompt") or ""
        # フックの差し戻しや、サブエージェントの報告(ユーザーの発言ではない)は対象外(2026-10-02 に誤検知)
        if prompt.startswith("Stop hook feedback") or "[Subagent hand-back]" in prompt or prompt.startswith("Another Claude session"):
            return
        hits = sorted(set(MARKERS.findall(prompt)))
        os.makedirs(os.path.dirname(turn_file), exist_ok=True)
        json.dump({"start": time.time(), "markers": hits}, open(turn_file, "w"), ensure_ascii=False)
        return
    if event != "Stop" or data.get("stop_hook_active"):
        return
    try:
        turn = json.load(open(turn_file))
    except (OSError, json.JSONDecodeError):
        return
    if not turn.get("markers"):
        return
    if "指摘ではない" in (data.get("last_assistant_message") or ""):
        return
    if os.path.exists(inbox) and os.path.getmtime(inbox) >= turn["start"]:
        return
    reason = ("ミス置き場の点検: このターンのユーザーの発言に、指摘らしい言葉("
              + "、".join(turn["markers"]) + ")がありました。しかし docs/knowledge/mistake-inbox.md が更新されていません。"
              "私のミスに当たるなら、原文のまま1行足してください。指摘ではなかったなら、返答に「指摘ではない」と理由を書いてください。")
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


if __name__ == "__main__":
    main()
