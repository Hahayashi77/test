"""配置读取：TOML → dataclass。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Site:
    title: str = "每日技术简报"
    tagline: str = ""
    base_url: str = ""
    language: str = "zh-CN"


@dataclass
class Model:
    provider: str = "deepseek"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.3
    batch_size: int = 8
    max_output_tokens: int = 3000
    excerpt_chars: int = 420
    # OpenAI 兼容端点通用旋钮
    json_mode: str = "auto"       # auto | on | off
    # 中转站计价：官方价 × 倍率 × 汇率。三者都为默认值时等价于"按官方美元价"。
    price_multiplier: float = 1.0
    usd_per_unit: float = 1.0     # 1 个"站内美元"折多少真美元
    api_key_env: str = "DEEPSEEK_API_KEY"


@dataclass
class Budget:
    max_usd_per_run: float = 0.12
    fallback_on_budget: bool = True


@dataclass
class Select:
    total_items: int = 24
    max_per_source: int = 6
    recency_hours: int = 18
    min_title_chars: int = 6
    drop_promo: bool = True
    enrich_max: int = 10
    keywords: dict[str, float] = field(default_factory=dict)
    negative_keywords: dict[str, float] = field(default_factory=dict)


@dataclass
class Source:
    id: str
    kind: str
    url: str
    category: str = "其他"
    weight: float = 1.0
    limit: int = 20
    min_points: int = 0
    cats: str = ""
    order: int = 50
    enabled: bool = True


@dataclass
class Config:
    site: Site
    model: Model
    budget: Budget
    select: Select
    sources: list[Source]
    root: Path

    @property
    def cache_dir(self) -> Path:
        return self.root / ".cache"


def _pick(cls: type, data: dict[str, Any]) -> Any:
    """只取 dataclass 声明过的字段，忽略配置里的多余键。"""
    names = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in names})


def load(path: Path) -> Config:
    path = Path(path).resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))

    site = _pick(Site, raw.get("site", {}))
    model = _pick(Model, raw.get("model", {}))
    budget = _pick(Budget, raw.get("budget", {}))

    sel_raw = dict(raw.get("select", {}))
    select = Select(
        total_items=int(sel_raw.get("total_items", 24)),
        max_per_source=int(sel_raw.get("max_per_source", 6)),
        recency_hours=int(sel_raw.get("recency_hours", 18)),
        min_title_chars=int(sel_raw.get("min_title_chars", 6)),
        drop_promo=bool(sel_raw.get("drop_promo", True)),
        enrich_max=int(sel_raw.get("enrich_max", 10)),
        keywords={str(k).lower(): float(v) for k, v in sel_raw.get("keywords", {}).items()},
        negative_keywords={
            str(k).lower(): float(v) for k, v in sel_raw.get("negative_keywords", {}).items()
        },
    )

    sources: list[Source] = []
    for s in raw.get("sources", []):
        src = _pick(Source, s)
        if src.id and src.url:
            sources.append(src)

    return Config(site=site, model=model, budget=budget, select=select,
                  sources=sources, root=path.parent)