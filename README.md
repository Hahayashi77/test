# 每日技术简报 · daily-brief

用 DeepSeek 的便宜额度，每天自动生成一份**中文技术简报**：抓 7 个信源 → 打分挑选 24 条 →
模型写解读 → 生成静态站点 → 部署到 GitHub Pages。**零第三方依赖**，一个 `python -m brief build` 搞定，
每月成本大约 **$1～3**。

在线示例结构：

```
首页 index.html ── 今日概述 / 5 个板块 / 每条"是什么 + 为什么值得看" / 今日判断
归档 archive.html ── 往期列表
订阅 feed.xml ── RSS
机器可读 data/latest.json ── 给别的 agent 或自建前端用
```

---

## 1. 三分钟跑起来

```bash
# 1) 演练：不花钱、不需要 Key，验证抓取和渲染链路
python -m brief build --mock --force

# 2) 用真实 Key 生成
export DEEPSEEK_API_KEY=sk-...
python -m brief build --offline --force     # --offline = 信源挂了就用 .cache 快照

# 3) 看效果
python -m http.server 8000 -d site
# 打开 http://localhost:8000
```

> Windows PowerShell 里设置环境变量：`$env:DEEPSEEK_API_KEY="sk-..."`

## 2. 部署到 GitHub Pages

1. 新建仓库，把这个目录（`daily-brief/` 里的全部内容）推到 `main`。
2. **Settings → Secrets and variables → Actions → New repository secret**
   名字 `DEEPSEEK_API_KEY`，值填你的 key。
3. **Settings → Pages → Source** 选 **GitHub Actions**。
4. 手动跑一次 workflow（Actions → daily-brief → Run workflow）确认能出页。
5. 之后每天北京时间 **07:13** 自动跑；生成结果会提交回仓库，Pages 自动更新。

首次部署后把 `config.toml` 里的 `[site] base_url` 改成
`https://<用户名>.github.io/<仓库名>`，RSS 和分享链接才会是绝对地址。

## 3. 成本

以默认配置（24 条、7 个源、`deepseek-flash`）为例：

| 项目 | 数值 |
|---|---|
| 单次输入 token | 约 13k（7 个源摘要截断到 420 字后） |
| 单次输出 token | 约 3k（24 条 × 约 120 字） |
| 单期成本（谷时） | **≈ $0.004**（约 3 分钱） |
| 单期成本（峰时） | ≈ $0.008 |
| 每月（30 期） | **≈ $0.12 ~ $0.25** |

真正的成本大头其实是**你改 prompt 反复调试那几次**，所以：

- `--dry-run`：只抓取不调模型，0 成本，改排序/信源时用它。
- `[budget] max_usd_per_run`：硬上限。超过就停止调用模型，自动降级成"只列标题"，不会一夜烧光余额。
- 峰时（北京时间工作日 09–12、14–18）价格翻倍；cron 定在早上 7 点就是为了吃谷时价。

## 4. 改配置（不用改代码）

全部在 `config.toml`：

| 想改什么 | 改哪里 |
|---|---|
| 每期条数 / 单源上限 | `[select] total_items` / `max_per_source` |
| 加信源 | 复制一个 `[[sources]]`，填 `kind`（rss/atom/arxiv）、`url`、`category`、`order` |
| 想让某类内容多出现 | `[select.keywords]` 加词、调大权重 |
| 想屏蔽某类内容 | `[select.negative_keywords]`（命中即大幅减分）；会议/招聘类会被 `PROMO_RE` 直接丢弃 |
| 摘要更详细（更贵） | `[model] excerpt_chars` 调大，同时看 `batch_size` |
| 换模型 / 换端点 | `[model] model` / `base_url`（任何 OpenAI 兼容端点都行，价格表在 `brief/llm.py` 顶部） |
| 板块顺序 | `[[sources]] order`，小的排前面 |

## 5. 代码地图

```
brief/
├── config.py    读 TOML → dataclass（多余字段自动忽略，配置永不报错）
├── feed.py   ★ 抓取 + 解析 RSS/Atom/arXiv；坏条目跳过，单源失败不影响整体
├── select.py ★ 粗筛（推广/短标题）→ 去重（链接+标题相似度）→ 打分 → 限流挑选
├── llm.py    ★ DeepSeek 调用：分批、JSON 模式、退避重试、峰谷计价、预算熔断
├── render.py    HTML（含深色模式）/ 归档 / RSS / JSON 快照
└── cli.py       命令行编排 + 原始快照落盘（.cache/）+ 日志
```

几个设计上值得说的点：

1. **失败不空转**：任一信源超时只记一条 `error` 到页面页脚和 JSON 里，当天照常出报。
   `--offline` 会回退到上次成功的 `.cache/*.json`。
2. **摘要补全**：HN 这类源只给标题（正文里全是 "Article URL / Comments URL" 模板噪音，已清洗），
   所以挑完之后会去原文页抓 `og:description` 补一段真摘要（`enrich_max` 控制数量）。
   实测 7 条缺摘要的条目里能补上 6 条，解读质量差别很大。
3. **评分是纯乘法**：`信源权重 × 新鲜度 × 热度 × 关键词加成 × 负向惩罚`。
   任何一项归零就沉底，比加权求和更好调、不容易出现"某条巨长摘要霸榜"。
4. **去重两级**：先链接归一化（去掉 `utm_*` 等）取哈希，再用标题相似度（0.86）折叠转载。
5. **JSON 容错**：模型偶尔包 ```json 代码块或加解释，`_loads_loose` 会兜住；
   一条解析失败不影响其它条目。
6. **预算熔断**：调用前按 token 粗估成本，超 `max_usd_per_run` 就不发请求，直接降级；
   实测中报告会正确标记为降级，而不是"看起来成功了但内容空"。

## 6. 测试

43 个测试，全部离线（用本地假的 OpenAI 兼容服务和打桩的 http 响应）：

```bash
python -m unittest discover -s tests -t . -v
```

覆盖：JSON 围栏/夹杂话术的容错、401 不重试而 5xx 重试、用量与峰谷计价、预算熔断、
promo/短标题过滤、标题相似去重、单源限流与板块排序、摘要补全的各种页面形态。

## 7. 常用命令

```bash
python -m brief build --dry-run          # 只抓取+挑选，0 成本（改排序时用）
python -m brief build --mock             # 假数据跑通全流程（不需要 Key）
python -m brief build --offline --force  # 正式生成，信源挂了走缓存
python -m brief build --date 2026-11-24  # 补生成某一天
python -m brief build --no-enrich        # 不补抓原文摘要（更快）
python scripts/preview.py                # 生成演示排版的数据（不调模型，仅看样式）
```

## 8. 可以接着做的

- 加信源：`https://github.com/trending?since=daily`（需自己写个抓取器）、
  arXiv 其它分类（`cs.RO`、`stat.ML`）、你所在行业的公众号 RSS。
- 加"本周回顾"：周末那期改成聚合过去 7 天的 top 条目。
- 加推送：用 Actions 把 `data/latest.json` 的 `lede` 发到 Telegram / 飞书 / 企业微信。
- 换前端：`site/data/*.json` 已经把所有结构化字段都保留了，套 Astro / Next 都行。