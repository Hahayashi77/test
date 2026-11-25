"""信源抓取与解析：RSS / Atom / arXiv（全部用标准库）。"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

UA = "daily-brief/1.0 (+https://github.com/) Python-urllib"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
# HN RSS 的模板噪音：这些是元数据，不是正文，留着只会挤掉真正的内容
_BOILER_RE = re.compile(
    r"(Article URL:\s*\S+|Comments URL:\s*\S+|#\s*Comments:\s*\d+|Points:\s*\d+\s*$)",
    re.I | re.M,
)
_URL_ONLY_RE = re.compile(r"^(https?://\S+\s*)+$")
# 中文公众号/媒体常见的署名、来源、导流尾巴——对解读毫无信息量。
# 注意：只在"短整行"上判定，避免把正文里的"作者""来源"等词误删。
_BYLINE_STRICT = re.compile(
    r"^("
    r"(作者|编辑|责编|来源|文|编译|整理|审校|校对|author|posted by|via|source)"
    r"\s*[:：丨|]\s*.{1,25}$"
    r"|发自\s*\S{1,10}"
    r"|(量子位|机器之心|新智元|量子学派)\s*\|?\s*[\w\s]*"
    r"|公众号\s*[:：]?\s*.{0,20}"
    r"|(点击|扫码|长按)\s*.{0,10}(关注|阅读|查看|报名)"
    r"|本文来自\s*.{1,20}"
    r"|原文链接\s*[:：]?\s*\S*"
    r")$",
    re.I,
)
# 行首署名（"衡宇 发自 凹非寺"）：只在开头出现时删掉
_BYLINE_PREFIX = re.compile(r"^(发自\s*\S{1,10}\s*|\S{1,6}\s+发自\s*\S{1,10}\s*)")


@dataclass
class Item:
    source_id: str
    category: str
    title: str
    link: str
    summary: str = ""
    published: dt.datetime | None = None
    weight: float = 1.0
    points: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        """稳定的去重键：链接归一化后取哈希（去掉 utm 等噪音参数）。"""
        return hashlib.sha1(_canonical_link(self.link).encode("utf-8")).hexdigest()[:16]


def _canonical_link(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url.strip())
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False)
             if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid", "spm"))]
        path = p.path.rstrip("/") or "/"
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return urllib.parse.urlunsplit((p.scheme or "https", netloc, path,
                                        urllib.parse.urlencode(q), ""))
    except Exception:
        return url.strip()


def _strip_byline(text: str) -> str:
    """去掉署名 / 来源 / 导流尾巴，但别把整段正文误删。

    策略：整行严格匹配"署名式"才丢；行首的"XX 发自 XX"只删前缀；
    其余内容一律保留 —— 误删正文比留一点噪音代价大得多。
    """
    kept: list[str] = []
    for line in text.splitlines():
        compact = re.sub(r"\s+", " ", line).strip()
        if not compact:
            continue
        if len(compact) <= 40 and _BYLINE_STRICT.match(compact):
            continue
        compact = _BYLINE_PREFIX.sub("", compact).strip(" 　|丨:：")
        if compact:
            kept.append(compact)
    return "\n".join(kept)


def clean_text(raw: str, limit: int = 0) -> str:
    """HTML → 纯文本：去脚本、解实体、压空白、剔掉 RSS 模板噪音。"""
    if not raw:
        return ""
    txt = _SCRIPT_RE.sub(" ", raw)
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", txt, flags=re.I)
    txt = _TAG_RE.sub(" ", txt)
    txt = html.unescape(txt)
    txt = _WS_RE.sub(" ", txt)
    txt = _BOILER_RE.sub("", txt)
    txt = "\n".join(line.strip() for line in txt.splitlines())
    # 剩下全是裸链接的行也没有信息量
    txt = "\n".join(line for line in txt.splitlines() if not _URL_ONLY_RE.match(line))
    txt = _strip_byline(txt)
    txt = _NL_RE.sub("\n\n", txt).strip()
    if limit and len(txt) > limit:
        cut = txt[:limit]
        # 尽量在句末断开
        for sep in ("。", "！", "？", ". ", "! ", "? ", "；", "; "):
            idx = cut.rfind(sep)
            if idx > limit * 0.55:
                return cut[: idx + len(sep)].strip()
        return cut.rstrip() + "…"
    return txt


# ── HTTP ────────────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 25, retries: int = 2) -> bytes:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except Exception as exc:  # 网络抖动 / 429 / DNS
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url} :: {last}")


# ── 解析 ────────────────────────────────────────────────────────────────────

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _find_text(node: ET.Element, *names: str) -> str:
    for child in node.iter():
        if _localname(child.tag) in names and (child.text or "").strip():
            return child.text or ""
    return ""


def _parse_date(value: str) -> dt.datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = parsedate_to_datetime(value)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            d = dt.datetime.strptime(value.replace("Z", "+0000") if fmt.endswith("%z") else value, fmt)
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
    return None


def _pick_link(node: ET.Element, base: str = "") -> str:
    """RSS <link> 与 Atom <link rel=alternate> 都要兼容。"""
    # Atom: 优先 rel=alternate
    for child in node:
        if _localname(child.tag) == "link":
            rel = (child.get("rel") or "alternate").lower()
            href = child.get("href")
            if href and rel in ("alternate", ""):
                return _abs(href, base)
    # RSS: <link> 是文本
    for child in node:
        if _localname(child.tag) == "link" and (child.text or "").strip():
            return _abs(child.text or "", base)
    # arXiv 的特例：<id> 就是摘要页地址
    ident = _find_text(node, "id")
    if ident.startswith("http"):
        return _abs(ident, base)
    for child in node:
        if _localname(child.tag) == "guid" and (child.text or "").startswith("http"):
            return _abs(child.text or "", base)
    return ""


def _abs(href: str, base: str) -> str:
    href = href.strip()
    if not href:
        return ""
    try:
        return urllib.parse.urljoin(base, href)
    except Exception:
        return href


_POINTS_RE = re.compile(r"(?:Points|points)\s*[:：]\s*(\d+)")


def parse_feed(data: bytes, source, excerpt_chars: int = 420) -> list[Item]:
    """解析 RSS 2.0 / Atom / arXiv API 返回。单条坏掉不影响整体。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # 有些源末尾有垃圾字符，退一步做一次容错修补
        text = data.decode("utf-8", "replace")
        text = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        root = ET.fromstring(text)

    entries = [n for n in root.iter() if _localname(n.tag) in ("item", "entry")]
    items: list[Item] = []
    for node in entries[: max(1, source.limit)]:
        try:
            title = clean_text(_find_text(node, "title"))
            link = _pick_link(node, source.url)
            desc = _find_text(node, "description", "summary", "content")
            published = None
            for tag in ("pubdate", "published", "updated", "date"):
                published = _parse_date(_find_text(node, tag))
                if published:
                    break

            summary = clean_text(desc, excerpt_chars)

            points = 0
            m = _POINTS_RE.search(desc or "")
            if m:
                points = int(m.group(1))
            if source.min_points and points < source.min_points:
                continue
            if not title or not link:
                continue

            extra = {}
            authors = [a.text.strip() for a in node.iter()
                       if _localname(a.tag) == "name" and (a.text or "").strip()]
            if authors:
                extra["authors"] = authors[:4]
            # arXiv 分类
            for c in node.iter():
                if _localname(c.tag) == "primary_category" and c.get("term"):
                    extra["cat"] = c.get("term")
                    break

            items.append(Item(
                source_id=source.id,
                category=source.category,
                title=title,
                link=link,
                summary=summary,
                published=published,
                weight=source.weight,
                points=points,
                extra=extra,
            ))
        except Exception:
            continue
    return items


