"""验证解析与挑选：用真实格式的样本跑，不联网。"""

from __future__ import annotations

import datetime as dt
import unittest

from brief import feed, select
from brief.config import Select, Source

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>t</title>
<item>
  <title><![CDATA[2035 Came Early]]></title>
  <description><![CDATA[<p>Article URL: <a href="https://fzakaria.com/2035">x</a></p>
  <p>Points: 310</p><p># Comments: 94</p>]]></description>
  <pubDate>Tue, 24 Nov 2026 06:30:33 +0000</pubDate>
  <link>https://fzakaria.com/2035?utm_source=rss</link>
  <guid isPermaLink="false">https://news.ycombinator.com/item?id=1</guid>
</item>
<item>
  <title>Low score item</title>
  <description><![CDATA[<p>Points: 3</p>]]></description>
  <link>https://example.com/low</link>
</item>
</channel></rss>"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>blog</title>
  <entry>
    <title>datasette-charts 0.2a0</title>
    <link rel="alternate" href="/2026/Nov/24/charts"/>
    <id>https://simonwillison.net/2026/Nov/24/charts/</id>
    <published>2026-11-24T04:24:00+00:00</published>
    <summary type="html">&lt;p&gt;New release with &lt;b&gt;axes&lt;/b&gt;.&lt;/p&gt;</summary>
  </entry>
</feed>"""

ARXIV_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2611.12345v1</id>
    <title>ToolSmith: An Automatic Tool-Generation Framework
      for Multimodal Agents</title>
    <summary>We present a framework that generates tools on demand.</summary>
    <published>2026-11-23T18:48:00Z</published>
    <author><name>Alice Zhang</name></author>
    <author><name>Bob Li</name></author>
    <arxiv:primary_category term="cs.AI"/>
  </entry>
</feed>"""


def src(**kw):
    base = dict(id="t", kind="rss", url="https://example.com/feed", category="社区热议",
                weight=1.2, limit=10, order=10)
    base.update(kw)
    return Source(**base)


class ParseTest(unittest.TestCase):
    def test_rss_points_and_filter(self):
        items = feed.parse_feed(RSS_SAMPLE.encode(), src(min_points=100))
        self.assertEqual(len(items), 1, "低于 min_points 的应被过滤")
        it = items[0]
        self.assertEqual(it.title, "2035 Came Early")
        self.assertEqual(it.points, 310)
        self.assertNotIn("<p>", it.summary, "HTML 标签应被剥离")
        self.assertEqual(it.published.tzinfo is not None, True)
        # HN 的 "Article URL / Comments URL / Points" 是元数据不是正文，必须清掉
        for noise in ("Article URL", "Comments URL", "Points:", "# Comments"):
            self.assertNotIn(noise, it.summary, f"{noise} 属于模板噪音，应被剥离")

    def test_link_canonicalization(self):
        a = feed.Item(source_id="a", category="c", title="t1", link="https://www.x.com/p/?utm_source=rss")
        b = feed.Item(source_id="a", category="c", title="t2", link="https://x.com/p")
        self.assertEqual(a.uid, b.uid, "去 utm_/www 后应视为同一条")

    def test_atom_relative_link(self):
        items = feed.parse_feed(ATOM_SAMPLE.encode(), src(kind="atom", url="https://simonwillison.net/atom/everything/"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].link, "https://simonwillison.net/2026/Nov/24/charts")
        self.assertNotIn("<b>", items[0].summary)

    def test_arxiv_fields(self):
        items = feed.parse_feed(ARXIV_SAMPLE.encode(), src(kind="arxiv", category="论文速递"))
        it = items[0]
        self.assertEqual(it.link, "http://arxiv.org/abs/2611.12345v1")
        self.assertEqual(it.extra["authors"], ["Alice Zhang", "Bob Li"])
        self.assertEqual(it.extra["cat"], "cs.AI")
        self.assertIn("ToolSmith", it.title)

    def test_broken_xml_tolerated(self):
        bad = b'<?xml version="1.0"?><rss><channel><item><title>A & B</title>' \
              b'<link>https://e.com/1</link></item></channel></rss>'
        items = feed.parse_feed(bad, src())
        self.assertEqual(items[0].title, "A & B")

    def test_clean_text_truncation(self):
        long = "这是一个很长的摘要。" * 40
        out = feed.clean_text(f"<p>{long}</p>", 60)
        self.assertLessEqual(len(out), 70)
        self.assertTrue(out.endswith(("。", "…")))

    def test_byline_stripped_not_body(self):
        raw = ("<p>端侧老炮辛米尔，打通物理AI规模化落地之路</p>"
               "<p>衡宇 发自 凹非寺</p><p>量子位 | 公众号 QbitAI</p>")
        out = feed.clean_text(raw, 200)
        self.assertIn("打通物理AI规模化落地之路", out)
        for noise in ("发自", "量子位", "公众号"):
            self.assertNotIn(noise, out)

    def test_body_mentioning_source_kept(self):
        # 正文里出现"来源""作者"这类词不该被删
        raw = "<p>这套方案来源不明的性能数据，作者本人也没有复现。</p>"
        out = feed.clean_text(raw, 200)
        self.assertIn("来源不明的性能数据", out)
        self.assertIn("作者本人", out)


class SelectTest(unittest.TestCase):
    now = dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.timezone.utc)

    def mk(self, title, hours_ago=1.0, weight=1.0, points=0, source="s1", summary=""):
        return feed.Item(source_id=source, category="c", title=title,
                         link=f"https://e.com/{abs(hash(title))}", summary=summary,
                         published=self.now - dt.timedelta(hours=hours_ago),
                         weight=weight, points=points)

    def test_freshness_beats_stale(self):
        fresh = self.mk("Fresh item about compilers", hours_ago=0.5)
        stale = self.mk("Stale item about compilers", hours_ago=70)
        sel = Select(total_items=2, max_per_source=5)
        self.assertGreater(select.score(fresh, sel, self.now),
                           select.score(stale, sel, self.now) * 1.5)

    def test_points_boost(self):
        a = self.mk("Thing one about rust")
        b = self.mk("Thing two about rust", points=800)
        sel = Select()
        self.assertGreater(select.score(b, sel, self.now), select.score(a, sel, self.now))

    def test_negative_keyword_sinks(self):
        good = self.mk("New inference engine released")
        bad = self.mk("New inference airdrop giveaway")
        sel = Select(negative_keywords={"airdrop": 1.5, "giveaway": 1.5})
        self.assertLess(select.score(bad, sel, self.now),
                        select.score(good, sel, self.now) * 0.5)

    def test_promo_dropped(self):
        items = [self.mk("最新议程与嘉宾阵容公布"),
                 self.mk("某公司急招后端工程师"),
                 self.mk("Rust 1.90 发布，编译器重写")]
        sel = Select(drop_promo=True)
        kept = [i.title for i in select.screen(items, sel)]
        self.assertEqual(kept, ["Rust 1.90 发布，编译器重写"])

    def test_short_title_dropped(self):
        sel = Select(min_title_chars=6)
        self.assertEqual(select.screen([self.mk("abc")], sel), [])

    def test_dedupe_similar_titles(self):
        a = self.mk("OpenAI releases a new small model for laptops")
        b = self.mk("OpenAI releases a new small model for laptop")
        self.assertEqual(len(select.dedupe([a, b])), 1)

    def test_per_source_cap(self):
        words = ["kernels", "parsers", "routers", "caches", "linters", "schedulers",
                 "profilers", "loggers", "proxies", "garbage collectors"]
        items = [self.mk(f"Tuning {w} in production systems", source="hn") for w in words]
        sel = Select(total_items=10, max_per_source=3, min_title_chars=3)
        out = select.pick(items, sel, self.now, [src(id="hn", order=10)])
        self.assertEqual(len(out), 3)

    def test_pick_respects_total_and_order(self):
        papers = ["Sparse attention routing", "Diffusion policy learning",
                  "Graph retrieval benchmarks", "Reward hacking surveys",
                  "Speculative decoding latency"]
        posts = ["Rust async runtime internals", "Postgres index bloat",
                 "Battery life on ARM laptops", "A tour of WebGPU",
                 "Why my side project failed"]
        items = ([self.mk(t, source="arxiv") for t in papers]
                 + [self.mk(t, source="hn") for t in posts])
        sel = Select(total_items=6, max_per_source=4, min_title_chars=3)
        sources = [src(id="hn", order=10), src(id="arxiv", order=30)]
        out = select.pick(items, sel, self.now, sources)
        self.assertEqual(len(out), 6)
        # 输出顺序必须按 order 单调不减（板块顺序稳定），且每源不超上限
        order_of = {"hn": 10, "arxiv": 30}
        keys = [order_of[o.source_id] for o in out]
        self.assertEqual(keys, sorted(keys), "板块顺序应按 order 单调排列")
        self.assertTrue(any(o.source_id == "arxiv" for o in out),
                        "HN 上限用满后 arxiv 必须能补进来，总量不该缩水")
        self.assertTrue(any(o.source_id == "hn" for o in out))
        # 每个源不超过上限
        self.assertLessEqual(sum(1 for o in out if o.source_id == "hn"), 4)

    def test_undated_short_title_treated_fresh(self):
        gh = feed.Item(source_id="gh", category="开源趋势",
                       title="nginx/nginx", link="https://github.com/nginx/nginx")
        old = feed.Item(source_id="rss", category="x",
                        title="A very long undated article title here",
                        link="https://e.com/z")
        sel = Select()
        self.assertGreater(select.score(gh, sel, self.now),
                           select.score(old, sel, self.now))


if __name__ == "__main__":
    unittest.main()