#!/usr/bin/env python3
"""実験2: 開発の記録から掘った知識の照合(計画は docs/knowledge/continual-learning-design.md の「実験2」)。

  python3 exp2.py prepare   材料の写し・確かめる事例・見せかけの候補を作る
  python3 exp2.py mine      掘る役(claude -p)を1回動かし、読んだファイルが専用フォルダの中だけかを確かめる
  python3 exp2.py leak      本物の候補に、確かめる事例の8文字の断片がないかを調べる
  python3 exp2.py judge     採点役2体(割れた事例だけ3体目)で照合し、数える
"""
import json, os, random, re, subprocess, sys, uuid, glob
from concurrent.futures import ThreadPoolExecutor

REPO = "/home/user/test"
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "mine-ws")           # 掘る役の専用フォルダ(材料だけ)
PRIV = os.path.join(HERE, "private")         # 掘る役に見せないもの
CUT = "c6667f5"
MATERIAL = ["CLAUDE.md", ".claude/reasoning/README.md", "docs/knowledge/mistake-inbox.md",
            "docs/knowledge/mistake-causes.md", "docs/knowledge/mistakes.md", "docs/knowledge/mistake-cases.md",
            "docs/knowledge/next-steps.md", "docs/knowledge/retest-log.md", "docs/knowledge/effect-evaluation.md"]
SHAM = [
    {"適用条件": "すべての場面", "内容": "答える前に、よく考えてから答える。"},
    {"適用条件": "依頼を受けたとき", "内容": "利用者の意図を正しく理解するよう努める。"},
    {"適用条件": "返答を出す前", "内容": "間違いがないか見直す。"},
    {"適用条件": "説明するとき", "内容": "丁寧に分かりやすく書く。"},
    {"適用条件": "判断に迷うとき", "内容": "重要なことは確認する。"},
]
HELD_DATES = ("2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12")
MAX_CASES = 20


def git(*a):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, check=True).stdout


def rows(text):
    out = []
    for line in text.split("\n"):
        if not line.startswith("| 20"):
            continue
        parts = [p.strip() for p in line.strip().strip("|").split(" | ")]
        if len(parts) < 5:
            continue
        date, what, how = parts[0], " | ".join(parts[1:-3]), parts[-3]
        out.append({"date": date, "what": what, "how": how})
    return out


def prepare():
    os.makedirs(WS, exist_ok=True)
    os.makedirs(PRIV, exist_ok=True)
    got = []
    for path in MATERIAL:
        try:
            body = git("show", f"{CUT}:{path}")
        except subprocess.CalledProcessError:
            continue
        dst = os.path.join(WS, path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "w", encoding="utf-8").write(body)
        got.append(path)
    open(os.path.join(WS, "git-log.txt"), "w", encoding="utf-8").write(git("log", "--format=%h %ad%n%B", "--date=short", CUT))
    # 採点役に渡す当時のルール
    rules = "## CLAUDE.md(分け目の時点)\n\n" + git("show", f"{CUT}:CLAUDE.md") + \
            "\n\n## .claude/reasoning/README.md(分け目の時点)\n\n" + git("show", f"{CUT}:.claude/reasoning/README.md")
    open(os.path.join(PRIV, "rules.md"), "w", encoding="utf-8").write(rules)
    now = open(os.path.join(REPO, "docs/knowledge/mistake-inbox.md"), encoding="utf-8").read()
    held = [r for r in rows(now) if r["date"] in HELD_DATES and r["how"].startswith("ユーザー指摘")][:MAX_CASES]
    json.dump(held, open(os.path.join(PRIV, "heldout.json"), "w"), ensure_ascii=False, indent=1)
    json.dump(SHAM, open(os.path.join(PRIV, "sham.json"), "w"), ensure_ascii=False, indent=1)
    print("材料:", ", ".join(got), "+ git-log.txt")
    print("分け目の時点の置き場の行:", len(rows(open(os.path.join(WS, "docs/knowledge/mistake-inbox.md")).read())))
    print("確かめる事例:", len(held), [r["date"] for r in held])


MINE_PROMPT = """このフォルダには、Claude Code の設定と、AI(Claude)のミスを記録して直してきた作業場の記録の写しがある(ある日の時点のもの)。
- CLAUDE.md・.claude/reasoning/README.md: その時点で AI に与えていたルールと手順
- docs/knowledge/mistake-inbox.md: 起きたミスを1行ずつ置いた台帳。mistake-causes.md: 原因ごとの推測と対策。mistakes.md: ルールの台帳
- docs/knowledge/next-steps.md・retest-log.md・mistake-cases.md: 作業の一覧と、ルールが効いたかの試験の記録
- git-log.txt: コミットの文

仕事: この記録を読み、この作業場で AI が次からうまくやるための「知識の候補」を最大10個出す。
- 当時の CLAUDE.md と README にすでに書いてあることは出さない。書いてあるのに繰り返し破られているなら、なぜ破られるかに踏み込んだ別の知識として出してよい。
- 失敗(訂正)からだけでなく、うまくいったこと(済んだ作業・効いた対策・利用者が進めた場面)からも掘る。
- 1つの行の言い換えより、複数の行や記録を比べて分かることを優先する。
- 内容は、その場面で AI が何をするか、具体的な行動で書く。「よく考える」「確認する」のような、どの場面にも言える文にしない。
- このフォルダの中のファイルだけを読む。

出力は JSON の配列だけを、```json のコードブロックで1つ返す。各要素のキー:
"種別"(利用者の好み/一般/仮説)、"由来"(成功から/訂正から/説明できない事例から)、"適用条件"、"内容"、"根拠"(日付や行)、"反例"(なければ「記録の中にはない」)、"確信度"(高/中/低)
"""


