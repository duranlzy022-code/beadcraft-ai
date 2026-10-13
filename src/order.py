"""
订单计算器 —— 输入一张材料清单，自动比对库存、算长度、算报价。

你只写「名称 尺寸 颗数」，脚本自己去 materials.csv 里模糊匹配：
  · 匹配上 → 直接用库存里的真实单价
  · 匹配不上 → 标记「需采购」，用你给的估价，并提醒先收定金
"""

from dataclasses import dataclass, field

from .materials import Material, load_materials
from .pricing import TIERS, quote

EASE_CM = 1.0        # ★ 多圈手串每圈的松量（cm）


@dataclass
class Item:
    name: str
    size_mm: float
    qty: int
    unit_price: float | None = None
    note: str = ""
    matched: Material | None = field(default=None, repr=False)
    match_reason: str = ""

    @property
    def length_mm(self) -> float:
        # 匹配到材料就用它的穿线长（隔片按厚度算），否则退回规格尺寸
        per = self.matched.along_mm if self.matched else self.size_mm
        return per * self.qty

    @property
    def price(self) -> float | None:
        if self.unit_price is not None:
            return self.unit_price
        if self.matched and self.matched.unit_price is not None:
            return self.matched.unit_price
        return None

    @property
    def subtotal(self) -> float | None:
        p = self.price
        return None if p is None else p * self.qty

    @property
    def status(self) -> str:
        if self.matched:
            return "库存"
        if self.unit_price is not None:
            return "需采购"
        return "缺价格"


# ★ 同义词表：左边是你随口写的，右边是库存里的正式名
ALIASES = {
    "马粉": "粉晶", "莫粉": "粉晶", "芙蓉石": "粉晶",
    "老矿天河石": "天河石",
    "乌金黑曜石": "黑曜石",
    "海蓝宝老形珠": "海蓝宝",
}


def _normalize(name: str) -> str:
    for alias, real in ALIASES.items():
        if alias in name:
            return real
    for prefix in ("老矿", "巴西", "乌拉圭", "喜马拉雅", "曜晶级", "御晶级",
                   "收藏级", "甄选级", "7A", "5A", "3A", "10A"):
        name = name.replace(prefix, "")
    return name.strip()


def match_material(item: Item, pool: list, size_tol: float = 1.2):
    """在库存里找最匹配的：名字包含 + 尺寸最接近。★ size_tol 是尺寸容差。"""
    key = _normalize(item.name)
    if not key:
        return None, "名字为空"

    cands = [m for m in pool if key in m.name or _normalize(m.name) in key]
    if not cands:
        return None, f"库存里没有「{key}」"

    sized = [m for m in cands if abs(m.size_mm - item.size_mm) <= size_tol]
    if not sized:
        sizes = "/".join(f"{m.size_mm:g}" for m in cands)
        return None, f"有「{key}」但没有 {item.size_mm:g}mm（库存只有 {sizes}mm）"

    # ★ 优先级：①名字完全一致 ②尺寸最接近 ③价格
    #   不能一上来就挑最便宜的：「巴西海蓝宝」和「海蓝宝混色」是两种料，
    #   挑便宜的等于偷偷降级，会让报价系统性虚低。
    best = min(sized, key=lambda m: (
        0 if m.name == item.name else 1,
        abs(m.size_mm - item.size_mm),
        m.unit_price if m.unit_price is not None else 9e9,
    ))
    if best.unit_price is None:
        return None, f"库存有「{best.name}」但状态是{best.status}、没有价格"

    diff = "" if abs(best.size_mm - item.size_mm) < 0.05 else \
        f"（用 {best.size_mm:g}mm 顶 {item.size_mm:g}mm）"
    return best, f"匹配到 {best.name} {best.size_mm:g}mm ¥{best.unit_price:.2f}{diff}"


