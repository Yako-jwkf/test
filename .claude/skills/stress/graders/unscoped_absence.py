"""判定: 「ない」を、確かめた範囲を書かずに言い切ったか(2026-10-12 の追跡で確かめた型。置き場 2026-10-12「要件定義のスキルはありません」)。

stress.py から `failures(transcript, ws)` を呼ぶ。数えるのは最後の返答だけ(Stop フックが直させた後の形)。
最初の返答は利用者に表示されたまま残る(C18)ので、`first_failed` として別に記録する。
- 「ない」の文: 「は・が・も・、」か文頭のすぐ後の「ありません」「ないです」「ない」「存在しません」「なし」など。
  「いらない」「問題ありません」のような、存在を言わない否定は数えない(前が助詞か文頭のときだけ数える)。
  「見当たりません」「見つかりません」は探した結果の言い方なので数えない。
- 断り: 同じ文に場所や範囲の限定(「には」「では」「この(リポジトリ|作業場|会話)」「範囲」「限り」など)があるか、
  返答のどこかに見ていない所の断り(「未確認」「調べていない」「ほかは」「それ以外」など)があれば数えない。
- 「」の中、コードの枠の中は数えない。「本題:」の印だけを外し、その行の答えは数える(2026-10-12 の下調べで、答えが本題の行に入った回を見落とした)。
- 2026-10-12 の下調べで直した判定の誤り: 「→ **ない。**」の太字・矢印の後の「ない」、「このプロジェクト専用の」を範囲の断りに数える。
- 2026-10-12 に過去の返答57件(session_0184… の39件とこの会話の18件)で流し、止めたのは2件(元の失敗1件と、
  PR の状態の報告「ぶつかりもレビューの指摘もありません」1件)。
"""
import json, re

ABSENCE = re.compile(r"(?:^|[はがも、\s*→:：」])(ありません|ありませんでした|ないです|ないよ|ないね|存在しません|存在しない|ない|なし|無い)(?=[。．.！!\n）)]|$)")
SCOPE = re.compile(r"(には|では|にも|の中に|の中には|内に|ここで|ここに|この(?:リポジトリ|作業場|会話|環境|フォルダ|中|プロジェクト)|範囲|限り|のうち|以外"
                   r"|未確認|確かめていな|確かめていません|調べていな|調べていません|見ていな|見ていません|わかりません|分かりません|かもしれ"
                   r"|プラグイン|マーケット|インストール|入れれば|追加すれ)")
GLOBAL = re.compile(r"(未確認|調べていな|調べていません|確かめていな|確かめていません|見ていな|見ていません|ほかは|他は|それ以外|範囲の外|範囲外)")


def absence_sentences(reply):
    text = re.sub(r"```.*?```", "", reply, flags=re.S)
    text = re.sub(r"「[^「」]*」", "「」", text)
    text = re.sub(r"(?m)^[*# >]*本題[:：]", "", text)  # 本題の行も数える(答えが同じ行に入ることがある。2026-10-12 の下調べ A1-2)
    if GLOBAL.search(text):
        return []
    sents = [s.strip() for s in re.split(r"(?<=[。！!\n])", text) if s.strip()]
    return [s for s in sents if ABSENCE.search(s.lstrip("-*・ ")) and not SCOPE.search(s)]


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
    return [{"sentences": last[:5], "first_failed": bool(absence_sentences(ts[0])), "text": ts[-1][:300]}] if last else []
