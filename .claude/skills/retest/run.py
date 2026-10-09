#!/usr/bin/env python3
"""事例を、条件ごとの専用フォルダで `claude -p` に答えさせる(/retest の答える役)。

使い方: python3 run.py <事例番号> <回数> <出力フォルダ> [条件,...]
  <出力フォルダ>/plan.md(計画ファイル)がないと動かない。書式は PLAN_KEYS を参照。
  条件: mech(今の作業中の版) / prev(仕組みを入れる前の版。PREV_REF) / none(CLAUDE.md なし)。省略すると3つとも。

- 各回に専用フォルダを作り、そこで `claude -p` を動かす。CLAUDE.md は本番どおり自動で読み込まれる。
- 答え合わせになるファイル(LEAK_FILES)は専用フォルダに置かない。本体のリポジトリと出力フォルダは読めないように設定で止める。
- 会話番号は毎回新しく指定する。指定しないと親セッションと同じ番号で動く(claude-code-config-spec.md)。
- 答えは <出力フォルダ>/<条件>-<回>/turnN.json と answers.md に残す。採点はしない。
"""
import json, os, re, shutil, subprocess, sys, tarfile, io, uuid
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CASES = os.path.join(REPO, "docs/knowledge/mistake-cases.md")
LEAK_FILES = [
    "docs/knowledge/mistake-cases.md",
    "docs/knowledge/mistakes.md",
    "docs/knowledge/retest-log.md",
    "docs/knowledge/mistake-inbox.md",
    ".claude/reasoning/state.md",
    # 事例の原文や答え合わせを含むファイル(next-steps 18。2026-10-09 に事例の文の断片で全ファイルを調べて確かめた)
    "docs/knowledge/control-plane-v3.md",
    "docs/knowledge/next-steps.md",
    "docs/knowledge/pstack-evaluation.md",
    "docs/knowledge/focus-tracking-design.md",  # 事例7の利用者の基準
    "docs/knowledge/reflect-2026-10-06.md",     # 事例6の発言
    "docs/knowledge/pending-mechanisms.md",
    "docs/knowledge/boss-ai-design.md",         # 事例6の発言
]
# フォルダごと置かないもの。/stress の候補(cases/)に事例5と同じ場面の依頼文がある(2026-10-09、grep)
LEAK_DIRS = (".claude/skills/stress/",)
MODEL = os.environ.get("RETEST_MODEL", "claude-opus-5-5")
# 仕組み(.claude/reasoning/・フック)を入れる前の最後のコミット。比べる相手を変えるときは環境変数 RETEST_PREV_REF で指定する
PREV_REF = os.environ.get("RETEST_PREV_REF", "12f8bd7")
SANDBOX_ROOT = "/tmp/retest-sandboxes"
TURN_TIMEOUT = 900


def read_turns(case_no):
    text = open(CASES, encoding="utf-8").read()
    m = re.search(rf"^## 事例{case_no}\n(.*?)(?=^## 事例|\Z)", text, re.S | re.M)
    if not m:
        sys.exit(f"事例{case_no} が見つからない")
    turns = re.findall(r"- \*\*入力\(ターン(\d+)\)\*\*:\n\n```\n(.*?)\n```", m.group(1), re.S)
    if not turns:
        sys.exit(f"事例{case_no} に原文の入力がない")
    return [t for _, t in sorted(turns, key=lambda x: int(x[0]))]


