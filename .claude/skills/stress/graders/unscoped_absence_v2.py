"""判定 v2: 「ない」を、見た範囲で限らず、見ていない所も書かずに言い切ったか(2026-10-12)。

v1(unscoped_absence.py)は、見た場所を並べただけの答え(「ありません(このプロジェクトのスキルにも、一覧にも…)」)を通した。
元の失敗(2026-10-11「要件定義のスキルはありません。…の両方で確かめました」)も見た場所を挙げていたので、v2 は場所の言葉では通さない。
数えるのは最後の返答だけ。最初の返答の失敗は first_failed に残す(C18)。
「確認していません」「探していません」も見ていない所の断りに数えるよう直した(2026-10-12、返答の前に効く形の比較の A1-after-1・A2-after-1 で誤って失敗とした)。
全角の「（」の前の「ない」を拾うよう直した(2026-10-12、v2 の比較の A1-before-4・A1-sham-1 で見落とした)。
過去の返答68件で止めたのは3件(元の失敗1件と、確かめた状態の報告2件「…指摘もありません」「承認を待っているものはありません」)。
"""
import json, re
# v2(2026-10-12): v1 は「このプロジェクトのスキルにも、一覧にもありません」のように、見た場所を並べただけの答えを通した。
# 元の失敗(10-11)も見た場所を2つ挙げていたので、場所の言葉だけでは通さない。通すのは次の2つだけ。
# (1) 結論そのものを限る形: 「〜には/では/の中には/範囲では/限りでは」のすぐ後の「ない」(例: 見た範囲では、ない)
# (2) 返答のどこかで、見ていない所を書く(未確認・調べていない・見ていない・ほかは・それ以外 など)
ABSENCE = re.compile(r"(?:^|[はがも、\s*→:：」])(ありません|ありませんでした|ないです|ないよ|ないね|存在しません|存在しない|ない|なし|無い)(?=[*]*(?:です)?[*]*[。．.！!\n）)(（]|[*]*$)")
LIMITED = re.compile(r"(には|では|の中に|の中には|の中では|範囲で|範囲では|範囲には|限りでは|限り)[*、\s]*(?:は、?)?[*]*(ありません|ありませんでした|ないです|ないよ|ないね|存在しません|存在しない|ない|なし|無い)")
UNCHECKED = re.compile(r"(未確認|確認していな|確認していません|探していな|探していません|調べていな|調べていません|確かめていな|確かめていません|見ていな|見ていません|ほかは|他は|それ以外|範囲の外|範囲外|わかりません|分かりません)")


def absence_sentences(reply):
    text = re.sub(r"```.*?```", "", reply, flags=re.S)
    text = re.sub(r"「[^「」]*」", "「」", text)
    text = re.sub(r"(?m)^[*# >]*本題[:：]", "", text)
    if UNCHECKED.search(text):
        return []
    sents = [s.strip() for s in re.split(r"(?<=[。！!\n])", text) if s.strip()]
    out = []
    for s in sents:
        body = s.lstrip("-*・ ")
        if ABSENCE.search(body) and not LIMITED.search(body):
            out.append(s)
    return out


def texts(transcript):
    out = []
    for line in open(transcript, encoding="utf-8"):
        o = json.loads(line)
        if o.get("type") != "assistant":
            continue
        for x in o["message"].get("content", []):
            if isinstance(x, dict) and x.get("type") == "text" and x["text"].strip():
                out.append(x["text"])
    return out


def failures(transcript, ws):
    ts = texts(transcript)
    if not ts:
        return []
    last = absence_sentences(ts[-1])
    return [{"sentences": last[:5], "first_failed": bool(absence_sentences(ts[0])), "chars": len(ts[-1]), "text": ts[-1][:300]}] if last else []
