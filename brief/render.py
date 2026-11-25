"""静态站渲染：HTML 日报、归档页、RSS、JSON 快照。

纯字符串拼接，不引模板引擎 —— 一个文件跑到底，部署只要拷 site/ 目录。
"""

from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

from .feed import Item

CN_WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

CSS = """
:root{
  --ink:#14181f; --ink-2:#3d4653; --ink-3:#6b7686;
  --bg:#fbfaf7; --card:#ffffff; --line:#e6e2da;
  --accent:#0f62fe; --accent-soft:#e8f0ff;
  --whale:#2b6cb0; --warm:#b45309;
  --serif:"Iowan Old Style","Charter","Songti SC","Noto Serif CJK SC",Georgia,serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",
         "Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  --mono:"SF Mono","JetBrains Mono",Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme: dark){
  :root{ --ink:#e9eaee; --ink-2:#b6bcc9; --ink-3:#8b94a5;
    --bg:#0f1216; --card:#161a20; --line:#262b33;
    --accent:#6ea8ff; --accent-soft:#1b2637; --whale:#7aa7d9; --warm:#e0a458; }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:var(--sans);font-size:16px;line-height:1.7;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:3px}
.wrap{max-width:760px;margin:0 auto;padding:0 22px 72px}

/* ── 报头 ── */
header.top{padding:34px 0 22px;border-bottom:1px solid var(--line);margin-bottom:30px}
.masthead{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.masthead h1{font-family:var(--serif);font-size:30px;margin:0;letter-spacing:-.4px}
.masthead .date{color:var(--ink-3);font-size:13px;font-variant-numeric:tabular-nums}
.masthead nav{margin-left:auto;font-size:13px;display:flex;gap:16px}
.tagline{color:var(--ink-2);font-size:14px;margin-top:6px}

/* ── 导语 ── */
.lede{font-family:var(--serif);font-size:18.5px;line-height:1.72;color:var(--ink);
  margin:0 0 26px;padding-left:16px;border-left:3px solid var(--accent)}
.lede .lbl{display:block;font-family:var(--sans);font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--ink-3);margin-bottom:4px}

/* ── 板块 ── */
.section{margin:38px 0 0}
.section h2{font-size:13px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--ink-3);font-weight:600;margin:0 0 14px;padding-bottom:8px;
  border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
.section h2 .count{font-variant-numeric:tabular-nums;color:var(--accent);
  background:var(--accent-soft);border-radius:20px;padding:1px 8px;
  font-size:11px;letter-spacing:0}

/* ── 条目 ── */
.item{padding:16px 0 18px;border-bottom:1px dashed var(--line)}
.item:last-child{border-bottom:0}
.item-head{display:flex;gap:10px;align-items:baseline}
.rank{font-family:var(--mono);font-size:12px;color:var(--ink-3);
  min-width:22px;padding-top:3px;font-variant-numeric:tabular-nums}
.title{font-size:17px;font-weight:600;line-height:1.45;letter-spacing:-.1px;margin:0}
.title a{color:var(--ink)}
.title a:hover{color:var(--accent);text-decoration:none}
.meta{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--ink-3);
  margin:6px 0 0 32px;font-variant-numeric:tabular-nums}
.meta .tag{color:var(--whale);background:var(--accent-soft);border-radius:4px;
  padding:1px 7px;font-size:11px;font-weight:600;letter-spacing:.02em}
.body{margin:8px 0 0 32px}
.what{color:var(--ink-2);font-size:14.5px;margin:0}
.why{color:var(--ink-2);font-size:13.5px;margin:7px 0 0;padding-left:11px;
  border-left:2px solid var(--line)}
.why b{color:var(--warm);font-weight:600;font-size:12px;letter-spacing:.06em}
.raw{color:var(--ink-3);font-size:13.5px;margin:0}
.authors{color:var(--ink-3);font-size:12.5px;margin-top:5px}

/* ── 今日判断 / 页脚 ── */
.takeaway{margin:44px 0 0;padding:20px 22px;background:var(--card);
  border:1px solid var(--line);border-radius:10px}
.takeaway .lbl{font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--warm);font-weight:700}
.takeaway p{margin:8px 0 0;font-size:15.5px;line-height:1.65}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);
  font-size:12px;color:var(--ink-3);display:flex;gap:16px;flex-wrap:wrap;
  font-variant-numeric:tabular-nums}
footer a{color:var(--ink-3);text-decoration:underline}
.degraded{background:#fff8e6;border:1px solid #f0d9a8;color:#7a5a12;
  padding:10px 14px;border-radius:8px;font-size:13px;margin-bottom:24px}
@media (prefers-color-scheme: dark){.degraded{background:#2a2415;border-color:#4a3d1d;color:#e0bf7a}}

/* ── 归档 ── */
.arc-list{list-style:none;padding:0;margin:0}
.arc-list li{display:flex;gap:14px;align-items:baseline;padding:11px 0;
  border-bottom:1px solid var(--line);font-size:15px}
.arc-list .d{font-family:var(--mono);font-size:12.5px;color:var(--ink-3);min-width:88px}
.arc-list .t{color:var(--ink-2);font-size:13.5px;margin-left:auto;text-align:right;
  max-width:52%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:640px){
  .masthead h1{font-size:24px}
  .masthead nav{width:100%;margin-left:0}
  .body,.meta{margin-left:0}
  .rank{display:none}
  .arc-list .t{display:none}
}
"""


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _fmt_date(d: dt.date) -> str:
    return f"{d.year} 年 {d.month} 月 {d.day} 日 · 星期{CN_WEEKDAYS[d.weekday()]}"


