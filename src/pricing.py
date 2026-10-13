"""
报价模块：纯算术，零 LLM。规则按经营定价清单实现。

【为什么报价绝对不能交给模型】
模型算数学是"预测下一个字最可能是几"，不是真在算。
让它算 4.87×14 + 2.57×4 + ...，它有很大概率给一个看着挺像的错数字，
错得没规律，很难发现。报价错了是真金白银。

下面 ★ 标记的都是【经营判断】，不是客观事实，可按自己的业务调整。
改这些数字就是改定价策略，跑一遍就能看到全部方案的价格变化。
"""

import math
from dataclasses import dataclass

# 定价参数。下面是【示例值】，不是任何真实店铺的报价。
# 用你自己的参数：在 data/pricing.json 里写同名字段（该文件不进 git），会覆盖示例值。
import json as _json
from pathlib import Path as _Path

MARKUP = 3.0                      # 加价系数：报价 ≈ 材料成本 × MARKUP
TIERS = {                         # 档位价格区间
    "入门款": {"min": 50.0,  "max": 100.0,  "desc": "全用通用料，主色+辅色，基础珠"},
    "标准款": {"min": 100.0, "max": 300.0,  "desc": "引入较通用料/点缀色，配色更丰富"},
    "高配款": {"min": 300.0, "max": 800.0,  "desc": "可用专用料、贵价珠，复杂配色"},
}
FLOOR_MULTIPLIER = 1.2            # 议价底线：不低于材料成本 × 这个倍数
DEPOSIT_RATIO_ON_EXCLUSIVE = 1.0  # 用到专用料时，定金占专用料成本的比例
TIER_RULES = {                    # 判档门槛（示例值）
    "exclusive_ratio": 0.3,       # 专用料颗数占比 ≥ 这个值 → 高配款
    "high_cost": 300.0,           # 材料成本 ≥ 这个值 → 高配款
    "standard_cost": 50.0,        # 全通用料但成本 ≥ 这个值 → 标准款
}

_cfg = _Path(__file__).resolve().parent.parent / "data" / "pricing.json"
if _cfg.exists():
    _c = _json.loads(_cfg.read_text(encoding="utf-8"))
    MARKUP = _c.get("MARKUP", MARKUP)
    TIERS = _c.get("TIERS", TIERS)
    FLOOR_MULTIPLIER = _c.get("FLOOR_MULTIPLIER", FLOOR_MULTIPLIER)
    DEPOSIT_RATIO_ON_EXCLUSIVE = _c.get("DEPOSIT_RATIO_ON_EXCLUSIVE", DEPOSIT_RATIO_ON_EXCLUSIVE)
    TIER_RULES = {**TIER_RULES, **_c.get("TIER_RULES", {})}


@dataclass
class Quote:
    """一份报价的完整拆解，每一项都能追溯来源。"""

    material_cost: float
    tier: str
    raw_price: float          # 材料成本 × MARKUP，未修正
    suggested_price: float    # 落到档位区间后的建议售价
    floor_price: float        # 议价底线
    deposit_required: float   # 需要预收的定金（0 表示不用收）
    exclusive_cost: float     # 其中专用料占的成本
    notes: list[str]

    def __str__(self) -> str:
        s = (f"报价 ¥{self.suggested_price:.0f}（{self.tier}）"
             f"｜成本 ¥{self.material_cost:.2f}｜底线 ¥{self.floor_price:.0f}")
        if self.deposit_required > 0:
            s += f"｜需收定金 ¥{self.deposit_required:.0f}"
        return s

    def as_lines(self) -> list[str]:
        lines = [
            f"材料成本      ¥{self.material_cost:>8.2f}",
            f"加价系数      ×{MARKUP}",
            f"毛算售价      ¥{self.raw_price:>8.2f}",
            f"落档          {self.tier}（¥{TIERS[self.tier]['min']:.0f}–"
            f"{TIERS[self.tier]['max']:.0f}）",
            f"建议售价      ¥{self.suggested_price:>8.0f}",
            f"议价底线      ¥{self.floor_price:>8.0f}",
        ]
        if self.deposit_required > 0:
            lines.append(f"应收定金      ¥{self.deposit_required:>8.0f}"
                         f"（专用料成本 ¥{self.exclusive_cost:.2f}）")
        lines.extend(f"备注          {n}" for n in self.notes)
        return lines


