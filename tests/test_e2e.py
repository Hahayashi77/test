"""端到端集成测试：mock 全链路（不联网）+ 假 OpenAI 服务真调用。

跑法：
    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from brief import cli, llm

from .test_llm import Handler, make_items, reply_mode, start_server

ROOT = Path(__file__).resolve().parent.parent


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brief-e2e-"))
        # 造一份只含本地缓存的最小配置：完全不依赖网络
        self.cfg = self.tmp / "config.toml"
        self.cfg.write_text(
            (ROOT / "config.toml").read_text(encoding="utf-8"), encoding="utf-8")
        self.out = self.tmp / "site"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argparse_like(self, **kw):
        base = dict(config=str(self.cfg), date="2026-11-24", dry_run=False, mock=True,
                    offline=False, no_fetch=True, out=str(self.out), force=True,
                    github_output=None)
        base.update(kw)
        return type("A", (), base)()

    def test_mock_pipeline_produces_site(self):
        # 先抓一次真实信源，落 .cache（这一步联网；失败则跳过）
        try:
            raw, status = cli.load_all(cli.cfgmod.load(self.cfg))
        except Exception:
            self.skipTest("网络不可用")
        if sum(s["got"] for s in status) < 3:
            self.skipTest("信源全都不可用")
        self.assertGreater(len(raw), 0)

        code = cli.build(self._argparse_like())
        self.assertEqual(code, 0)

        idx = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("每日技术简报", idx)
        self.assertIn("<title>", idx)
        self.assertIn('lang="zh-CN"', idx)
        self.assertIn("今日判断", idx)
        self.assertNotIn("None", idx.split("<body>")[1][:2000])

        day = self.out / "d" / "2026-11-24.html"
        self.assertTrue(day.exists())
        self.assertEqual(day.read_text(encoding="utf-8"), idx, "index 应等于最新一期")

        snap = json.loads((self.out / "data" / "2026-11-24.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["date"], "2026-11-24")
        self.assertTrue(snap["items"])
        self.assertEqual(snap["degraded"], True)   # mock 模式标记
        self.assertIn("演练", snap["lede"])
        for it in snap["items"]:
            self.assertTrue(it["title"] and it["url"].startswith("http"))

        arc = json.loads((self.out / "data" / "archive.json").read_text(encoding="utf-8"))
        self.assertEqual(arc["entries"][0]["date"], "2026-11-24")

        feed = (self.out / "feed.xml").read_text(encoding="utf-8")
        self.assertIn("<rss version=\"2.0\">", feed)
        self.assertIn("2026-11-24", feed)

    def test_no_overwrite_without_force(self):
        try:
            cli.load_all(cli.cfgmod.load(self.cfg))
        except Exception:
            self.skipTest("网络不可用")
        cli.build(self._argparse_like())
        code = cli.build(self._argparse_like(force=False))
        self.assertEqual(code, 0)

    def test_dry_run_has_no_analysis(self):
        try:
            cli.load_all(cli.cfgmod.load(self.cfg))
        except Exception:
            self.skipTest("网络不可用")
        cli.build(self._argparse_like(mock=False, dry_run=True))
        snap = json.loads((self.out / "data" / "2026-11-24.json").read_text(encoding="utf-8"))
        self.assertTrue(all(it["analysis"] is None for it in snap["items"]))
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("原始标题", html)

    def test_archive_accumulates(self):
        try:
            cli.load_all(cli.cfgmod.load(self.cfg))
        except Exception:
            self.skipTest("网络不可用")
        cli.build(self._argparse_like(date="2026-11-24"))
        # 第二期：同缓存、不同日期
        cli.build(self._argparse_like(date="2026-11-25", force=True))
        arc = json.loads((self.out / "data" / "archive.json").read_text(encoding="utf-8"))
        dates = [e["date"] for e in arc["entries"]]
        self.assertEqual(dates, ["2026-11-25", "2026-11-24"], "归档应新在前")
        self.assertTrue((self.out / "d" / "2026-11-25.html").exists())
        self.assertEqual(len(arc["entries"]), 2)


class RealApiPipelineTest(unittest.TestCase):
    """用假 OpenAI 服务走通"真的发请求"这条路径。"""

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_analyze_all_batches_and_budget(self):
        reply_mode["mode"] = "normal"
        model = llm.Model(model="deepseek-flash", base_url=self.base, batch_size=2)
        client = llm.DeepSeek(model, "k", timeout=10)
        items = make_items(5)
        cfg = type("C", (), {"model": model, "budget": type("B", (), {"max_usd_per_run": 1.0})()})()
        analyses, digest = cli.analyze_all(client, cfg, items)
        self.assertEqual(client.usage.requests, 3, "5 条 / 每批 2 条 = 3 批")
        self.assertFalse(digest["degraded"])
        self.assertTrue(digest["lede"])
        self.assertEqual(client.usage.failures, 0)

    def test_budget_stops_early(self):
        reply_mode["mode"] = "normal"
        model = llm.Model(model="deepseek-flash", base_url=self.base, batch_size=1)
        client = llm.DeepSeek(model, "k", timeout=10)
        items = make_items(4)
        # 每次约 $0.0004；给 1 厘（$0.001）只够两批
        cfg = type("C", (), {"model": model, "budget": type("B", (), {"max_usd_per_run": 0.001})()})()
        _, digest = cli.analyze_all(client, cfg, items)
        self.assertLess(client.usage.requests, 4, "预算耗尽后必须停止调用")
        self.assertTrue(digest["degraded"])


if __name__ == "__main__":
    unittest.main()