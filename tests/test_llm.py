"""用一个假的 OpenAI 兼容服务验证 llm.py：请求构造、用量计费、JSON 容错、预算熔断。

无需联网、无需 API Key：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from brief import llm
from brief.config import Model
from brief.feed import Item

REPLY = {
    "lede": "今天的主线是端侧推理与小模型压缩。",
    "takeaway": "先看 GLM-5.4 的 loop transformer，值得复现。",
    "items": [
        {"id": 1, "what": "某团队把 27B 模型压到 10GB。", "why": "端侧部署可行的信号。", "tag": "推理"},
        {"id": 2, "what": "第二条的转述。", "why": "第二条的意义。", "tag": "工具"},
    ],
}

reply_mode = {"mode": "normal"}


class Handler(BaseHTTPRequestHandler):
    last_body: dict = {}

    def log_message(self, *args):  # 静音
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        Handler.last_body = body
        Handler.last_auth = self.headers.get("Authorization", "")

        if reply_mode["mode"] == "http500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"boom"}')
            return
        if reply_mode["mode"] == "badkey":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid key"}')
            return

        content = json.dumps(REPLY, ensure_ascii=False)
        if reply_mode["mode"] == "fenced":
            content = "```json\n" + content + "\n```"
        if reply_mode["mode"] == "chatty":
            content = "好的，以下是结果：\n" + content + "\n希望有帮助。"

        payload = {
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 300,
                "prompt_cache_hit_tokens": 200,
            },
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def make_items(n=2):
    return [
        Item(source_id="hn", category="社区热议", title=f"标题{i}", link=f"https://e.com/{i}",
             summary=f"摘要{i}", weight=1.2, points=200)
        for i in range(1, n + 1)
    ]


class LlmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def client(self, model_name="deepseek-flash"):
        model = Model(model=model_name, base_url=self.base, temperature=0.2,
                      batch_size=8, max_output_tokens=1500, excerpt_chars=300)
        return llm.DeepSeek(model, "test-key", timeout=10)

    def test_request_shape(self):
        reply_mode["mode"] = "normal"
        c = self.client()
        c.analyze(make_items(), budget_left=1.0)
        body = Handler.last_body
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])
        self.assertEqual(Handler.last_auth, "Bearer test-key")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("[1]", body["messages"][1]["content"])
        self.assertIn("HN 200", body["messages"][1]["content"])

    def test_usage_accounting(self):
        reply_mode["mode"] = "normal"
        c = self.client()
        c.analyze(make_items(), budget_left=1.0)
        u = c.usage
        self.assertEqual(u.requests, 1)
        self.assertEqual(u.prompt_tokens, 1200)
        self.assertEqual(u.cache_hit_tokens, 200)
        self.assertEqual(u.completion_tokens, 300)
        # 200 hit + 1000 miss + 300 out，谷/峰只差倍数
        price = llm.PRICES["deepseek-flash"]
        base = (200 * price["cache_hit"] + 1000 * price["cache_miss"]
                + 300 * price["output"]) / 1_000_000
        self.assertAlmostEqual(u.cost_usd, base * (2.0 if u.peak else 1.0), places=9)
        self.assertGreater(u.cost_usd, 0)

    def test_parses_ids_and_meta(self):
        reply_mode["mode"] = "normal"
        c = self.client()
        items = make_items()
        out = c.analyze(items, budget_left=1.0)
        self.assertEqual(out[items[0].uid]["tag"], "推理")
        self.assertIn("端侧", out["__meta__"]["lede"])
        self.assertIn("loop transformer", out["__meta__"]["takeaway"])

    def test_fenced_json(self):
        reply_mode["mode"] = "fenced"
        out = self.client().analyze(make_items(), budget_left=1.0)
        self.assertEqual(out[make_items()[0].uid]["tag"], "推理")

    def test_chatty_json(self):
        reply_mode["mode"] = "chatty"
        out = self.client().analyze(make_items(), budget_left=1.0)
        self.assertEqual(out[make_items()[1].uid]["tag"], "工具")

    def test_budget_blocks_call(self):
        reply_mode["mode"] = "normal"
        c = self.client()
        Handler.last_body = {}
        out = c.analyze(make_items(), budget_left=0.0)   # 一分钱预算都不给
        self.assertEqual(out, {})
        self.assertEqual(c.usage.requests, 0, "超预算时不应该发出请求")
        self.assertEqual(Handler.last_body, {})

    def test_retries_then_succeeds(self):
        reply_mode["mode"] = "http500"
        c = self.client()
        calls = {"n": 0}
        orig = llm.time.sleep

        def fake_sleep(_):
            calls["n"] += 1
            if calls["n"] >= 2:
                reply_mode["mode"] = "normal"

        with mock.patch.object(llm.time, "sleep", fake_sleep):
            out = c.analyze(make_items(), budget_left=1.0)
        self.assertTrue(out.get(make_items()[0].uid), "重试后应该拿到结果")
        self.assertEqual(c.usage.failures, 0)

    def test_401_not_retried(self):
        reply_mode["mode"] = "badkey"
        c = self.client()
        with self.assertRaises(RuntimeError) as ctx:
            c.analyze(make_items(), budget_left=1.0)
        self.assertIn("401", str(ctx.exception))

    def test_missing_key(self):
        c = llm.DeepSeek(Model(base_url=self.base), None)
        with self.assertRaises(RuntimeError):
            c.analyze(make_items(), budget_left=1.0)

    def test_peak_detection(self):
        import datetime as dt
        bj = llm.BEIJING
        # 2026-11-24 是周二
        self.assertTrue(llm.is_peak(dt.datetime(2026, 11, 24, 10, 0, tzinfo=bj)))
        self.assertFalse(llm.is_peak(dt.datetime(2026, 11, 24, 12, 30, tzinfo=bj)))
        self.assertTrue(llm.is_peak(dt.datetime(2026, 11, 24, 15, 0, tzinfo=bj)))
        self.assertFalse(llm.is_peak(dt.datetime(2026, 11, 24, 20, 0, tzinfo=bj)))
        self.assertFalse(llm.is_peak(dt.datetime(2026, 11, 28, 10, 0, tzinfo=bj)))  # 周六

    def test_price_table_has_pro(self):
        self.assertIn("deepseek-v4-pro", llm.PRICES)
        self.assertAlmostEqual(llm.PRICES["deepseek-v4-pro"]["output"], 1.98)


if __name__ == "__main__":
    unittest.main()