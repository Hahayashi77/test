"""打分、去重、挑选：把候选池压成每期固定的 N 条。"""

from __future__ import annotations

import datetime as dt
import math
import re
from difflib import SequenceMatcher

from .config import Select
from .feed import Item

# 标题很短、且没带日期的条目，多半是 GitHub Trending 这类"无摘要无时间"的榜单，
# 新鲜度按当日算（它们本来就是今天的榜）
DATED_SHORT_TITLE = 14

# 推广 / 会议 / 招聘类噪音：标题命中就丢
PROMO_RE = re.compile(
    r"(议程|嘉宾|报名|直播预约|峰会|论坛|大会|年会|展会|发布会|报名中|"
    r"限时|免费领|优惠|促销|"
    r"招人|内推|招聘|诚聘|急招|招募|应聘|简历|薪资|"
    r"webinar|register now|early bird|call for papers|sponsor|promo code|we'?re hiring|"
    r"now hiring|job opening)",
    re.I,
)


def _age_hours(published: dt.datetime | None, now: dt.datetime, title: str = "") -> float:
    if not published:
        return 1.0 if len(title) < DATED_SHORT_TITLE else 24.0
    delta = (now - published).total_seconds() / 3600.0
    return max(0.0, min(delta, 24 * 7))


def score(item: Item, sel: Select, now: dt.datetime) -> float:
    """分数 = 信源权重 × 新鲜度 × 热度 × 关键词加成。

    全部是乘法项，保证任何一项为 0 都会沉底（例如被负向关键词命中）。
    """
    # 1) 信源权重
    s = max(0.05, item.weight)

    # 2) 新鲜度：24h 内近似线性衰减，24h 外快速掉
    age = _age_hours(item.published, now, item.title)
    if age <= sel.recency_hours:
        freshness = 1.0 + 0.35 * (1.0 - age / max(1, sel.recency_hours))
    else:
        freshness = math.exp(-(age - sel.recency_hours) / 36.0)

    # 3) 热度：只有带 points 的源（HN）才有
    heat = 1.0
    if item.points:
        heat = 1.0 + min(1.2, math.log10(1 + item.points) / 2.6)

    # 4) 关键词：正文 + 标题一起看
    haystack = f"{item.title} {item.summary}".lower()
    kw_bonus = 0.0
    for kw, w in sel.keywords.items():
        if kw in haystack:
            kw_bonus += w * 0.25
    neg = 0.0
    for kw, w in sel.negative_keywords.items():
        if kw in haystack:
            neg += w

    s *= freshness * heat * (1.0 + min(1.5, kw_bonus))
    s *= max(0.0, 1.0 - min(1.0, neg * 0.6))
    return round(s, 4)


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def dedupe(items: list[Item], threshold: float = 0.86) -> list[Item]:
    """先按链接 uid 去重，再按标题相似度折叠（同一新闻被多个源转载）。"""
    seen_uid: set[str] = set()
    kept: list[Item] = []
    for item in items:
        if item.uid in seen_uid:
            continue
        seen_uid.add(item.uid)
        dup = False
        for k in kept:
            if _similar(item.title.lower(), k.title.lower()) >= threshold:
                dup = True
                # 保留信息更全的那条，但把热度叠加过去
                if len(item.summary) > len(k.summary) * 1.3:
                    k.summary = item.summary
                k.points = max(k.points, item.points)
                k.extra.setdefault("also", []).append(item.link)
                break
        if not dup:
            kept.append(item)
    return kept


def screen(items: list[Item], sel: Select) -> list[Item]:
    """粗筛：太短的标题、推广/会议/招聘类内容、明显无关的源内噪音。"""
    out: list[Item] = []
    for it in items:
        if len(it.title) < sel.min_title_chars:
            continue
        if sel.drop_promo and PROMO_RE.search(it.title):
            continue
        out.append(it)
    return out


def pick(items: list[Item], sel: Select, now: dt.datetime,
         sources: list | None = None) -> list[Item]:
    """粗筛 → 去重 → 打分 → 按源限流 → 截断到 total_items → 按信源顺序分组。"""
    scored = [(score(it, sel, now), it) for it in dedupe(screen(items, sel))]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    per_source: dict[str, int] = {}
    chosen: list[tuple[float, Item]] = []
    for s, it in scored:
        if len(chosen) >= sel.total_items:
            break
        if per_source.get(it.source_id, 0) >= sel.max_per_source:
            continue
        per_source[it.source_id] = per_source.get(it.source_id, 0) + 1
        it.extra["score"] = s
        chosen.append((s, it))

    # 输出按信源 order → 分数排，保证板块顺序稳定（不随当天热度乱跳）
    orders = {s.id: getattr(s, "order", 50) for s in (sources or [])}
    chosen.sort(key=lambda pair: (orders.get(pair[1].source_id, 50), -pair[0]))
    return [it for _, it in chosen]