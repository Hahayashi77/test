"""命令行入口：抓取 → 挑选 → 分析 → 渲染 一条龙。

用法：
    python -m brief build                 # 生成今天的日报
    python -m brief build --dry-run       # 只抓取+挑选，不调用模型（0 成本）
    python -m brief build --mock          # 用假数据跑通全流程，不需要 API Key
    python -m brief build --date 2026-11-24 --force
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as cfgmod
from . import feed, llm, render, select

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── 抓取 ────────────────────────────────────────────────────────────────────

def load_all(cfg, use_cache: bool = False) -> tuple[list[feed.Item], list[dict]]:
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    sources = [s for s in cfg.sources if s.enabled]
    results: dict[str, tuple[list[feed.Item], str | None]] = {}

    def work(src):
        return src.id, feed.load_source(src, cfg.model.excerpt_chars)

    with ThreadPoolExecutor(max_workers=6) as pool:
        for sid, res in pool.map(work, sources):
            results[sid] = res

    items: list[feed.Item] = []
    status: list[dict] = []
    for src in sources:
        got, err = results.get(src.id, ([], "not run"))
        # 原始快照：出问题时可复现，不依赖网络
        cache_file = cfg.cache_dir / f"{src.id}.json"
        if got:
            render.write_json(cache_file, {
                "source": src.id, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "items": [{"title": i.title, "link": i.link, "summary": i.summary,
                           "published": i.published.isoformat() if i.published else None,
                           "points": i.points, "cat": i.category, "extra": i.extra}
                          for i in got],
            })
        elif use_cache and cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            got = [feed.Item(source_id=src.id, category=src.category, title=c["title"],
                             link=c["link"], summary=c["summary"],
                             published=(dt.datetime.fromisoformat(c["published"])
                                        if c.get("published") else None),
                             weight=src.weight, points=c.get("points", 0),
                             extra=c.get("extra", {}))
                   for c in cached.get("items", [])]
            err = f"{err}（已用本地缓存）" if err else None
        items.extend(got)
        status.append({"source": src.id, "category": src.category,
                       "got": len(got), "error": err})
        flag = "OK " if got else "ERR"
        log(f"{flag} {src.id:<16} {len(got):>3} 条" + (f"  ← {err}" if err else ""))
    return items, status


# ── 分析 ────────────────────────────────────────────────────────────────────

def analyze_all(client, cfg, items) -> tuple[dict, dict]:
    analyses: dict[str, dict] = {}
    digest_meta: dict = {"lede": "", "takeaway": "", "degraded": False}
    batches = [items[i:i + cfg.model.batch_size]
               for i in range(0, len(items), cfg.model.batch_size)]
    ledes: list[str] = []
    for n, batch in enumerate(batches, 1):
        left = cfg.budget.max_usd_per_run - client.usage.cost_usd
        if left <= 0:
            log(f"预算已用完（${client.usage.cost_usd:.4f}），剩余批次跳过")
            digest_meta["degraded"] = True
            break
        try:
            t0 = time.time()
            res = client.analyze(batch, left)
            meta = res.pop("__meta__", {}) or {}
            if not res:
                # 空结果只可能是预算不够（analyze 内部已拒绝发请求），此时别再往下刷
                log(f"  批次 {n} 超出剩余预算，停止调用（累计 ${client.usage.cost_usd:.4f}）")
                digest_meta["degraded"] = True
                break
            analyses.update(res)
            if meta.get("lede"):
                ledes.append(meta["lede"])
            if meta.get("takeaway") and not digest_meta["takeaway"]:
                digest_meta["takeaway"] = meta["takeaway"]
            log(f"  批次 {n}/{len(batches)} · {len(res)}/{len(batch)} 条解读 · "
                f"{time.time() - t0:.1f}s · 累计 ${client.usage.cost_usd:.4f}")
        except Exception as exc:
            client.usage.failures += 1
            log(f"  批次 {n} 失败：{exc}")
            digest_meta["degraded"] = True
    if ledes:
        digest_meta["lede"] = ledes[0]
    return analyses, digest_meta


# ── mock（离线演练）──────────────────────────────────────────────────────────

def mock_analyses(items) -> tuple[dict, dict]:
    """演练用占位解读：结构和真实输出一致，方便看排版；文本本身是模板。"""
    out = {}
    for it in items:
        out[it.uid] = {
            "what": f"{it.title} —— 演练占位文本，真实运行时这里是模型对该条目 40-70 字的客观转述。",
            "why": "真实运行时这里是‘对开发者意味着什么’的一句判断。",
            "tag": "演练",
        }
    return out, {
        "lede": "演练模式：没有调用模型，以下解读均为占位文本，仅用于检查排版、板块结构和部署链路。",
        "takeaway": "演练占位：上线后这里放当天最值得深挖的一条判断。",
        "degraded": True,
        "reason": "mock",
    }

# ── 主流程 ──────────────────────────────────────────────────────────────────

def build(args) -> int:
    cfg = cfgmod.load(args.config)
    site_dir = Path(args.out) if getattr(args, "out", None) else SITE
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    now = dt.datetime.now(dt.timezone.utc)

    log(f"配置：{cfg.site.title}｜模型 {cfg.model.model}｜目标 {cfg.select.total_items} 条"
        f"｜峰时 {'是' if llm.is_peak() else '否'}")

    if getattr(args, "no_fetch", False):
        raw_items, status = _items_from_cache(cfg)
        log(f"跳过抓取，改用 .cache 快照：{len(raw_items)} 条")
    else:
        raw_items, status = load_all(cfg, use_cache=args.offline)
    items = select.pick(raw_items, cfg.select, now, cfg.sources)
    log(f"候选 {len(raw_items)} 条 → 入选 {len(items)} 条")
    if not items:
        log("没有任何条目，退出（不覆盖线上内容）")
        return 2

    # 摘要太短的（典型是 HN 只有标题）去原文页补一段 meta description，提升解读质量
    if not getattr(args, "no_enrich", False):
        try:
            filled = feed.enrich_summaries(items, max_items=cfg.select.enrich_max,
                                           excerpt_chars=cfg.model.excerpt_chars)
            log(f"摘要补全：{filled} 条")
        except Exception as exc:
            log(f"摘要补全失败（忽略）：{exc}")

    client = llm.DeepSeek(cfg.model, os.environ.get("DEEPSEEK_API_KEY"))
    if args.mock:
        analyses, digest_meta = mock_analyses(items)
    elif args.dry_run:
        analyses, digest_meta = {}, {"lede": "", "takeaway": "", "degraded": True}
    else:
        if not client.api_key:
            log("缺少 DEEPSEEK_API_KEY；使用 --dry-run 可先验证抓取，或 --mock 演练")
            return 3
        analyses, digest_meta = analyze_all(client, cfg, items)

    meta = {
        "generated_at": dt.datetime.now(llm.BEIJING).strftime("%Y-%m-%d %H:%M"),
        "sources_ok": sum(1 for s in status if s["got"]),
        "sources": status,
        "usage": client.usage.as_dict(),
        "model": cfg.model.model,
    }

    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "d").mkdir(exist_ok=True)
    (site_dir / "data").mkdir(exist_ok=True)
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")

    page = render.digest_page(cfg, today, items, analyses, meta, digest_meta)
    out = site_dir / "d" / f"{today.isoformat()}.html"
    if out.exists() and not args.force:
        log(f"{out.name} 已存在，未覆盖（要覆盖加 --force）")
    else:
        out.write_text(page, encoding="utf-8")
    (site_dir / "index.html").write_text(page, encoding="utf-8")

    snap = render.snapshot(today, items, analyses, meta, digest_meta)
    render.write_json(site_dir / "data" / f"{today.isoformat()}.json", snap)
    render.write_json(site_dir / "data" / "latest.json", snap)

    archive = _load_archive(site_dir)
    archive[today.isoformat()] = {
        "date": today.isoformat(),
        "lede": digest_meta.get("lede", ""),
        "takeaway": digest_meta.get("takeaway", ""),
        "count": len(items),
    }
    entries = [archive[k] for k in sorted(archive, reverse=True)]
    render.write_json(site_dir / "data" / "archive.json", {"entries": entries})
    (site_dir / "archive.html").write_text(render.archive_page(cfg, entries), encoding="utf-8")
    (site_dir / "feed.xml").write_text(render.rss_feed(cfg, entries), encoding="utf-8")

    u = client.usage
    log(f"完成：{out.name}｜{u.requests} 次调用｜"
        f"输入 {u.prompt_tokens} / 输出 {u.completion_tokens} token｜"
        f"花费 ${u.cost_usd:.4f}")
    if args.github_output:
        # 单行化，避免导语里的换行把 $GITHUB_OUTPUT 的 key=value 解析搞坏
        lede = re.sub(r"\s+", " ", digest_meta.get("lede", "")).strip()
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"lede={lede}\n")
            fh.write(f"count={len(items)}\n")
            fh.write(f"cost={u.cost_usd:.6f}\n")
    return 0


def _items_from_cache(cfg) -> tuple[list[feed.Item], list[dict]]:
    """只用 .cache 里的快照出报：适合离线重排版、或信源全挂时的兜底。"""
    items: list[feed.Item] = []
    status: list[dict] = []
    for src in cfg.sources:
        path = cfg.cache_dir / f"{src.id}.json"
        got: list[feed.Item] = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for c in data.get("items", []):
                    got.append(feed.Item(
                        source_id=src.id, category=src.category, title=c["title"],
                        link=c["link"], summary=c.get("summary", ""),
                        published=(dt.datetime.fromisoformat(c["published"])
                                   if c.get("published") else None),
                        weight=src.weight, points=c.get("points", 0),
                        extra=c.get("extra", {})))
            except Exception as exc:
                status.append({"source": src.id, "category": src.category, "got": 0,
                               "error": f"缓存损坏: {exc}"})
                continue
        items.extend(got)
        status.append({"source": src.id, "category": src.category, "got": len(got),
                       "error": None if got else "无缓存"})
    return items, status


def _load_archive(site_dir: Path) -> dict:
    path = site_dir / "data" / "archive.json"
    if path.exists():
        try:
            return {e["date"]: e for e in json.loads(path.read_text(encoding="utf-8")).get("entries", [])}
        except Exception:
            return {}
    return {}


def main(argv=None) -> int:
    # Windows 控制台默认 GBK，中文日志会乱码；这里强制 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(prog="brief", description="每日技术简报生成器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="生成一期日报")
    b.add_argument("--config", default=str(ROOT / "config.toml"))
    b.add_argument("--date", help="指定日期 YYYY-MM-DD（默认今天）")
    b.add_argument("--dry-run", action="store_true", help="不调用模型，只验证抓取与渲染")
    b.add_argument("--mock", action="store_true", help="用占位数据跑通全流程（无需 Key）")
    b.add_argument("--offline", action="store_true", help="信源失败时回退到 .cache 快照")
    b.add_argument("--no-fetch", action="store_true", help="完全不联网，只用 .cache 快照")
    b.add_argument("--no-enrich", action="store_true", help="不补抓原文摘要（更快、更少请求）")
    b.add_argument("--out", help="输出目录（默认 ./site）")
    b.add_argument("--force", action="store_true", help="覆盖当天已存在的文件")
    b.add_argument("--github-output", help="把 lede/count/cost 追加到文件（Actions 用）")
    b.set_defaults(func=build)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"✗ 失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())