def decide_tier(material_cost: float, reuse_mix: dict[str, int]) -> str:
    """判断这套方案属于哪个档位。

    ★ 判断依据（由定价清单翻译成的可执行规则）：
        · 全是通用料            → 入门款
        · 含较通用料，无专用料  → 标准款
        · 含专用料 或 成本很高  → 高配款

    reuse_mix 形如 {"通用": 18, "较通用": 4, "专用": 0}，值是颗数。
    """
    total_beads = sum(reuse_mix.values()) or 1
    if reuse_mix.get("专用", 0) / total_beads >= TIER_RULES["exclusive_ratio"]:
        return "高配款"
    # 成本高到即使没用专用料也该按高配卖
    if material_cost >= TIER_RULES["high_cost"]:
        return "高配款"
    if reuse_mix.get("较通用", 0) > 0 or reuse_mix.get("专用", 0) > 0:
        return "标准款"
    # ★ 全通用料但成本也不低时，仍然算标准款而不是入门款
    if material_cost >= TIER_RULES["standard_cost"]:
        return "标准款"
    return "入门款"


def _round_price(x: float) -> float:
    """取整到符合标价习惯的数字（¥328 而不是 ¥327.43）。

    ★ 这里用的是"低于300取整到10，高于300取整到50"，是零售常见做法。

    【坑】不能直接用 Python 的 round()：它是"银行家舍入"，round(4.5) 得 4 不是 5。
         算钱的地方必须自己写四舍五入，否则会出现底线价被抹到成本以下这种事故。
    """
    step = 10.0 if x < 300 else 50.0
    return math.floor(x / step + 0.5) * step


def quote(material_cost: float, reuse_mix: dict[str, int],
          exclusive_cost: float = 0.0) -> Quote:
    """核心报价函数：材料成本 + 材料构成 → 完整报价。"""
    tier = decide_tier(material_cost, reuse_mix)
    band = TIERS[tier]
    notes: list[str] = []

    raw = material_cost * MARKUP

    # 落档：毛算价格如果跑出档位区间，就拉回区间内
    suggested = raw
    if raw < band["min"]:
        suggested = band["min"]
        notes.append(f"毛算 ¥{raw:.0f} 低于{tier}下限，按下限报")
    elif raw > band["max"]:
        suggested = band["max"]
        notes.append(f"毛算 ¥{raw:.0f} 超出{tier}上限，按上限报（或考虑升档）")

    suggested = _round_price(suggested)
    # 底线不能低于材料成本本身：低于就是纯亏
    floor = _round_price(max(material_cost * FLOOR_MULTIPLIER, band["min"] * 0.75))
    floor = max(floor, math.ceil(material_cost / 10.0) * 10.0)

    deposit = 0.0
    if exclusive_cost > 0:
        deposit = _round_price(exclusive_cost * DEPOSIT_RATIO_ON_EXCLUSIVE)
        notes.append("含专用料，按规矩先收定金再采购，不垫资")

    return Quote(
        material_cost=round(material_cost, 2),
        tier=tier,
        raw_price=round(raw, 2),
        suggested_price=suggested,
        floor_price=floor,
        deposit_required=deposit,
        exclusive_cost=round(exclusive_cost, 2),
        notes=notes,
    )


def negotiate(q: Quote, customer_offer: float) -> tuple[bool, str]:
    """议价判断：返回确定的判断和固定话术，不交给模型即兴发挥。

    模型即兴发挥，可能会答应一个亏本价。
    """
    if customer_offer >= q.suggested_price:
        return True, "可以直接成交"
    if customer_offer >= q.floor_price:
        gap = q.suggested_price - customer_offer
        return True, f"可接受：让利 ¥{gap:.0f}，仍在底线之上"
    short = q.floor_price - customer_offer
    return False, f"低于底线 ¥{short:.0f}，建议还价到 ¥{q.floor_price:.0f}"


if __name__ == "__main__":
    cases = [
        ("全通用料小成本", 38.0, {"通用": 22, "较通用": 0, "专用": 0}, 0.0),
        ("含较通用料", 96.0, {"通用": 14, "较通用": 6, "专用": 0}, 0.0),
        ("含专用料", 268.0, {"通用": 8, "较通用": 4, "专用": 10}, 149.0),
    ]
    for label, cost, mix, exc in cases:
        q = quote(cost, mix, exc)
        print(f"── {label} " + "─" * (44 - len(label)))
        for line in q.as_lines():
            print("  " + line)
        for offer in (q.suggested_price, q.floor_price, q.floor_price - 40):
            ok, msg = negotiate(q, offer)
            print(f"  客户出 ¥{offer:>5.0f} → {'✓' if ok else '✗'} {msg}")
        print()
