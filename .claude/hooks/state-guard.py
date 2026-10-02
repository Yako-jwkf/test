#!/usr/bin/env python3
"""状態ファイル(.claude/reasoning/state.md)の扱いを守らせるフック。

(a) 返答が「本題:」で始まるのに、そのターンで state.md を更新していなければ止める。
(c) そのターンで state.md の「会話全体の目的」の行を書き換えたのに、返答で「本題の変更」を明示していなければ止める。
(b) 撤回した命題に依存している命題が、「撤回」にも「見直し済み」にもなっていなければ止める(撤回の連鎖)。
    本題を確認なしに書き換えたミス(2026-10-02)への対策。返答で変更を明示し、ユーザーに確認を求めれば止めない。
(d) 評価の軸: 返答に「評価するのは」があるのに state.md の「評価の軸」の表に行がなければ止める。表の行のうち、
    「対応」の欄に命題の番号(P1 など)も「対象外」もないものがあれば止める。
    文書が自分で書いた目的(「表示＝演出」)を軸にせず、観点を落としたミス(2026-10-02)への対策。
- UserPromptSubmit: ターンの開始時刻と、その時点の目的の行を .claude/reasoning/turn-state.json に記録する。
- Stop: 上の (a)〜(d) を調べる。止めるのは1回だけ(stop_hook_active が true なら何もしない)。
"""
import json, os, sys, time

GOAL_PREFIX = "- 会話全体の目的"


def goal_line(state_path):
    try:
        for line in open(state_path, encoding="utf-8"):
            if line.startswith(GOAL_PREFIX):
                return line.strip()
    except OSError:
        return None
    return None


def unchecked_dependents(state_path):
    """(b) 撤回の連鎖: 撤回した命題に依存しているのに、状態が「撤回」でも「見直し済み」でもない命題を返す。"""
    import re
    rows = []
    try:
        for line in open(state_path, encoding="utf-8"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 6 and re.fullmatch(r"P\d+", cells[0]):
                rows.append((cells[0], cells[3], cells[-1]))
    except OSError:
        return []
    retracted = {pid for pid, _, st in rows if st.startswith("撤回")}

    def deps(text):
        ids = set(re.findall(r"P\d+", text))
        for a, b in re.findall(r"P(\d+)\s*[〜~-]\s*P(\d+)", text):
            ids |= {f"P{i}" for i in range(int(a), int(b) + 1)}
        return ids

    return [f"{pid}(依存: {', '.join(sorted(deps(dep) & retracted))})"
            for pid, dep, st in rows
            if deps(dep) & retracted and not st.startswith("撤回") and "見直し" not in st]


def axis_rows(state_path):
    """(d) 「## 評価の軸」の表の行を (軸, 対応) で返す。節がなければ None。"""
    rows, inside = None, False
    try:
        for line in open(state_path, encoding="utf-8"):
            if line.startswith("## "):
                inside = line.startswith("## 評価の軸")
                if inside:
                    rows = []
                continue
            if not inside or not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not cells or cells[0].startswith("軸") or set(cells[0]) <= set("-: "):
                continue
            rows.append((cells[0], cells[-1] if len(cells) >= 3 else ""))
    except OSError:
        return None
    return rows


def unanswered_axes(rows):
    import re
    return [axis for axis, resp in rows if not re.search(r"P\d+", resp) and "対象外" not in resp]


def main():
    data = json.load(sys.stdin)
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or "."
    state = os.path.join(root, ".claude/reasoning/state.md")
    turn_file = os.path.join(root, ".claude/reasoning/turn-state.json")
    event = data.get("hook_event_name")
    if event == "UserPromptSubmit":
        prompt = data.get("prompt") or ""
        if prompt.startswith("Stop hook feedback") or "[Subagent hand-back]" in prompt or prompt.startswith("Another Claude session"):
            return
        os.makedirs(os.path.dirname(turn_file), exist_ok=True)
        json.dump({"start": time.time(), "goal": goal_line(state)}, open(turn_file, "w"), ensure_ascii=False)
        return
    if event != "Stop" or data.get("stop_hook_active") or not os.path.exists(state):
        return
    try:
        turn = json.load(open(turn_file))
    except (OSError, json.JSONDecodeError):
        return
    reply = data.get("last_assistant_message") or ""
    first = next((l.strip() for l in reply.split("\n") if l.strip()), "")
    reasons = []
    if first.startswith("本題:") and os.path.getmtime(state) < turn["start"]:
        reasons.append("返答が「本題:」で始まっていますが、このターンで .claude/reasoning/state.md を更新していません。"
                       ".claude/reasoning/README.md の手順どおり、本題の位置づけ・指摘・命題を更新してください。")
    now_goal = goal_line(state)
    if turn.get("goal") and now_goal != turn["goal"] and "本題の変更" not in reply:
        reasons.append("このターンで state.md の「会話全体の目的」を書き換えましたが、返答に「本題の変更」の明示がありません。"
                       "変えた前後を返答に書き、ユーザーに確認を求めてください。確認なしに変えるつもりがなかったなら、元に戻してください。")
    stale = unchecked_dependents(state)
    if stale:
        reasons.append("撤回した命題に依存している命題が、見直されないまま残っています: " + "、".join(stale)
                       + "。それぞれ、状態を「撤回」か「有効(見直し済み: 理由)」にしてください。")
    axes = axis_rows(state)
    if "評価するのは" in reply and not axes:
        reasons.append("返答に「評価するのは」がありますが、state.md の「評価の軸」の表に行がありません。"
                       "評価する文書が自分で書いている目的・原則・意図の宣言を原文のまま並べ、会話全体の目的も1行入れてください"
                       "(.claude/reasoning/README.md の手順2)。")
    missing = unanswered_axes(axes or [])
    if missing:
        reasons.append("「評価の軸」のうち、評価した命題の番号も「対象外(理由)」もない行があります: " + "、".join(missing)
                       + "。その軸で評価して命題を足すか、対象外の理由を書いてください。")
    if reasons:
        print(json.dumps({"decision": "block", "reason": "状態ファイルの点検: " + " / ".join(reasons)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
