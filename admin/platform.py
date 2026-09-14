"""平台抽象：WorkBuddy CN（国内版）与 WorkBuddy AI（国外版）。

背景
----
本项目原有实现把上游地址写死为 CN 的 ``https://copilot.tencent.com``，
但 WorkBuddy 实际有两套完全独立的后端：

===========  ==============================  ========================
平台           chat / growth 域                billing 域
===========  ==============================  ========================
``cn``        copilot.tencent.com             www.codebuddy.cn
``ai``        www.workbuddy.ai                www.workbuddy.ai
===========  ==============================  ========================

实测（2026-05）：两端 token **互不通用** —— AI 账号的 token 打 CN 后端返回
HTTP 401，反之亦然。因此必须按账号归属平台分流，不能共用同一 base URL。

平台判定
--------
auth 文件的 ``auth.domain`` 是最可靠的信号（``www.workbuddy.ai`` → AI，
``www.codebuddy.cn`` 等 → CN）。缺失时才回退到显式字段 / 默认值。
"""
from __future__ import annotations

CN = "cn"
AI = "ai"

PLATFORMS = (CN, AI)

#: 各平台的默认 domain（domain 缺失时用于拼 ``X-Domain`` 头）
DEFAULT_DOMAINS = {
    CN: "www.codebuddy.cn",
    AI: "www.workbuddy.ai",
}

#: 各平台的上游 base URL。
#: CN 的 chat 与 billing 分属两个域；AI 侧共用一个域。
CHAT_BASES = {
    CN: "https://copilot.tencent.com",
    AI: "https://www.workbuddy.ai",
}
BILLING_BASES = {
    CN: "https://www.codebuddy.cn",
    AI: "https://www.workbuddy.ai",
}

#: 人类可读名称（前端徽标 / 日志）
LABELS = {
    CN: "CN",
    AI: "AI",
}


def normalize(platform: str | None) -> str:
    """把任意输入归一化为合法平台名；未知值一律回退 CN（保持既有行为）。"""
    if not platform:
        return CN
    p = str(platform).strip().lower()
    return p if p in PLATFORMS else CN


def from_domain(domain: str | None) -> str:
    """由 auth 文件的 domain 推断平台。"""
    d = (domain or "").strip().lower()
    if not d:
        return CN
    if "workbuddy.ai" in d:
        return AI
    return CN


def detect(domain: str | None = None, platform: str | None = None) -> str:
    """综合判定平台：显式 platform 优先，其次 domain 推断。

    显式值优先是为了让用户能在后台手动纠正（例如同一 domain 下的特殊账号）。
    """
    if platform and str(platform).strip().lower() in PLATFORMS:
        return normalize(platform)
    return from_domain(domain)


def chat_base(domain: str | None = None, platform: str | None = None) -> str:
    """该账号的平台 chat / growth 域 base URL。"""
    return CHAT_BASES[detect(domain, platform)]


def billing_base(domain: str | None = None, platform: str | None = None) -> str:
    """该账号的平台 billing 域 base URL。"""
    return BILLING_BASES[detect(domain, platform)]


def default_domain(domain: str | None = None, platform: str | None = None) -> str:
    """该账号的 ``X-Domain`` 头取值：优先账号自带 domain，否则用平台默认。"""
    d = (domain or "").strip()
    if d:
        return d
    return DEFAULT_DOMAINS[detect(domain, platform)]


def label(platform: str | None) -> str:
    """平台展示名。"""
    return LABELS[normalize(platform)]


def supports_cat_travel(platform: str | None) -> bool:
    """该平台是否默认参与猫猫旅行自动巡检。

    仅 CN 默认开启：AI 侧虽同样有 growth 域（``buddy/info`` 实测返回 200 且
    ``buddy: null``，即可领养），但 AI 是不限量免费用量制，"300 积分" 的实际
    价值未经验证，故保留为手动触发，避免对上游做无意义的自动化请求。
    """
    return normalize(platform) == CN
