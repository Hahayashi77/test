"""生成一张"模拟真实输出"的预览页，用来看排版（不调用任何 API）。

它做的事：抓真实信源 → 真实打分挑选 → 用规则生成近似模型的解读文本 → 渲染到 site/。
用途仅限预览；线上请用 `python -m brief build`（真模型）。
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brief import cli, config as cfgmod, feed, llm, render, select  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 按关键词给"为什么值得看"挑一句像人写的判断（预览用，不是真分析）
WHY_RULES = [
    (r"(release|released|v?\d+\.\d+|版本|发布)", "关注升级点与破坏性变更，评估是否值得跟进。"),
    (r"(security|cve|漏洞|攻击|exploit)", "安全口径变化，建议排查自身依赖是否受影响。"),
    (r"(benchmark|性能|speed|faster|latency|吞吐)", "核对测试条件与硬件，别直接照搬结论。"),
    (r"(agent|智能体|tool|工具调用)", "留意它和现有 agent 工作流的兼容边界。"),
    (r"(paper|arXiv|论文|study|研究)", "先看摘要与实验设置，判断能否复用到自己的场景。"),
    (r"(open[- ]?source|开源|repo|github|trending)", "看 star 曲线的真实性，再看提交节奏和维护者。"),
    (r"(price|pricing|cost|费用|成本)", "直接影响预算，值得和现有方案对比。"),
    (r"(rust|go|c\+\+|compiler|编译器)", "工程实现细节较多，适合需要优化性能的场景。"),
]


def fake_what(item) -> str:
    base = item.summary or item.title
    base = re.sub(r"\s+", " ", base)
    if len(base) > 110:
        base = base[:110].rstrip() + "…"
    return base


def fake_why(item) -> str:
    hay = f"{item.title} {item.summary}".lower()
    for pattern, text in WHY_RULES:
        if re.search(pattern, hay):
            return text
    return "先判断它与自己手头工作的相关性，再决定要不要深读。"


def fake_tag(item) -> str:
    hay = f"{item.title} {item.summary}".lower()
    for pattern, tag in [
        (r"(paper|arxiv|研究|论文)", "论文"),
        (r"(security|cve|漏洞)", "安全"),
        (r"(release|released|版本|发布)", "版本"),
        (r"(agent|智能体)", "Agent"),
        (r"(model|模型|llm|qwen|glm|gpt)", "模型"),
        (r"(github|repo|开源|trending)", "开源"),
        (r"(price|pricing|成本|费用)", "商业"),
    ]:
        if re.search(pattern, hay):
            return tag
    return item.category


def build_preview():
    cfg = cfgmod.load(ROOT / "config.toml")
    today = dt.date.today()
    now = dt.datetime.now(dt.timezone.utc)

    raw, status = cli.load_all(cfg, use_cache=True)
    items = select.pick(raw, cfg.select, now, cfg.sources)
    if not items:
        print("没有抓到条目，无法生成预览")
        return 1

    filled = feed.enrich_summaries(items, max_items=cfg.select.enrich_max,
                                   excerpt_chars=cfg.model.excerpt_chars)
    print(f"摘要补全 {filled} 条")

    analyses = {}
    for it in items:
        analyses[it.uid] = {"what": fake_what(it), "why": fake_why(it), "tag": fake_tag(it)}

    digest_meta = {
        "lede": (f"今天共 {len(items)} 条：社区最热的是 {items[0].title}，"
                 f"开源榜上 {items[len(items)//3].title} 值得扫一眼；"
                 f"论文侧集中在推理效率与 agent 工具链。"),
        "takeaway": "排版预览用的示意文本 —— 正式运行时由模型写出当日判断。",
        "degraded": False,
    }
    meta = {
        "generated_at": dt.datetime.now(llm.BEIJING).strftime("%Y-%m-%d %H:%M"),
        "sources_ok": sum(1 for s in status if s["got"]),
        "sources": status,
        "usage": llm.Usage().as_dict(),
        "model": "preview（未调用模型）",
    }
    SITE = ROOT / "site"
    (SITE / "d").mkdir(parents=True, exist_ok=True)
    (SITE / "data").mkdir(parents=True, exist_ok=True)
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    page = render.digest_page(cfg, today, items, analyses, meta, digest_meta)
    (SITE / "index.html").write_text(page, encoding="utf-8")
    (SITE / "d" / f"{today.isoformat()}.html").write_text(page, encoding="utf-8")
    render.write_json(SITE / "data" / "archive.json", {"entries": [
        {"date": today.isoformat(), "lede": digest_meta["lede"],
         "takeaway": digest_meta["takeaway"], "count": len(items)}]})
    (SITE / "archive.html").write_text(render.archive_page(
        cfg, [{"date": today.isoformat(), "lede": digest_meta["lede"], "count": len(items)}]),
        encoding="utf-8")
    (SITE / "feed.xml").write_text(render.rss_feed(
        cfg, [{"date": today.isoformat(), "lede": digest_meta["lede"]}]), encoding="utf-8")
    print(f"预览已生成：{SITE / 'index.html'}（{len(items)} 条）")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(build_preview())