def load_source(source, excerpt_chars: int = 420) -> tuple[list[Item], str | None]:
    """返回 (条目, 错误信息)。错误不抛出，交给上层记录到报告里。"""
    try:
        if source.kind == "arxiv":
            url = _arxiv_url(source)
        else:
            url = source.url
        data = fetch(url)
        items = parse_feed(data, source, excerpt_chars)
        return items, None
    except Exception as exc:
        return [], str(exc)[:200]


def _arxiv_url(source) -> str:
    cats = [c.strip() for c in (source.cats or "cs.AI").split(",") if c.strip()]
    query = " OR ".join(f"cat:{c}" for c in cats)
    params = {
        "search_query": query,
        "start": "0",
        "max_results": str(max(1, min(source.limit, 50))),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    return f"{source.url}?{urllib.parse.urlencode(params)}"


# ── 摘要补全 ────────────────────────────────────────────────────────────────
# 有些源（典型是 HN）只给标题 + 元数据，没有正文。抓一下原文页的 meta description
# 能显著提升解读质量，成本是每天十来个额外请求。

_META_PATTERNS = [
    re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description', re.I),
    re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description', re.I),
]
_SKIP_EXT = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mp3", ".svg", ".webp")


def summarize_page(url: str, limit: int = 400, timeout: int = 8) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return ""
        if parts.path.lower().endswith(_SKIP_EXT):
            return ""
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype:
                return ""
            raw = resp.read(200_000)
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        text = raw.decode("utf-8", "replace")
        for pat in _META_PATTERNS:
            m = pat.search(text)
            if m:
                desc = clean_text(m.group(1), limit)
                if len(desc) >= 40:
                    return desc
        # 没有 meta description：退一步抓正文第一段
        body = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", text)
        m = re.search(r"(?is)<p[^>]*>(.{60,600}?)</p>", body)
        if m:
            return clean_text(m.group(1), limit)
    except Exception:
        return ""
    return ""


def enrich_summaries(items, max_items: int = 10, min_chars: int = 60,
                     excerpt_chars: int = 400) -> int:
    """给摘要太短的条目补 meta description（并发，失败静默）。返回补全条数。"""
    targets = [it for it in items if len(it.summary) < min_chars][:max_items]
    if not targets:
        return 0

    def work(item):
        desc = summarize_page(item.link, excerpt_chars)
        if desc:
            item.summary = desc
            item.extra["enriched"] = True
            return 1
        return 0

    with ThreadPoolExecutor(max_workers=5) as pool:
        return sum(pool.map(work, targets))