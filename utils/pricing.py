"""模型單價與審查費用計算。

費用依「實際回應的模型」與「服務商回報的 token 數」計算：
    費用 = 未快取輸入 × 輸入單價 + 快取命中 × 快取讀取單價 + 快取寫入 × 快取寫入單價 + 輸出（含思考）× 輸出單價
單價為預設值（美元／百萬 token），介面上可逐一修改；匯率亦可設定。
Claude 單價取自 Anthropic 官方；Gemini 4 Argon、GPT 6.1-sol 取自第三方整理（2026-10），請以各服務商帳單為準。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

USD_TWD = 32.5  # 預設匯率（2026-10 下旬約 32.5～32.6）


@dataclass(frozen=True)
class Price:
    input: float  # 美元／百萬 token
    output: float
    cache_read: float
    cache_write: float  # Claude 5 分鐘快取寫入為輸入單價 1.25 倍；其他家不另計
    source: str = ""


# (模型名稱規則, 生效起日, 單價)；同一規則有多筆時取審查日期適用的最後一筆
PRICE_TABLE: list[tuple[str, date, Price]] = [
    (r"^claude-opus-5-5", date(2026, 1, 1), Price(4.00, 20.00, 0.20, 5.00, "Anthropic 官方")),
    (r"^claude-opus-5$", date(2026, 1, 1), Price(5.00, 25.00, 0.50, 6.25, "Anthropic 官方")),
    (r"^claude-sonnet-5-5", date(2026, 1, 1), Price(2.00, 10.00, 0.20, 2.50, "Anthropic 官方")),
    (r"^claude-fable-5-1", date(2026, 1, 1), Price(10.00, 50.00, 0.25, 12.50, "Anthropic 官方")),
    (r"^claude-haiku-5-5", date(2026, 1, 1), Price(0.10, 0.50, 0.01, 0.125, "Anthropic 官方")),
    (r"argon", date(2026, 1, 1), Price(2.00, 10.00, 0.50, 0.0, "第三方整理：優惠價至 2026-11-30")),
    (r"argon", date(2026, 12, 1), Price(4.00, 20.00, 1.00, 0.0, "第三方整理：2026-12-01 起標準價")),
    (r"gpt-6\.1-sol", date(2026, 1, 1), Price(2.00, 10.00, 0.20, 0.0, "第三方整理")),
    (r"^mock", date(2000, 1, 1), Price(0.0, 0.0, 0.0, 0.0, "離線示範不計費")),
]


def default_price(model: str, on: date | None = None) -> Price | None:
    """預設單價；未收錄的模型回傳 None（介面上請使用者自行填寫）。"""
    on = on or date.today()
    hit = None
    for pat, since, price in PRICE_TABLE:
        if re.search(pat, model) and since <= on:
            hit = price
    return hit


@dataclass
class CostRow:
    model: str
    calls: int = 0
    input: int = 0
    cached: int = 0
    cache_write: int = 0
    output: int = 0
    usd: float | None = 0.0  # None：沒有單價
    estimated: bool = False

    @property
    def total_input(self) -> int:
        return self.input + self.cached + self.cache_write


@dataclass
class CostSummary:
    rows: list[CostRow]
    fx: float
    prices: dict[str, Price] = field(default_factory=dict)

    @property
    def usd(self) -> float:
        return sum(r.usd or 0.0 for r in self.rows)

    @property
    def twd(self) -> float:
        return self.usd * self.fx

    @property
    def calls(self) -> int:
        return sum(r.calls for r in self.rows)

    @property
    def missing(self) -> list[str]:
        return [r.model for r in self.rows if r.usd is None]

    @property
    def estimated(self) -> bool:
        return any(r.estimated for r in self.rows)


def summarize(usage: list, prices: dict[str, Price] | None = None, fx: float = USD_TWD,
              on: date | None = None) -> CostSummary:
    """usage：Usage 或 dict（JSON 讀回）清單；prices：使用者指定的單價（模型 → Price），未指定者用預設值。"""
    prices = dict(prices or {})
    rows: dict[str, CostRow] = {}
    for u in usage:
        d = u if isinstance(u, dict) else u.__dict__
        r = rows.setdefault(d["model"], CostRow(d["model"]))
        r.calls += 1
        r.input += d.get("input", 0)
        r.cached += d.get("cached", 0)
        r.cache_write += d.get("cache_write", 0)
        r.output += d.get("output", 0)
        r.estimated = r.estimated or d.get("estimated", False)
    for r in rows.values():
        p = prices.get(r.model) or default_price(r.model, on)
        if p is None:
            r.usd = None
            continue
        prices[r.model] = p
        r.usd = (r.input * p.input + r.cached * p.cache_read + r.cache_write * p.cache_write + r.output * p.output) / 1e6
    return CostSummary(list(rows.values()), fx, prices)


def twd(usd: float, fx: float) -> str:
    return f"NT${usd * fx:,.0f}" if usd * fx >= 10 else f"NT${usd * fx:,.1f}"