def _page(title: str, body: str, site_title: str, extra_head: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(title)}</title>
<meta property="og:title" content="{esc(title)}">
{extra_head}
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""


def _header(site, date: dt.date, nav: str = "") -> str:
    nav_html = nav or '<a href="archive.html">归档</a><a href="feed.xml">RSS</a>'
    return f"""<header class="top">
  <div class="masthead">
    <h1>{esc(site.title)}</h1>
    <span class="date">{_fmt_date(date)}</span>
    <nav>{nav_html}</nav>
  </div>
  <div class="tagline">{esc(site.tagline)}</div>
</header>"""


def _item_html(idx: int, item: Item, analysis: dict | None) -> str:
    tag = (analysis or {}).get("tag") or ""
    what = (analysis or {}).get("what") or ""
    why = (analysis or {}).get("why") or ""
    bits = [f'<span>{esc(item.category)}</span>']
    if item.points:
        bits.append(f'<span>HN {item.points} 分</span>')
    if item.extra.get("cat"):
        bits.append(f'<span>{esc(item.extra["cat"])}</span>')
    if tag:
        bits.insert(0, f'<span class="tag">{esc(tag)}</span>')

    if what:
        body = f'<p class="what">{esc(what)}</p>'
        if why:
            body += f'<p class="why"><b>为什么值得看</b> {esc(why)}</p>'
    else:
        body = f'<p class="raw">{esc(item.summary or "（无摘要）")}</p>'

    authors = ""
    if item.extra.get("authors"):
        authors = f'<div class="authors">{esc("、".join(item.extra["authors"]))}</div>'

    return f"""<article class="item">
  <div class="item-head">
    <span class="rank">{idx:02d}</span>
    <h3 class="title"><a href="{esc(item.link)}" target="_blank" rel="noopener">{esc(item.title)}</a></h3>
  </div>
  <div class="meta">{"".join(bits)}</div>
  <div class="body">{body}{authors}</div>
</article>"""