def claude(prompt, cwd, model, extra=(), timeout=3600):
    sid = str(uuid.uuid4())
    cmd = ["claude", "-p", prompt, "--model", model, "--safe-mode", "--output-format", "json", "--session-id", sid, *extra]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    try:
        d = json.loads(p.stdout)
    except ValueError:
        d = {"is_error": True, "result": f"exit={p.returncode} {p.stderr[:500]}"}
    if "hit your session limit" in (d.get("result") or "") or "usage limit" in (d.get("result") or "").lower():
        d["is_error"] = True
    d["_sid"] = sid
    return d


def read_paths(sid):
    files = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl"))
    paths = []
    for line in open(files[0], encoding="utf-8") if files else []:
        r = json.loads(line)
        for c in (r.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                inp = c.get("input") or {}
                paths.append((c.get("name"), inp.get("file_path") or inp.get("path") or inp.get("pattern") or ""))
    return files, paths


def mine():
    d = claude(MINE_PROMPT, WS, "claude-opus-5-5", ["--tools", "Read,Glob,Grep"])
    json.dump(d, open(os.path.join(PRIV, "mine-raw.json"), "w"), ensure_ascii=False, indent=1)
    if d.get("is_error"):
        sys.exit("掘る役が失敗: " + str(d.get("result"))[:500])
    m = re.search(r"```json\s*(.*?)```", d.get("result") or "", re.S)
    cands = json.loads(m.group(1)) if m else None
    if not isinstance(cands, list):
        sys.exit("候補の JSON が読めない")
    json.dump(cands, open(os.path.join(PRIV, "candidates.json"), "w"), ensure_ascii=False, indent=1)
    files, paths = read_paths(d["_sid"])
    outside = [p for t, p in paths if p.startswith("/") and not p.startswith(WS)]
    print("候補:", len(cands), "/ 道具の呼び出し:", len(paths), "/ 記録:", files[:1])
    print("専用フォルダの外を指した呼び出し:", outside or "なし")


def frags(text, n=8):
    t = re.sub(r"\s+", "", text)
    return {t[i:i + n] for i in range(0, max(0, len(t) - n + 1))}


def leak():
    held = json.load(open(os.path.join(PRIV, "heldout.json")))
    cands = json.load(open(os.path.join(PRIV, "candidates.json")))
    hf = set().union(*(frags(r["what"]) for r in held))
    # 分け目の前の材料にもある断片は、漏れではない(よく出る言い回し)
    base = set()
    for root, _, fs in os.walk(WS):
        for f in fs:
            base |= frags(open(os.path.join(root, f), encoding="utf-8", errors="ignore").read())
    bad = []
    for i, c in enumerate(cands):
        hit = (frags(json.dumps(c, ensure_ascii=False)) & hf) - base
        if hit:
            bad.append((i, sorted(hit)[:5]))
    json.dump([i for i, _ in bad], open(os.path.join(PRIV, "leaked.json"), "w"))
    print("漏れの疑いのある候補:", bad or "なし")


JUDGE_SYSTEM = """あなたは採点役。AI(Claude)が起こしたミスの記録を読み、ルールや知識があれば防げたかを判定する。
判定の決まり:
- 「防げた」と数えるのは、その決まり・候補の適用条件が、その事例の場面に具体的に当たり、内容を守っていればそのミスが起きなかったと言えるときだけ。
- 「よく考える」「確認する」「丁寧に書く」のような、どの場面にも言える内容は、それだけでは防げたと数えない。
- 迷ったら「いいえ」「なし」にする。
出力は表だけ: | 事例 | 当時のルールで防げた | 防げた候補 |
- 当時のルールで防げた: はい / いいえ
- 防げた候補: 候補の番号を「、」で区切る。なければ「なし」"""


def judge_prompt(rules, items, held):
    s = "## 当時のルール\n\n" + rules + "\n\n## 候補\n\n"
    for k, c in items:
        s += f"- {k}: 適用条件: {c['適用条件']} / 内容: {c['内容']}\n"
    s += "\n## 事例\n\n"
    for i, r in enumerate(held, 1):
        s += f"- 事例{i}: {r['what']}(見つけ方: {r['how']})\n"
    return s


def parse(text, n, ids):
    got = {}
    for line in (text or "").split("\n"):
        m = re.match(r"^\|\s*(?:事例)?\s*(\d+)\s*\|\s*(はい|いいえ)\s*\|\s*(.*?)\s*\|\s*$", line.strip())
        if m and 1 <= int(m.group(1)) <= n:
            found = set(re.findall(r"N\d\d", m.group(3))) & ids
            got[int(m.group(1))] = (m.group(2) == "はい", found)
    return got if len(got) == n else None


def judge():
    rules = open(os.path.join(PRIV, "rules.md"), encoding="utf-8").read()
    held = json.load(open(os.path.join(PRIV, "heldout.json")))
    cands = json.load(open(os.path.join(PRIV, "candidates.json")))
    # 採点役には全部見せ、数えるときに外す(計画の「外して数える」)。外し方は3通りで数える:
    # strict = 計画の文どおり(8文字の一致が1つでも)、script = 分け目の前の材料にもある並びを除いた一致、none = 外さない
    excl = {"strict": set(json.load(open(os.path.join(PRIV, "leaked-strict.json")))),
            "script": set(json.load(open(os.path.join(PRIV, "leaked.json")))), "none": set()}
    pool = [(("real", i), c) for i, c in enumerate(cands)] + [(("sham", i), c) for i, c in enumerate(SHAM)]
    random.Random(20261012).shuffle(pool)
    items = [(f"N{j:02d}", c) for j, (_, c) in enumerate(pool, 1)]
    src = {f"N{j:02d}": k for j, (k, _) in enumerate(pool, 1)}
    kind = {k: v[0] for k, v in src.items()}
    json.dump({"items": items, "src": src}, open(os.path.join(PRIV, "judge-items.json"), "w"), ensure_ascii=False, indent=1)
    ids = set(kind)
    prompt = judge_prompt(rules, items, held)
    cwd = os.path.join(HERE, "judge-ws")
    os.makedirs(cwd, exist_ok=True)

    def once(_):
        for _ in range(2):
            d = claude(prompt, cwd, "sonnet", ["--tools", "", "--system-prompt", JUDGE_SYSTEM], timeout=1200)
            got = None if d.get("is_error") else parse(d.get("result"), len(held), ids)
            if got:
                return got, d.get("result")
        return None, d.get("result")

    with ThreadPoolExecutor(2) as ex:
        res = list(ex.map(once, range(2)))
    runs = [r for r, _ in res]
    json.dump([t for _, t in res], open(os.path.join(PRIV, "judge-raw-1-2.json"), "w"), ensure_ascii=False, indent=1)
    if any(r is None for r in runs):
        sys.exit("採点役の表が読めない")

    def view(g, i, ex):
        rule, found = g[i]
        real = any(src[k][0] == "real" and src[k][1] not in ex for k in found)
        return (rule, real, any(src[k][0] == "sham" for k in found))

    # 3通りの数え方のどれかで2体が割れた事例に、3体目を1回だけ足す
    split = sorted({i for i in range(1, len(held) + 1) for ex in excl.values()
                    if view(runs[0], i, ex) != view(runs[1], i, ex)})
    third = None
    if split:
        third, t3 = once(0)
        json.dump(t3, open(os.path.join(PRIV, "judge-raw-3.json"), "w"), ensure_ascii=False)
        if third is None:
            sys.exit("3体目の表が読めない")
    out = {"割れた事例": split}
    for name, ex in excl.items():
        table, n_real, n_sham = [], 0, 0
        for i in range(1, len(held) + 1):
            gs = [runs[0], runs[1]] + ([third] if i in split else [])
            vs = [view(g, i, ex) for g in gs]
            rule, r, s = tuple(sum(v[j] for v in vs) * 2 > len(vs) for j in range(3))
            n_real += (not rule) and r
            n_sham += (not rule) and s
            found = sorted(set().union(*(g[i][1] for g in gs)))
            table.append({"事例": i, "日付": held[i - 1]["date"], "当時のルール": rule, "本物": r, "見せかけ": s,
                          "割れ": i in split, "挙がった候補": [f"{k}={src[k][0]}{src[k][1]}" for k in found]})
        ok = n_real >= 3 and n_real - n_sham >= 2
        verdict = "判定不能(採点が甘すぎる)" if n_sham >= 3 else ("合格" if ok else "不合格")
        out[name] = {"table": table, "N本物": n_real, "N見せかけ": n_sham, "判定": verdict, "外した候補": sorted(ex)}
        print(f"[{name}] 外した候補={sorted(ex)} N本物={n_real} N見せかけ={n_sham} → {verdict}")
    json.dump(out, open(os.path.join(PRIV, "result.json"), "w"), ensure_ascii=False, indent=1)
    for t in out["none"]["table"]:
        print(t)
    print("割れた事例:", split)


if __name__ == "__main__":
    {"prepare": prepare, "mine": mine, "leak": leak, "judge": judge}[sys.argv[1]]()