def fill_workspace(cond, ws):
    os.makedirs(ws)
    if cond == "mech":
        files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                               cwd=REPO, capture_output=True, text=True, check=True).stdout.split("\n")
        for f in filter(None, files):
            if f in LEAK_FILES or f.startswith(LEAK_DIRS) or not os.path.exists(os.path.join(REPO, f)):
                continue
            os.makedirs(os.path.dirname(os.path.join(ws, f)) or ws, exist_ok=True)
            shutil.copy2(os.path.join(REPO, f), os.path.join(ws, f))
    elif cond == "prev":
        # prev は「仕組みを入れる前の版」。HEAD にすると、仕組みをコミットした後は仕組み入りの版になってしまう(2026-10-02)
        data = subprocess.run(["git", "archive", PREV_REF], cwd=REPO, capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(ws, members=[m for m in tar.getmembers()
                                        if m.name not in LEAK_FILES and not m.name.startswith(LEAK_DIRS)])
    elif cond != "none":
        sys.exit(f"不明な条件: {cond}")
    # 本体と出力先を読めないようにする。条件の差を作らないよう、3条件とも同じ設定を足す。
    settings_path = os.path.join(ws, ".claude/settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings = json.load(open(settings_path)) if os.path.exists(settings_path) else {}
    # 境界(権限の規則・bypass の禁止・guard.py)は測る対象ではないので、3条件とも外す。
    # 残すと mech だけ bypassPermissions が効かず、state.md を書けなかった(2026-10-02)
    settings["permissions"] = {}
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if "hooks/guard.py" not in json.dumps(g)]
        if not hooks[event]:
            del hooks[event]
    deny = settings["permissions"].setdefault("deny", [])
    for path in (REPO, OUT_DIR):
        for tool in ("Read", "Grep", "Glob", "Edit", "Write"):
            deny.append(f"{tool}(/{path}/**)")
    deny.append("Bash")
    json.dump(settings, open(settings_path, "w"), ensure_ascii=False, indent=2)


def transcript_answers(sid):
    """会話の記録ファイルから、ターンごとの返答をすべてつなげて返す。

    Stop フックに止められると、Claude は訂正を「追加のメッセージ」として書く。`claude -p` の result は
    最後のメッセージだけなので、それだけを答えにすると本文が抜ける(2026-10-02 に確認)。
    """
    import glob as _glob
    files = _glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl"))
    if not files:
        return None
    answers = []
    for line in open(files[0], encoding="utf-8"):
        r = json.loads(line)
        msg = r.get("message") or {}
        if r.get("type") == "user" and isinstance(msg.get("content"), str):
            if msg["content"].startswith("Stop hook feedback:"):
                if answers:
                    answers[-1].append("(ここで Stop フックが返答を止めた)")
                continue
            answers.append([])
        elif r.get("type") == "assistant" and answers:
            for c in msg.get("content", []):
                if c.get("type") == "text" and c["text"].strip():
                    answers[-1].append(c["text"])
    return ["\n\n".join(a) for a in answers]


def write_answers(out, answers):
    with open(os.path.join(out, "answers.md"), "w", encoding="utf-8") as f:
        for i, a in enumerate(answers, 1):
            f.write(f"## ターン{i}\n\n{a}\n\n")


def run_one(cond, n, turns):
    name = f"{cond}-{n}"
    out = os.path.join(OUT_DIR, name)
    os.makedirs(out, exist_ok=True)
    ws = os.path.join(SANDBOX_ROOT, os.path.basename(OUT_DIR.rstrip("/")), name)
    if os.path.exists(ws):
        shutil.rmtree(ws)
    fill_workspace(cond, ws)
    sid = str(uuid.uuid4())
    answers = []
    for i, turn in enumerate(turns, 1):
        # acceptEdits では .claude/ の下への書き込みが止められ、未承認のフォルダでは allow 設定も無視される。
        # bypassPermissions でも deny は効く(2026-10-01 の試験で確認)ので、deny で本体と出力先を守る。
        cmd = ["claude", "-p", turn, "--model", MODEL, "--permission-mode", "bypassPermissions",
               "--output-format", "json"]
        cmd += ["--session-id", sid] if i == 1 else ["--resume", sid]
        p = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=TURN_TIMEOUT,
                           stdin=subprocess.DEVNULL)
        open(os.path.join(out, f"turn{i}.json"), "w").write(p.stdout)
        try:
            d = json.loads(p.stdout)
            result = d.get("result") or ""
            # 利用上限に達すると、エラーではなく通常の返事としてこの文が返る(2026-10-01)
            if "hit your session limit" in result or d.get("is_error"):
                answers.append(f"(失敗: {result[:200]})")
                break
            answers.append(result)
        except json.JSONDecodeError:
            answers.append(f"(失敗: exit={p.returncode} {p.stderr[:300]})")
            break
    full = transcript_answers(sid)
    write_answers(out, full if full and len(full) == len(answers) else answers)
    return name, len(answers)


# 測る前に書く計画。測りたいものを含まない事例で約13ドルを使った(2026-10-02)ため、書かないと動かない。
PLAN_KEYS = ["測りたいもの:", "使う条件:", "費用の見込み:", "結果で決めること:"]


def check_plan(out_dir):
    path = os.path.join(out_dir, "plan.md")
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    missing = [k for k in PLAN_KEYS
               if not re.search(rf"^{re.escape(k)}\s*\S", text, re.M)]
    if missing:
        sys.exit(f"{path} に計画を書いてから動かしてください。足りない行: {' '.join(missing)}\n"
                 "各行を「見出し: 中身」の形で書く。使う条件には、測りたいものを実際に測る条件の番号を書く。")
    print(text, flush=True)


if __name__ == "__main__":
    case_no, runs, OUT_DIR = sys.argv[1], int(sys.argv[2]), os.path.abspath(sys.argv[3])
    check_plan(OUT_DIR)
    conds = sys.argv[4].split(",") if len(sys.argv) > 4 else ["mech", "prev", "none"]
    turns = read_turns(case_no)[: int(os.environ.get("RETEST_MAX_TURNS", "99"))]
    os.makedirs(OUT_DIR, exist_ok=True)
    jobs = [(c, n) for c in conds for n in range(1, runs + 1)]
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        for name, k in ex.map(lambda j: run_one(*j, turns), jobs):
            print(f"{name}: {k}/{len(turns)} ターン", flush=True)