def digest_page(cfg, date: dt.date, items: list[Item], analyses: dict,
                meta: dict, digest_meta: dict, is_latest: bool = True) -> str:
    """一期日报的完整 HTML。"""
    sections: dict[str, list[tuple[int, Item]]] = {}
    for i, it in enumerate(items, 1):
        sections.setdefault(it.category, []).append((i, it))

    parts = []
    lede = digest_meta.get("lede", "")
    if lede:
        parts.append(f'<div class="lede"><span class="lbl">今日概述</span>{esc(lede)}</div>')
    if digest_meta.get("degraded"):
        parts.append('<div class="degraded">本次运行未使用模型（预算上限或 API 不可用），'
                     '以下为未经解读的原始标题列表。</div>')

    for cat, group in sections.items():
        inner = "\n".join(_item_html(i, it, analyses.get(it.uid)) for i, it in group)
        parts.append(f'<section class="section"><h2>{esc(cat)}'
                     f'<span class="count">{len(group)}</span></h2>{inner}</section>')

    take = digest_meta.get("takeaway", "")
    if take:
        parts.append(f'<div class="takeaway"><div class="lbl">今日判断</div>'
                     f'<p>{esc(take)}</p></div>')

    usage = meta.get("usage", {})
    footer = (
        f'<footer>'
        f'<span>{len(items)} 条 · 来自 {meta.get("sources_ok", 0)} 个信源</span>'
        f'<span>{usage.get("requests", 0)} 次模型调用</span>'
        f'<span>${usage.get("cost_usd", 0):.4f}</span>'
        f'<span>生成于 {meta.get("generated_at", "")}</span>'
        f'<span><a href="index.html">最新一期</a></span>'
        f'</footer>'
    )

    nav = ('<a href="index.html">最新</a><a href="archive.html">归档</a><a href="feed.xml">RSS</a>'
           if not is_latest else '<a href="archive.html">归档</a><a href="feed.xml">RSS</a>')
    body = _header(cfg.site, date, nav) + "\n".join(parts) + footer
    canonical = f"{cfg.site.base_url.rstrip('/')}/d/{date.isoformat()}.html" if cfg.site.base_url else ""
    extra = f'<link rel="canonical" href="{esc(canonical)}">' if canonical else ""
    return _page(f"{cfg.site.title} · {date.isoformat()}", body, cfg.site.title, extra)


def archive_page(cfg, entries: list[dict]) -> str:
    rows = "\n".join(
        f'<li><span class="d">{esc(e["date"])}</span>'
        f'<a href="d/{esc(e["date"])}.html">{esc(e.get("lede") or "（无概述）")}</a>'
        f'<span class="t">{e.get("count", 0)} 条</span></li>'
        for e in entries
    )
    body = (_header(cfg.site, dt.date.today(), '<a href="index.html">最新</a><a href="feed.xml">RSS</a>')
            + f'<h2 style="font-size:13px;letter-spacing:.16em;text-transform:uppercase;'
              f'color:var(--ink-3);margin:0 0 6px">全部期数</h2>'
              f'<ul class="arc-list">{rows}</ul>'
            + f'<footer><span>共 {len(entries)} 期</span></footer>')
    return _page(f"{cfg.site.title} · 归档", body, cfg.site.title)


def rss_feed(cfg, entries: list[dict]) -> str:
    base = cfg.site.base_url.rstrip("/")
    now = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = []
    for e in entries[:20]:
        link = f"{base}/d/{e['date']}.html" if base else f"d/{e['date']}.html"
        desc = esc(e.get("lede") or "")
        try:
            stamp = dt.datetime.fromisoformat(e["date"] + "T08:00:00+08:00")
            pub = stamp.astimezone(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            pub = now
        items.append(
            f"<item><title>{esc(cfg.site.title + ' · ' + e['date'])}</title>"
            f"<link>{esc(link)}</link>"
            f"<guid isPermaLink=\"true\">{esc(link)}</guid>"
            f"<pubDate>{pub}</pubDate>"
            f"<description>{desc}</description></item>"
        )
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<rss version="2.0"><channel>'
            f'<title>{esc(cfg.site.title)}</title>'
            f'<link>{esc(base or ".")}</link>'
            f'<description>{esc(cfg.site.tagline)}</description>'
            f'<language>zh-CN</language><lastBuildDate>{now}</lastBuildDate>'
            f'{"".join(items)}</channel></rss>\n')


def snapshot(date: dt.date, items: list[Item], analyses: dict,
             meta: dict, digest_meta: dict) -> dict:
    """机器可读快照：换前端、做二次分析、喂给别的 agent 都用得上。"""
    return {
        "date": date.isoformat(),
        "generated_at": meta.get("generated_at"),
        "lede": digest_meta.get("lede", ""),
        "takeaway": digest_meta.get("takeaway", ""),
        "degraded": bool(digest_meta.get("degraded")),
        "usage": meta.get("usage", {}),
        "sources": meta.get("sources", []),
        "items": [
            {
                "title": it.title,
                "url": it.link,
                "category": it.category,
                "source": it.source_id,
                "published": it.published.isoformat() if it.published else None,
                "points": it.points,
                "score": it.extra.get("score"),
                "summary": it.summary,
                "authors": it.extra.get("authors", []),
                "analysis": analyses.get(it.uid),
            }
            for it in items
        ],
    }


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")