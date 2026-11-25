"""发布前校验的测试 —— 对应那次"坏 JSON 被发到 Pages"的事故。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from brief import cli


def good_snapshot(n=3):
    return {
        "date": "2026-11-25",
        "degraded": False,
        "items": [{"title": f"t{i}", "url": f"https://e.com/{i}",
                   "analysis": {"what": "w", "why": "y", "tag": "t"}} for i in range(n)],
    }


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brief-verify-"))
        (self.tmp / "data").mkdir()
        (self.tmp / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        (self.tmp / "archive.html").write_text("<html>a</html>", encoding="utf-8")
        (self.tmp / "feed.xml").write_text("<rss/>", encoding="utf-8")
        self.write_snapshot(good_snapshot())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_snapshot(self, obj):
        (self.tmp / "data" / "latest.json").write_text(
            json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def run_verify(self, allow_degraded=False):
        args = type("A", (), {"site": str(self.tmp), "allow_degraded": allow_degraded})()
        return cli.verify(args)

    # ── 正常 ──────────────────────────────────────────────────
    def test_good_site_passes(self):
        self.assertEqual(self.run_verify(), 0)

    # ── 冲突标记（真实事故）────────────────────────────────────
    def test_conflict_markers_rejected(self):
        (self.tmp / "data" / "latest.json").write_text(
            '{\n  "date": "2026-11-25",\n<<<<<<< HEAD\n  "lede": "a"\n'
            '=======\n  "lede": "b"\n>>>>>>> abc\n}\n', encoding="utf-8")
        self.assertEqual(self.run_verify(), 1)

    def test_conflict_marker_in_html_rejected(self):
        (self.tmp / "index.html").write_text(
            "<html>\n<<<<<<< HEAD\n<p>a</p>\n=======\n<p>b</p>\n>>>>>>> x\n</html>",
            encoding="utf-8")
        self.assertEqual(self.run_verify(), 1)

    def test_marker_like_text_not_flagged(self):
        # 正文里出现 "=======" 分隔线不该误报（要求行首 7 个且后面是空格/行尾）
        (self.tmp / "index.html").write_text(
            "<pre>a ==== b</pre>\n<p>_______</p>", encoding="utf-8")
        self.assertEqual(self.run_verify(), 0)

    # ── JSON 合法性 ───────────────────────────────────────────
    def test_invalid_json_rejected(self):
        (self.tmp / "data" / "2026-11-24.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(self.run_verify(), 1)

    # ── 缺文件 ────────────────────────────────────────────────
    def test_missing_files_rejected(self):
        (self.tmp / "feed.xml").unlink()
        (self.tmp / "archive.html").unlink()
        self.assertEqual(self.run_verify(), 1)

    # ── 解读完整性 ────────────────────────────────────────────
    def test_missing_analyses_rejected(self):
        snap = good_snapshot()
        snap["items"][1]["analysis"] = None
        self.write_snapshot(snap)
        self.assertEqual(self.run_verify(), 1, "缺解读应拒绝发布")

    def test_missing_analyses_allowed_when_degraded(self):
        snap = good_snapshot()
        snap["degraded"] = True
        snap["items"][1]["analysis"] = None
        self.write_snapshot(snap)
        self.assertEqual(self.run_verify(), 0, "已标记降级时应放行")

    def test_missing_analyses_allowed_by_flag(self):
        snap = good_snapshot()
        snap["items"][1]["analysis"] = None
        self.write_snapshot(snap)
        self.assertEqual(self.run_verify(allow_degraded=True), 0)


if __name__ == "__main__":
    unittest.main()