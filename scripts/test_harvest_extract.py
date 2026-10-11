#!/usr/bin/env python3
"""scripts/harvest_extract.py の試験。python3 scripts/test_harvest_extract.py で動く。

本物の会話は使わず、会話の記録(.jsonl)の形だけをまねた作り物で試す。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harvest_extract as hx  # noqa: E402


def human(text, ts="2026-10-11T10:00:00Z"):
    return {"type": "user", "timestamp": ts, "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def text(t):
    return {"type": "text", "text": t}


RECORD = [
    {"type": "attachment", "attachment": {"type": "x"}},
    human("設計して"),
    assistant({"type": "thinking", "thinking": "考えている途中"}),
    assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}),
    {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "秘密のファイル一覧"}]}},
    assistant(text("調べています。")),
    assistant(text("設計はこうです。"), {"type": "tool_use", "id": "t2", "name": "Read", "input": {}}),
    {"type": "user", "isMeta": True, "message": {"role": "user", "content": [{"type": "text", "text": "スキルの本文"}]}},
    {"type": "user", "isMeta": True, "message": {"role": "user", "content": "Stop hook feedback:\nコミットして"}},
    assistant(text("コミットはしません。")),
    human("連絡は someone@example.com へ。鍵は sk-abcdefghijklmnop", ts="2026-10-11T11:00:00Z"),
    assistant(text("token: abc123 は伏せます")),
    {"type": "user", "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": "背景の作業が終わった"}},
]


class Extract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "s.jsonl")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(e, ensure_ascii=False) for e in RECORD))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_人の発言だけがターンになる(self):
        items = hx.turns(self.src)
        self.assertEqual([t["prompt"] for t in items], ["設計して", "連絡は someone@example.com へ。鍵は sk-abcdefghijklmnop"])

    def test_返答は本文だけを順に取る(self):
        first = hx.turns(self.src)[0]["answer"]
        self.assertEqual(first, [("本文", "調べています。"), ("本文", "設計はこうです。"), ("停止フックへの返事", "コミットはしません。")])

    def test_思考と道具の結果と差し込みは入らない(self):
        out = os.path.join(self.tmp, "o.md")
        hx.main(self.src, out)
        body = read(out)
        for word in ("考えている途中", "秘密のファイル一覧", "スキルの本文", "背景の作業が終わった"):
            self.assertNotIn(word, body)

    def test_鍵とメールを伏せる(self):
        out = os.path.join(self.tmp, "o.md")
        hx.main(self.src, out)
        body = read(out)
        self.assertNotIn("someone@example.com", body)
        self.assertNotIn("sk-abcdefghijklmnop", body)
        self.assertNotIn("abc123", body)
        self.assertIn("[メール]", body)
        self.assertIn("[鍵]", body)

    def test_停止フックへの返事に印を付ける(self):
        out = os.path.join(self.tmp, "o.md")
        hx.main(self.src, out)
        self.assertIn("(停止フックへの返事)\nコミットはしません。", read(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
