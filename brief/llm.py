"""DeepSeek 调用：分批摘要、JSON 输出、重试、峰谷计价、预算熔断。

设计要点
--------
* **只要 OpenAI 兼容接口**，所以换成任何兼容端点（本地 vLLM / 其他厂）只需改 config。
* **预算熔断按"预估-实扣"两步**：调用前按 token 估算，超过 budget.max_usd_per_run 就不再调用，
  报告自动降级为"只列标题"，保证一次 runaway 不会烧掉你的余额。
* **峰谷计价**：北京时间工作日 09–12、14–18 为峰时，价格翻倍，统计里会分开记。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import Model
from .feed import Item

# 每 1M token 的美元单价（deepseek-flash，2026-11 官方价）
PRICES = {
    "deepseek-flash": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
    "deepseek-v4-pro": {"cache_hit": 0.022, "cache_miss": 0.66, "output": 1.98},
}

BEIJING = dt.timezone(dt.timedelta(hours=8))


def is_peak(now: dt.datetime | None = None) -> bool:
    """北京时间工作日 09:00–12:00、14:00–18:00 为峰时（节假日无法判定，按谷时算）。"""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(BEIJING)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 <= minutes < 12 * 60) or (14 * 60 <= minutes < 18 * 60)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    requests: int = 0
    failures: int = 0
    cost_usd: float = 0.0
    peak: bool = False

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "peak": self.peak,
        }


@dataclass
class Digest:
    """一份日报的"分析层"：导语 + 板块 + 每条的一句话解读。"""
    lede: str = ""
    sections: list[dict] = field(default_factory=list)
    takeaway: str = ""
    degraded: bool = False


SYSTEM = (
    "你是一位资深技术编辑，为中文读者做每日技术简报。"
    "要求：只用给定的条目，不要编造事实、不要补充外部信息；"
    "每句话都要有信息量，禁止‘这体现了AI的飞速发展’这类空话；"
    "术语保留英文原文；不确定的数字和结论就不要写。"
)

SCHEMA_HINT = """严格输出 JSON（不要 Markdown 代码块，不要任何解释文字），格式：
{
  "lede": "2-3 句话，概括今天最重要的趋势或共同主题，不超过 90 字",
  "takeaway": "1 句话，今天最值得深挖或最该注意的一条判断，不超过 60 字",
  "items": [
    {"id": 1, "what": "这条讲了什么（客观转述，40-70 字）",
     "why": "对开发者意味着什么 / 为什么值得看（30-60 字）",
     "tag": "4-8 字标签，如 开源/论文/工具/安全/商业"}
  ]
}
必须为输入的每一条都输出一个对象，id 与输入一致。"""


def _estimate_tokens(text: str) -> int:
    """粗估：中文 ~1.5 字/token，英文 ~4 字符/token，混排取中间。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4) + 8


def estimate_cost(model: Model, prompt: str, expected_out: int) -> float:
    p = PRICES.get(model.model, PRICES["deepseek-flash"])
    factor = 2.0 if is_peak() else 1.0
    in_cost = (_estimate_tokens(prompt) / 1_000_000) * p["cache_miss"] * factor
    out_cost = (expected_out / 1_000_000) * p["output"] * factor
    return in_cost + out_cost


class DeepSeek:
    def __init__(self, model: Model, api_key: str | None, timeout: int = 120):
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.usage = Usage(peak=is_peak())

    # ── 底层请求 ────────────────────────────────────────────────
    def chat(self, messages: list[dict], max_tokens: int, json_mode: bool = True,
             retries: int = 3) -> str:
        if not self.api_key:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY")
        body = {
            "model": self.model.model,
            "messages": messages,
            "temperature": self.model.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = self.model.base_url.rstrip("/") + "/chat/completions"

        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, data=data, method="POST", headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                })
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.usage.requests += 1
                self._account(payload.get("usage") or {})
                return payload["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as exc:
                last = exc
                # 429 / 5xx 退避重试；4xx（key 错、参数错）直接放弃
                if exc.code in (408, 409, 425, 429, 500, 502, 503, 504) and attempt < retries:
                    exc.close()
                    time.sleep(min(20, 2 ** attempt * 2))
                    continue
                detail = exc.read().decode("utf-8", "replace")[:300]
                exc.close()
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                if attempt < retries:
                    time.sleep(min(20, 2 ** attempt * 2))
                    continue
                raise RuntimeError(f"请求失败: {exc}") from exc
        raise RuntimeError(f"请求失败: {last}")

    def _account(self, usage: dict) -> None:
        price = PRICES.get(self.model.model, PRICES["deepseek-flash"])
        factor = 2.0 if self.usage.peak else 1.0
        prompt = int(usage.get("prompt_tokens") or 0)
        hit = int(usage.get("prompt_cache_hit_tokens")
                  or usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens") or 0)
        miss = max(0, prompt - hit)
        self.usage.prompt_tokens += prompt
        self.usage.completion_tokens += completion
        self.usage.cache_hit_tokens += hit
        self.usage.cost_usd += (
            hit / 1_000_000 * price["cache_hit"]
            + miss / 1_000_000 * price["cache_miss"]
            + completion / 1_000_000 * price["output"]
        ) * factor

    # ── 业务层：分析一批条目 ────────────────────────────────────
    def analyze(self, items: list[Item], budget_left: float) -> dict[str, dict]:
        """返回 {item.uid: {"what","why","tag"}}；失败或超预算返回空 dict。"""
        prompt = self._build_prompt(items)
        est = estimate_cost(self.model, prompt, self.model.max_output_tokens)
        if est > budget_left:
            return {}

        raw = self.chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": prompt}],
            max_tokens=self.model.max_output_tokens,
        )
        data = _loads_loose(raw)
        out: dict[str, dict] = {}
        for obj in (data.get("items") or []):
            try:
                idx = int(obj.get("id"))
                if 1 <= idx <= len(items):
                    out[items[idx - 1].uid] = {
                        "what": str(obj.get("what", "")).strip()[:220],
                        "why": str(obj.get("why", "")).strip()[:180],
                        "tag": str(obj.get("tag", "")).strip()[:12],
                    }
            except Exception:
                continue
        out["__meta__"] = {"lede": str(data.get("lede", "")).strip()[:160],
                           "takeaway": str(data.get("takeaway", "")).strip()[:120]}
        return out

    def _build_prompt(self, items: list[Item]) -> str:
        lines = []
        for i, it in enumerate(items, 1):
            head = f"[{i}] 标题: {it.title}\n来源: {it.category}"
            if it.points:
                head += f"（HN {it.points} 分）"
            if it.extra.get("authors"):
                head += f"\n作者: {', '.join(it.extra['authors'])}"
            body = it.summary or "（无摘要）"
            lines.append(f"{head}\n摘要: {body}")
        return ("下面是今天抓到的条目。请逐条写中文解读，并给出导语和今日判断。\n\n"
                + "\n\n".join(lines)
                + "\n\n" + SCHEMA_HINT)


def _loads_loose(raw: str) -> dict:
    """模型偶尔会包 ```json 代码块或前后加话，这里做一次容错。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {}