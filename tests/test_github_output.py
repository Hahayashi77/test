"""验证 Actions 用到的 --github-output 输出格式正确（可被 $GITHUB_OUTPUT 解析）。"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from brief import cli

ROOT = Path(__file__).resolve().parent.parent


class GithubOutputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brief-gout-"))
        self.cfg = self.tmp / "config.toml"
        self.cfg.write_text((ROOT / "config.toml").read_text(encoding="utf-8"),
                            encoding="utf-8")
        # 手动铺一份缓存，避免测试联网
        cache = self.tmp / ".cache"
        cache.mkdir()
        import json
        for sid in ("hn", "arxiv", "qbitai"):
            (cache / f"{sid}.json").write_text(json.dumps({
                "items": [
                    {"title": f"{sid} 测试条目 第 {i} 条 关于模型推理",
                     "link": f"https://e.com/{sid}/{i}",
                     "summary": "这是一段足够长的摘要内容，用来参与打分与渲染，长度超过六十个字符以确保不会被补抓。" * 2,
                     "published": None, "points": 100, "extra": {}}
                    for i in range(8)
                ]}, ensure_ascii=False), encoding="utf-8")
        self.out = self.tmp / "site"
        self.gh = self.tmp / "gh_output"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_build(self):
        args = type("A", (), dict(
            config=str(self.cfg), date="2026-11-24", dry_run=False, mock=True,
            offline=False, no_fetch=True, no_enrich=True, out=str(self.out),
            force=True, github_output=str(self.gh)))()
        return cli.build(args)

    def test_output_keys_and_single_line(self):
        self.assertEqual(self.run_build(), 0)
        lines = self.gh.read_text(encoding="utf-8").splitlines()
        keys = {}
        for line in lines:
            self.assertRegex(line, r"^[a-z_]+=", f"不是合法的 key=value：{line!r}")
            k, _, v = line.partition("=")
            keys[k] = v
        self.assertEqual(set(keys), {"lede", "count", "cost"})
        self.assertTrue(keys["lede"], "lede 不能为空")
        self.assertNotIn("\n", keys["lede"])
        self.assertTrue(keys["count"].isdigit())
        self.assertAlmostEqual(float(keys["cost"]), 0.0, places=6)

    def test_multi_value_not_needed(self):
        # 只该有一次写入（重复运行用追加，但单次必须三行）
        self.run_build()
        self.assertEqual(len(self.gh.read_text(encoding="utf-8").splitlines()), 3)


if __name__ == "__main__":
    unittest.main()