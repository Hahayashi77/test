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

# 每 1M token 的美元单价（DeepSeek 官方价，2026-11）
# 中转站常用不同模型名，这里把常见别名都列上，避免"认不出"而估算失真。
PRICES = {
    "deepseek-flash": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
    "deepseek-v4.1-flash": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
    "deepseek-v4-flash": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
    "deepseek-v4-pro": {"cache_hit": 0.022, "cache_miss": 0.66, "output": 1.98},
    "deepseek-chat": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
    "deepseek-reasoner": {"cache_hit": 0.022, "cache_miss": 0.66, "output": 1.98},
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


def _price_table(model: Model) -> dict:
    """取该模型的官方单价；中转站按 model.pricing 的倍率与汇率折算。"""
    known = PRICES.get(model.model)
    if known is None:
        # 中转站模型名千奇百怪；按名字里有没有 "pro"/"reasoner" 猜档位，
        # 都不像就按 Flash 价。宁可高估，别让预算熔断失效。
        low = (model.model or "").lower()
        if any(k in low for k in ("pro", "reasoner", "r1", "max")):
            known = PRICES["deepseek-v4-pro"]
        else:
            known = PRICES["deepseek-flash"]
    mult = max(0.0, model.price_multiplier) * max(0.0, model.usd_per_unit)
    return {k: v * mult for k, v in known.items()}


def estimate_cost(model: Model, prompt: str, expected_out: int) -> float:
    p = _price_table(model)
    # 中转站通常没有峰谷价差；只有官方域名才按峰谷算
    factor = 2.0 if (is_peak() and _is_official(model)) else 1.0
    in_cost = (_estimate_tokens(prompt) / 1_000_000) * p["cache_miss"] * factor
    out_cost = (expected_out / 1_000_000) * p["output"] * factor
    return in_cost + out_cost


def _is_official(model: Model) -> bool:
    return "api.deepseek.com" in (model.base_url or "")


class DeepSeek:
    def __init__(self, model: Model, api_key: str | None, timeout: int = 120):
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.usage = Usage(peak=is_peak() and _is_official(model))
        # json_mode 的运行时开关：on=一直要，off=一直不要，auto=先要，遇 400 自动关掉
        self._json_mode = {"on": True, "off": False}.get(model.json_mode, True)
        self._probed: dict | None = None

    # ── 探测：中转站的模型名和 /models 列表必须核对 ──────────────
    def probe(self) -> dict:
        """检查端点与模型是否可用，返回诊断信息（不抛异常）。

        - available_models: /models 返回的列表（端点不支持时为 None）
        - model_ok: 目标模型是否在列表里（列表拿不到时为 None=未知）
        """
        if self._probed is not None:
            return self._probed
        info: dict = {"base_url": self.model.base_url, "model": self.model.model,
                      "reachable": False, "auth_ok": False,
                      "available_models": None, "model_ok": None, "error": None}
        url = self.model.base_url.rstrip("/") + "/models"
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "daily-brief/1.0",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            info["reachable"] = True
            info["auth_ok"] = True
            ids = [str(m.get("id")) for m in (payload.get("data") or [])
                   if isinstance(m, dict) and m.get("id")]
            if ids:
                info["available_models"] = ids
                info["model_ok"] = self.model.model in ids
        except urllib.error.HTTPError as exc:
            info["reachable"] = True
            detail = exc.read().decode("utf-8", "replace")[:200]
            exc.close()
            info["error"] = f"HTTP {exc.code}: {detail}"
            info["auth_ok"] = exc.code != 401
        except Exception as exc:
            info["error"] = str(exc)[:200]
        self._probed = info
        return info

    def suggest_model(self) -> str | None:
        """从 /models 列表里挑一个最像 DeepSeek 的（给用户改配置用）。"""
        ids = (self.probe().get("available_models") or [])
        for kw in ("deepseek", "v3", "v4"):
            for mid in ids:
                if kw in mid.lower():
                    return mid
        return ids[0] if ids else None

    # ── 底层请求 ────────────────────────────────────────────────
    def chat(self, messages: list[dict], max_tokens: int, json_mode: bool | None = True,
             retries: int = 3) -> str:
        if not self.api_key:
            raise RuntimeError(f"缺少 {self.model.api_key_env}")
        # json_mode: True=要，False=不要，None=交给自适应开关
        want_json = bool(self._json_mode) if json_mode is None else bool(json_mode)

        while True:
            try:
                return self._post(messages, max_tokens, want_json, retries)
            except _JsonFormatRejected as exc:
                # 端点不认 response_format：关掉开关，立刻用纯文本重发（只降级一次）
                log_json_fallback(str(exc))
                self._json_mode = False
                want_json = False

    def _post(self, messages: list[dict], max_tokens: int, want_json: bool,
              retries: int) -> str:
        body = {
            "model": self.model.model,
            "messages": messages,
            "temperature": self.model.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if want_json:
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = self.model.base_url.rstrip("/") + "/chat/completions"

        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, data=data, method="POST", headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": "daily-brief/1.0",
                })
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.usage.requests += 1
                self._account(payload.get("usage") or {})
                return payload["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as exc:
                last = exc
                detail = exc.read().decode("utf-8", "replace")[:400]
                exc.close()
                # 400 + 明确指向 response_format → 交给上层降级重发
                if (exc.code == 400 and want_json
                        and _looks_like_format_error(detail)):
                    raise _JsonFormatRejected(detail) from exc
                if exc.code in (408, 409, 425, 429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(min(20, 2 ** attempt * 2))
                    continue
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                if attempt < retries:
                    time.sleep(min(20, 2 ** attempt * 2))
                    continue
                raise RuntimeError(f"请求失败: {exc}") from exc
        raise RuntimeError(f"请求失败: {last}")

    def _account(self, usage: dict) -> None:
        price = _price_table(self.model)
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
            json_mode=None,          # 交给自适应开关决定
        )
        data = _loads_loose(raw)
        if not data:
            # 纯文本端点（无 json 模式）偶尔会拒答或输出废格式：补一次极简重试
            raw = self.chat(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": prompt
                  + "\n\n再次强调：只输出一个 JSON 对象，第一个字符必须是 {，不要任何前言后语。"}],
                max_tokens=self.model.max_output_tokens,
                json_mode=False,
            )
            data = _loads_loose(raw)
        out: dict[str, dict] = {}
        for obj in (data.get("items") or []):
            if not isinstance(obj, dict):
                continue
            try:
                idx = int(str(obj.get("id", "")).strip(" []"))
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


class _JsonFormatRejected(RuntimeError):
    """端点以 400 拒绝 response_format —— 内部信号，触发一次降级重发。"""


def log_json_fallback(detail: str) -> None:
    print(f"  ⚠ 端点不接受 response_format，已自动降级为纯文本 JSON 输出"
          f"（{detail[:100]}）", flush=True)


def _looks_like_format_error(detail: str) -> bool:
    """判断 400 是不是"不认识 response_format"，避免把别的参数错误也当成它。"""
    low = (detail or "").lower()
    hints = ("response_format", "json_object", "json mode", "response format")
    return any(h in low for h in hints)


def _loads_loose(raw: str) -> dict:
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