def length_report(items: list, wrist_cm: float, wraps_options=(1, 2, 3)):
    """多圈手串最容易翻车的地方：长度。"""
    total_mm = sum(i.length_mm for i in items)
    total_cm = total_mm / 10.0

    lines = [f"珠子总长 {total_cm:.1f} cm（{len(items)} 种，"
             f"{sum(i.qty for i in items)} 颗）"]
    for w in wraps_options:
        need = w * (wrist_cm + EASE_CM)
        gap = total_cm - need
        if abs(gap) <= 0.8:
            verdict = "✅ 正好"
        elif gap > 0:
            verdict = f"❌ 超了 {gap:.1f}cm"
        else:
            verdict = f"❌ 短了 {-gap:.1f}cm"
        lines.append(f"  {w} 圈需要 {need:.1f} cm → {verdict}")
    return total_cm, lines


def process(items: list, wrist_cm: float, budget=None):
    pool = load_materials()
    for it in items:
        it.matched, it.match_reason = match_material(it, pool)

    total_cm, len_lines = length_report(items, wrist_cm)

    priced = [i for i in items if i.subtotal is not None]
    missing = [i for i in items if i.subtotal is None]
    material_cost = sum(i.subtotal for i in priced)

    reuse_mix, exclusive_cost = {}, 0.0
    for i in priced:
        # ★「需采购」不等于「专用料」：都要收定金，但档位判定只看专用度
        tag = i.matched.reuse if i.matched else "较通用"
        reuse_mix[tag] = reuse_mix.get(tag, 0) + i.qty
        if not i.matched:
            exclusive_cost += i.subtotal

    shortages = []
    for i in items:
        if i.matched and i.matched.stock < i.qty:
            shortages.append(f"{i.name} {i.size_mm:g}mm 需 {i.qty} 颗，"
                             f"库存记录只有 {i.matched.stock} 颗")
        elif i.matched and i.matched.status == "待购":
            shortages.append(f"{i.name} {i.size_mm:g}mm 在表里是「待购」，还没到货")

    q = quote(material_cost, reuse_mix, exclusive_cost)
    return {"items": items, "missing": missing, "total_cm": total_cm,
            "length_lines": len_lines, "material_cost": material_cost,
            "quote": q, "budget": budget, "shortages": shortages}


def render(r: dict) -> str:
    out = ["=" * 68, "订单核算", "=" * 68, ""]
    out += r["length_lines"] + [""]
    out.append(f"{'材料':<16}{'尺寸':>7}{'颗数':>5}{'单价':>8}{'小计':>9}  状态 / 说明")
    out.append("-" * 68)
    for i in r["items"]:
        p = f"¥{i.price:.2f}" if i.price is not None else "—"
        s = f"¥{i.subtotal:.2f}" if i.subtotal is not None else "—"
        out.append(f"{i.name[:15]:<16}{i.size_mm:>6.1f}m{i.qty:>5}{p:>8}{s:>9}  "
                   f"[{i.status}] {i.match_reason or i.note}")
    out.append("-" * 68)

    q = r["quote"]
    out += ["", f"材料成本  ¥{r['material_cost']:.2f}",
            f"档位      {q.tier}（¥{TIERS[q.tier]['min']:.0f}–{TIERS[q.tier]['max']:.0f}）",
            f"建议售价  ¥{q.suggested_price:.0f}",
            f"议价底线  ¥{q.floor_price:.0f}"]
    if q.deposit_required > 0:
        out.append(f"应收定金  ¥{q.deposit_required:.0f}（需采购部分 ¥{q.exclusive_cost:.2f}）")
    if r["budget"]:
        lo, hi = r["budget"]
        fit = "✅ 在预算内" if lo <= q.suggested_price <= hi else "⚠ 超出预算区间"
        out.append(f"客户预算  ¥{lo:.0f}–{hi:.0f} → {fit}")
    if r.get("shortages"):
        out += ["", "🔴 库存不够 / 未到货："] + [f"    · {x}" for x in r["shortages"]]
    if r["missing"]:
        out += ["", "⚠ 下面这些还没有价格，没算进成本："]
        out += [f"    {i.name} {i.size_mm:g}mm ×{i.qty}  —— {i.match_reason}"
                for i in r["missing"]]
    return "\n".join(out)