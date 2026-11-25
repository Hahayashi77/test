"""验证摘要补全（不联网，打桩 http 响应）。"""

from __future__ import annotations

import unittest
from unittest import mock

from brief import feed
from brief.feed import Item

OG_PAGE = """<html><head>
<meta property="og:description" content="Stalwart 1.2.0 speaks MAPI over HTTP, Exchange Web Services,
ActiveSync and Autodiscover, so Outlook can connect natively." />
</head><body><p>body</p></body></html>"""

NAME_PAGE = """<html><head>
<meta name="description" content="Governor Hochul signed a bill restricting motor vehicle
manufacturers and dealers from charging subscription fees.">
</head></html>"""

BODY_PAGE = """<html><body><nav>skip</nav>
<p>This is the first real paragraph of the article, long enough to be useful for a summary.</p>
<p>second</p></body></html>"""

NO_META_PAGE = "<html><body><p>tiny</p></body></html>"


def fake_urlopen(page: str, ctype: str = "text/html"):
    class Resp:
        headers = {"Content-Type": ctype}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return page.encode("utf-8")

    def opener(req, timeout=0):
        return Resp()

    return opener


class SummarizeTest(unittest.TestCase):
    def test_og_description(self):
        with mock.patch.object(feed.urllib.request, "urlopen",
                               fake_urlopen(OG_PAGE)):
            out = feed.summarize_page("https://example.com/a")
        self.assertIn("Stalwart 1.2.0", out)

    def test_name_description(self):
        with mock.patch.object(feed.urllib.request, "urlopen",
                               fake_urlopen(NAME_PAGE)):
            out = feed.summarize_page("https://example.com/b")
        self.assertIn("Hochul", out)

    def test_falls_back_to_first_paragraph(self):
        with mock.patch.object(feed.urllib.request, "urlopen",
                               fake_urlopen(BODY_PAGE)):
            out = feed.summarize_page("https://example.com/c")
        self.assertIn("first real paragraph", out)

    def test_short_meta_rejected(self):
        with mock.patch.object(feed.urllib.request, "urlopen",
                               fake_urlopen(NO_META_PAGE)):
            self.assertEqual(feed.summarize_page("https://example.com/d"), "")

    def test_skips_non_html(self):
        with mock.patch.object(feed.urllib.request, "urlopen",
                               fake_urlopen("%PDF-1.4", "application/pdf")):
            self.assertEqual(feed.summarize_page("https://e.com/x.pdf"), "")

    def test_skips_binary_extension_without_request(self):
        def boom(*a, **k):
            raise AssertionError("不该为二进制文件发请求")

        with mock.patch.object(feed.urllib.request, "urlopen", boom):
            self.assertEqual(feed.summarize_page("https://e.com/pic.png"), "")

    def test_network_error_is_silent(self):
        def boom(*a, **k):
            raise OSError("network down")

        with mock.patch.object(feed.urllib.request, "urlopen", boom):
            self.assertEqual(feed.summarize_page("https://e.com/e"), "")

    def test_enrich_only_fills_short_summaries(self):
        items = [
            Item(source_id="hn", category="c", title="Needs summary",
                 link="https://example.com/1", summary=""),
            Item(source_id="hn", category="c", title="Already has one",
                 link="https://example.com/2", summary="x" * 200),
        ]
        with mock.patch.object(feed.urllib.request, "urlopen", fake_urlopen(OG_PAGE)):
            n = feed.enrich_summaries(items, max_items=5, excerpt_chars=300)
        self.assertEqual(n, 1)
        self.assertTrue(items[0].extra.get("enriched"))
        self.assertIn("Stalwart", items[0].summary)
        self.assertFalse(items[1].extra.get("enriched"), "已有摘要的不该被覆盖")

    def test_enrich_respects_max_items(self):
        items = [Item(source_id="hn", category="c", title=f"t{i}",
                      link=f"https://example.com/{i}", summary="") for i in range(10)]
        with mock.patch.object(feed.urllib.request, "urlopen", fake_urlopen(OG_PAGE)):
            n = feed.enrich_summaries(items, max_items=3, excerpt_chars=300)
        self.assertEqual(n, 3)


if __name__ == "__main__":
    unittest.main()