"""中转站（OpenAI 兼容第三方端点）适配测试。

这些坑真实存在：
  - 中转站不认 response_format，返回 400
  - 中转站模型名不是官方名
  - /models 接口关掉或返回空
  - 计价规则和官方不同（倍率 + 汇率）
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

STATE = {"reject_json": False, "models": None, "requests": []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code: int, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/models"):
            if STATE["models"] is None:
                self._json(404, {"error": "not supported"})
            else:
                self._json(200, {"data": [{"id": m} for m in STATE["models"]]})
            return
        self._json(404, {"error": "nope"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        STATE["requests"].append(body)
        if STATE["reject_json"] and "response_format" in body:
            self._json(400, {"error": {"message": "Unsupported parameter: response_format",
                                       "type": "invalid_request_error"}})
            return
        self._json(200, {
            "choices": [{"message": {"content": json.dumps({
                "lede": "导语", "takeaway": "判断",
                "items": [{"id": 1, "what": "转述", "why": "意义", "tag": "测试"}],
            }, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
        })


class RelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        STATE.update(reject_json=False, models=None, requests=[])

    def client(self, **kw):
        m = Model(base_url=self.base, model=kw.pop("model", "deepseek-chat"), **kw)
        return llm.DeepSeek(m, "sk-test", timeout=10)

    def items(self):
        return [Item(source_id="hn", category="c", title="t", link="https://e.com/1",
                     summary="摘要", weight=1.0)]

    # ── JSON 模式自适应 ────────────────────────────────────────
    def test_json_falls_back_on_400(self):
        STATE["reject_json"] = True
        c = self.client()
        out = c.analyze(self.items(), budget_left=1.0)
        self.assertTrue(out.get(self.items()[0].uid), "降级后应拿到解读")
        self.assertFalse(c._json_mode, "开关应被永久关掉")
        self.assertEqual(len(STATE["requests"]), 2, "先带 json 失败，再不带 json 成功")
        self.assertIn("response_format", STATE["requests"][0])
        self.assertNotIn("response_format", STATE["requests"][1],
                         "降级重发时不能再带 response_format")

    def test_fallback_only_once_per_client(self):
        STATE["reject_json"] = True
        c = self.client()
        c.analyze(self.items(), budget_left=1.0)   # 第一次触发降级
        before = len(STATE["requests"])
        c.analyze(self.items(), budget_left=1.0)   # 第二次应直接不带 json
        self.assertEqual(len(STATE["requests"]), before + 1, "降级后不该再多试一次")
        self.assertNotIn("response_format", STATE["requests"][-1])

    def test_off_mode_never_sends_json(self):
        c = self.client(json_mode="off")
        c.analyze(self.items(), budget_left=1.0)
        self.assertNotIn("response_format", STATE["requests"][0])

    def test_on_mode_keeps_sending_json(self):
        c = self.client(json_mode="on")
        c.analyze(self.items(), budget_left=1.0)
        self.assertIn("response_format", STATE["requests"][0])

    def test_unrelated_400_not_treated_as_format_error(self):
        # 400 但说的是别的问题，不该盲目降级
        self.assertFalse(llm._looks_like_format_error("max_tokens too large"))
        self.assertTrue(llm._looks_like_format_error("Unsupported parameter: response_format"))
        self.assertTrue(llm._looks_like_format_error("json_object is not supported"))

    # ── 端点探测 ──────────────────────────────────────────────
    def test_probe_detects_model_in_list(self):
        STATE["models"] = ["deepseek-chat", "gpt-5.6", "claude-sonnet-4.5"]
        info = self.client().probe()
        self.assertTrue(info["reachable"] and info["auth_ok"])
        self.assertTrue(info["model_ok"])
        self.assertEqual(len(info["available_models"]), 3)

    def test_probe_detects_wrong_model_name(self):
        STATE["models"] = ["deepseek-v3.2", "gpt-5.6"]
        c = self.client(model="deepseek-flash-not-exist")
        info = c.probe()
        self.assertFalse(info["model_ok"])
        self.assertEqual(c.suggest_model(), "deepseek-v3.2", "应能推荐出可用的 DeepSeek 模型")

    def test_probe_tolerates_missing_models_endpoint(self):
        STATE["models"] = None      # /models 返回 404
        info = self.client().probe()
        self.assertTrue(info["reachable"])
        self.assertIsNone(info["available_models"], "拿不到列表时应为未知，而不是失败")
        self.assertIsNone(info["model_ok"])

    def test_probe_result_is_cached(self):
        STATE["models"] = ["deepseek-chat"]
        c = self.client()
        n0 = len(STATE["requests"])
        c.probe()
        c.probe()
        self.assertEqual(len(STATE["requests"]), n0, "probe 结果应缓存，不该重复请求")

    # ── 计价 ──────────────────────────────────────────────────
    def test_relay_pricing_multiplier(self):
        """倍率 + 汇率都要反映到花费上。"""
        base = self.client(price_multiplier=1.0, usd_per_unit=1.0)
        base.analyze(self.items(), budget_left=1.0)
        plain = base.usage.cost_usd

        cheap = self.client(price_multiplier=0.5, usd_per_unit=1 / 7)
        cheap.analyze(self.items(), budget_left=1.0)
        self.assertLess(cheap.usage.cost_usd, plain * 0.1,
                        "0.5x 倍率 + 1/7 汇率应显著更便宜")

    def test_relay_has_no_peak_pricing(self):
        """官方域名的峰谷价差不该套到中转站上。"""
        m = Model(base_url=self.base, model="deepseek-chat")
        self.assertFalse(llm._is_official(m))
        official = Model(base_url="https://api.deepseek.com", model="deepseek-flash")
        self.assertTrue(llm._is_official(official))

    def test_unknown_model_falls_back_to_flash_price(self):
        """认不出的模型名要按 Flash 价估算，不能让预算熔断失效。"""
        m = Model(base_url=self.base, model="some-unknown-model-xyz")
        table = llm._price_table(m)
        self.assertAlmostEqual(table["output"], llm.PRICES["deepseek-flash"]["output"])

    def test_estimate_uses_multiplier(self):
        exp = Model(base_url=self.base, model="deepseek-flash", price_multiplier=1.0)
        cheap = Model(base_url=self.base, model="deepseek-flash", price_multiplier=0.1)
        prompt = "x" * 4000
        self.assertLess(llm.estimate_cost(cheap, prompt, 2000),
                        llm.estimate_cost(exp, prompt, 2000))

    # ── 自定义 key 环境变量名 ─────────────────────────────────
    def test_api_key_env_name_used_in_error(self):
        c = llm.DeepSeek(Model(base_url=self.base, api_key_env="YYAPI_KEY"), None)
        with self.assertRaises(RuntimeError) as ctx:
            c.analyze(self.items(), budget_left=1.0)
        self.assertIn("YYAPI_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()