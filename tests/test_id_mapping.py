"""回归测试：模型用"全局编号"而非"本批编号"回复时，不能整批丢数据。

真实事故：第 3 批（条目 17–24）模型返回 id 17..24，旧代码只认 1..8，
8 条全被丢弃，且上层把"有回复但无结果"误报成"超出预算"。
"""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from brief import llm
from brief.config import Model
from brief.feed import Item

STATE = {"reply": "", "requests": 0, "queue": []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        import json
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        STATE["requests"] += 1
        # queue 优先：模拟"第一次空、第二次正常"
        if STATE.get("queue"):
            content = STATE["queue"].pop(0)
        else:
            content = STATE["reply"]
        payload = {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class IdMappingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def client(self):
        return llm.DeepSeek(Model(base_url=self.base, model="m"), "k", timeout=10)

    def items(self, n=8, start=0):
        return [Item(source_id="s", category="c", title=f"标题{i}",
                     link=f"https://e.com/{start + i}", summary="摘要", weight=1.0)
                for i in range(n)]

    def reply(self, ids, what="转述"):
        import json
        objs = [{"id": i, "what": f"{what}{i}", "why": "意义", "tag": "t"} for i in ids]
        STATE["reply"] = json.dumps({"lede": "导语", "takeaway": "判断", "items": objs},
                                    ensure_ascii=False)

    # ── 核心回归 ──────────────────────────────────────────────
    def test_global_ids_from_batch_three(self):
        """模型按 17..24 回复第 3 批 —— 必须能映射回去，不能丢。"""
        self.reply(range(17, 25))
        batch = self.items(8, start=16)
        out = self.client().analyze(batch, budget_left=1.0)
        mapped = {k: v for k, v in out.items() if k != "__meta__"}
        self.assertEqual(len(mapped), 8, "8 条应全部映射成功，之前这里是 0")
        # 顺序要正确：17 → 本批第 1 条
        self.assertEqual(mapped[batch[0].uid]["what"], "转述17")
        self.assertEqual(mapped[batch[7].uid]["what"], "转述24")

    def test_local_ids_still_work(self):
        """模型老实按 1..8 回复时不能受影响。"""
        self.reply(range(1, 9))
        batch = self.items(8, start=16)
        out = self.client().analyze(batch, budget_left=1.0)
        mapped = {k: v for k, v in out.items() if k != "__meta__"}
        self.assertEqual(len(mapped), 8)
        self.assertEqual(mapped[batch[0].uid]["what"], "转述1")

    def test_zero_based_ids(self):
        """有些模型从 0 开始编号。"""
        self.reply(range(0, 8))
        batch = self.items(8, start=16)
        out = self.client().analyze(batch, budget_left=1.0)
        mapped = {k: v for k, v in out.items() if k != "__meta__"}
        self.assertEqual(len(mapped), 8)
        self.assertEqual(mapped[batch[0].uid]["what"], "转述0")

    def test_missing_ids_falls_back_to_order(self):
        """完全不返回 id 时按顺序对齐，而不是全丢。"""
        import json
        objs = [{"what": f"第{i}条", "why": "w", "tag": "t"} for i in range(1, 9)]
        STATE["reply"] = json.dumps({"lede": "", "takeaway": "", "items": objs},
                                    ensure_ascii=False)
        batch = self.items(8)
        out = self.client().analyze(batch, budget_left=1.0)
        mapped = {k: v for k, v in out.items() if k != "__meta__"}
        self.assertEqual(len(mapped), 8)
        self.assertEqual(mapped[batch[0].uid]["what"], "第1条")

    def test_mixed_valid_and_invalid_ids(self):
        """部分 id 越界时，只丢越界的那条，其余保留。"""
        self.reply([1, 2, 3, 999])
        batch = self.items(4)
        out = self.client().analyze(batch, budget_left=1.0)
        # 1,2,3 明显是本批编号；999 越界应被丢弃，但不能把前三者一起带崩
        mapped = {k: v for k, v in out.items() if k != "__meta__"}
        self.assertGreaterEqual(len(mapped), 3)
        self.assertIn(batch[0].uid, mapped)

    def test_string_ids_with_brackets(self):
        """模型偶尔返回 "[3]" 或 "3." 这类脏 id。"""
        import json
        objs = [{"id": f"[{i}]", "what": f"w{i}", "why": "y", "tag": "t"} for i in (1, 2)]
        STATE["reply"] = json.dumps({"lede": "", "takeaway": "", "items": objs},
                                    ensure_ascii=False)
        batch = self.items(4)
        out = self.client().analyze(batch, budget_left=1.0)
        mapped = {k: v for k, v in out.items() if k != "__meta__"}
        self.assertEqual(len(mapped), 2)

    # ── 预算与错误归因不混淆 ──────────────────────────────────
    def test_budget_rejection_makes_no_request(self):
        STATE["requests"] = 0
        self.reply(range(1, 9))
        out = self.client().analyze(self.items(8), budget_left=0.0)
        self.assertEqual(out, {})
        self.assertEqual(STATE["requests"], 0, "超预算时不该发请求")

    def test_prompt_states_batch_local_ids(self):
        c = self.client()
        prompt = c._build_prompt(self.items(8))
        self.assertIn("1..8", prompt, "提示词必须写清楚用本批编号")
        self.assertIn("正好 8 个对象", prompt)

    # ── 空 items 的回归（真实事故：第 1 批返回空数组导致 min() 崩溃）──
    def test_empty_items_retries_then_recovers(self):
        """第一次返回空 items，第二次正常 —— 应重试并救回整批。"""
        import json
        STATE["queue"] = [
            json.dumps({"lede": "", "takeaway": "", "items": []}),
            json.dumps({"lede": "导语", "takeaway": "判断",
                        "items": [{"id": i, "what": f"w{i}", "why": "y", "tag": "t"}
                                  for i in range(1, 9)]}, ensure_ascii=False),
        ]
        STATE["requests"] = 0
        batch = self.items(8)
        out = self.client().analyze(batch, budget_left=1.0)
        mapped = {k: v for k, v in out.items() if k != "__meta__"}
        self.assertEqual(len(mapped), 8, "重试应把 8 条救回来")
        self.assertEqual(STATE["requests"], 2, "应恰好重试一次")
        STATE.pop("queue", None)

    def test_empty_items_twice_returns_empty_not_crash(self):
        """两次都空：返回空 dict（不能抛异常，上层好记录降级）。"""
        import json
        STATE["queue"] = [json.dumps({"items": []}), json.dumps({"items": []})]
        STATE["requests"] = 0
        out = self.client().analyze(self.items(8), budget_left=1.0)
        mapped = {k: v for k, v in out.items() if k != "__meta__"}
        self.assertEqual(mapped, {})
        self.assertEqual(STATE["requests"], 2)
        STATE.pop("queue", None)

    def test_map_by_id_handles_empty(self):
        """直接调用 _map_by_id / min([]) 不能崩；无 id 时按顺序兜底。"""
        c = self.client()
        items = self.items(8)
        self.assertEqual(c._map_by_id(items, []), {}, "空列表必须安全返回")
        # 有内容但没 id：按返回顺序对齐到第 1 条（有意为之的兜底）
        got = c._map_by_id(items, [{"what": "有转述", "why": "y", "tag": "t"}])
        self.assertEqual(got[items[0].uid]["what"], "有转述")


if __name__ == "__main__":
    unittest.main()