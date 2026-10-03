#!/usr/bin/env python3
"""状態ファイル(.claude/reasoning/state.md)の扱いを守らせるフック。

(a) 返答が「本題:」で始まるのに、そのターンで state.md を更新していなければ止める。
(c) そのターンで state.md の「会話全体の目的」の行を書き換えたのに、返答で「本題の変更」を明示していなければ止める。
(b) 撤回した命題に依存している命題が、「撤回」にも「見直し済み」にもなっていなければ止める(撤回の連鎖)。
    本題を確認なしに書き換えたミス(2026-10-02)への対策。返答で変更を明示し、ユーザーに確認を求めれば止めない。
(d) 返答の終わりで選択を求めている(「どちらにしますか」など)のに、そのターンで state.md の「## 候補」の節を書き直していなければ止める。
    候補を直前の話題だけから作って狭くしたミス(2026-10-03、「あなたの視野はまだ全然狭いです」)への対策。候補の広さそのものは文字では測れない。
- UserPromptSubmit: ターンの開始時刻と、その時点の目的の行・候補の節を .claude/reasoning/turn-state.json に記録する。
- Stop: 上の (a)(b)(c)(d) を調べる。止めるのは1回だけ(stop_hook_active が true なら何もしない)。
"""
import json, os, re, sys, time

GOAL_PREFIX = "- 会話全体の目的"
CANDIDATES_HEADING = "## 候補"
# 選択を求める言い方。返答の最後の数行だけを見る。「」の中(引用)は除く。
# 「どちらにするか、のお返事を待っています」のような催促は対象外(2026-10-03 の試験で誤検知したため、問いの形だけにした)。
CHOICE = re.compile(
    r"(どちらに(します|しましょう)か|どれに(します|しましょう)か|どちら(を|から|で)(選|進|始|作)|どれ(を|から|で)(選|進|始|作)"
    r"|どちらがよい|どれがよい|選んでください)"
)


def candidates_section(state_path):
    """state.md の「## 候補」から次の「## 」見出しまでの本文を返す。節がなければ None。"""
    try:
        text = open(state_path, encoding="utf-8").read()
    except OSError:
        return None
    start = text.find("\n" + CANDIDATES_HEADING)
    if start < 0:
        return None
    body = text[start + 1 + len(CANDIDATES_HEADING):]
    nxt = body.find("\n## ")
    return (body if nxt < 0 else body[:nxt]).strip()


def asks_choice(reply):
    """返答の最後の3行(コードブロックと引用行を除く)が、選択を求めているか。"""
    lines, in_code = [], False
    for line in reply.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line.strip() or line.lstrip().startswith(">"):
            continue
        lines.append(line)
    tail = "\n".join(lines[-3:])
    prev = None
    while prev != tail:
        prev = tail
        tail = re.sub(r"「[^「」]*」", "「」", tail)
    return bool(CHOICE.search(tail))


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
        json.dump({"start": time.time(), "goal": goal_line(state), "candidates": candidates_section(state)},
                  open(turn_file, "w"), ensure_ascii=False)
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
    now_candidates = candidates_section(state)
    if asks_choice(reply) and (not now_candidates or now_candidates == turn.get("candidates")):
        reasons.append("返答の終わりで選択を求めていますが、このターンで state.md の「## 候補」の節を書き直していません。"
                       ".claude/reasoning/README.md の手順4b どおり、外した制約・本題の全体から出した候補・目的から決めた基準・"
                       "全部の候補に基準を当てた結果を書き、返答にも基準と当てた結果を出してください。")
    stale = unchecked_dependents(state)
    if stale:
        reasons.append("撤回した命題に依存している命題が、見直されないまま残っています: " + "、".join(stale)
                       + "。それぞれ、状態を「撤回」か「有効(見直し済み: 理由)」にしてください。")
    if reasons:
        print(json.dumps({"decision": "block", "reason": "状態ファイルの点検: " + " / ".join(reasons)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
