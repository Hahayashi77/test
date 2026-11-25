"""每日技术简报 · 极简实现（Python 标准库，零第三方依赖）。

模块划分：
    config    读取 TOML 配置
    feed      抓取与解析 RSS / Atom / arXiv
    select    打分、去重、挑选
    llm       DeepSeek 调用 + 预算控制 + 用量统计
    render    生成 HTML / RSS / JSON
    cli       命令行入口
"""

__version__ = "1.0.0"