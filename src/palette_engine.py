"""
配色引擎 —— 整个项目的内核。**这里没有一行 LLM 调用。**

【这个文件是"色卡不准"的解药】
输入是结构化参数（角色关键词、意向色相、预算档位），
输出是从你真实库存里选出的具体珠子 + 具体颗数 + 具体价格。
每一步都是查表和算术，所以：
  · 同样输入永远同样输出（可复现，能回归测试）
  · 永远不会推荐你没有的珠子（幻觉率 = 0）
  · 输出格式 100% 合规，因为格式是代码拼的，不是模型"记得要遵守"

LLM 只在这个文件**外面**干两件事：角色描述→参数（前），方案→文案（后）。

┌─────────────────────────────────────────────────────────────┐
│ ★ 标记的都是【判断】不是【事实】—— 这些是你的旋钮。          │
│   改完直接跑，三套方案会当场变。这就是你的优化循环。         │
│   面试追问清单见 面试追问.md，问的全是这些 ★。               │
└─────────────────────────────────────────────────────────────┘
"""

from dataclasses import dataclass, field

from .color import contrast_ratio, delta_e, hue_distance, swatch_block
from .materials import Material, available_materials
from .pricing import Quote, quote

# ★ 三种配色策略：每种就是"色相差该落在哪个区间"的规则。
#   这几个数字是把配色审美量化了 —— 改它们就是在调你的设计风格。
STRATEGIES = {

    "对比色": {"hue_window": (140.0, 180.0), "min_contrast": 1.15,
               "desc": "色相相对，冲突感强抓眼球，适合出挑"},
}

# ★ ΔE 可接受区间：小于下限两颗珠子会糊成一片，大于上限会花。
DELTA_E_MIN = 10.0
DELTA_E_MAX = 95.0

# 色系中文名 → 色相角度。方向是【代码里的固定映射】，不是让模型猜角度。
FAMILY_HUE: dict[str, float | None] = {
    "红": 0.0, "橙": 30.0, "黄": 50.0, "绿": 120.0, "青": 180.0,
    "蓝": 220.0, "紫": 280.0, "粉": 340.0, "茶": 25.0,
    "黑": None, "白": None, "灰": None,   # 中性色没有有意义的色相
}

# ★ 打分权重：加起来 = 1.0。这四个数字是你最该想清楚的东西。
WEIGHTS = {
    "hue": 0.35,      # 色相贴不贴合角色
    "reuse": 0.30,    # 复用度（专用料=死钱，所以给了很高权重）
    "keyword": 0.20,  # 备注/描述里有没有命中角色关键词
    "stock": 0.15,    # 库存健康度（避免推完发现只剩1颗）
}

# 名字里带这些词的当隔珠用
SPACER_KEYWORDS = ("隔珠", "隔片")


@dataclass
class CharacterBrief:
    """一个角色的配色需求 —— LLM 唯一被允许产出的东西。"""

    name: str = ""
    keywords: list[str] = field(default_factory=list)
    hue_center: float | None = None
    hue_tolerance: float = 60.0
    wrist_cm: float = 15.0
    bead_size_mm: float = 8.0
    allow_exclusive: bool = False
    budget: float | None = None      # ★ 材料成本预算（元），None = 不限
    avoid: list[str] = field(default_factory=list)
    prefer: list[str] = field(default_factory=list)
    raw_text: str = ""


@dataclass
class Component:
    material: Material
    qty: int
    role: str   # 主石 / 配石 / 隔珠 / 顶珠

    @property
    def subtotal(self) -> float:
        return (self.material.unit_price or 0.0) * self.qty


@dataclass
class Scheme:
    strategy: str
    components: list[Component]
    material_cost: float
    quote: Quote
    score: float
    rationale: str

    @property
    def bead_count(self) -> int:
        return sum(c.qty for c in self.components)

    @property
    def uses_exclusive(self) -> bool:
        return any(c.material.reuse == "专用" for c in self.components)

    @property
    def main(self) -> Material:
        return next(c.material for c in self.components if c.role == "主石")

    @property
    def accent(self) -> Material:
        return next(c.material for c in self.components if c.role == "配石")

    def render(self) -> str:
        """渲染成固定格式。模板是代码拼的，所以模板合规率恒等于 100%。"""
        lines = [f"【{self.strategy}】{STRATEGIES[self.strategy]['desc']}", ""]
        lines.append("  " + "".join(swatch_block(c.material.hex, 6)
                                    for c in self.components))
        lines.append("")
        for c in self.components:
            lines.append(
                f"  {c.role:<4} {c.material.name:<14}{c.material.hex} "
                f"{c.material.size_mm:>4.1f}mm ×{c.qty:>2}  "
                f"¥{c.subtotal:>7.2f}  [{c.material.reuse}]"
            )
        lines += [
            "",
            f"  共 {self.bead_count} 颗｜色相差 "
            f"{hue_distance(self.main.hex, self.accent.hex):.0f}°｜"
            f"ΔE {delta_e(self.main.hex, self.accent.hex):.0f}｜"
            f"对比度 {contrast_ratio(self.main.hex, self.accent.hex):.2f}",
            f"  {self.quote}",
        ]
        if self.uses_exclusive:
            lines.append("  ⚠ 含专用料 —— 按规矩先收定金再采购，不垫资")
        lines += ["", f"  搭配理由：{self.rationale}"]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 结构：手围 → 各类珠子各几颗
# ---------------------------------------------------------------------------


def plan_structure(wrist_cm: float, bead_size_mm: float) -> dict[str, int]:
    """典型的"预算分配"问题：总长度固定，先给顶珠和配石留位置，剩下全给主石。

    ★ 松量 1.0cm、配石占比 18%、每配石配 2 隔珠 —— 这三个都是工艺判断，可调。
    """
    total_mm = (wrist_cm + 1.0) * 10.0      # ★ 松量 1cm，戴着才舒服
    focal_mm, accent_mm, spacer_mm = 10.0, 6.0, 4.0

    n_focal = 1
    remaining = total_mm - n_focal * focal_mm

    n_accent = max(2, min(6, int(remaining * 0.18 / accent_mm)))   # ★ 18%
    n_spacer = n_accent * 2                                        # ★ 每颗配石两侧各一

    remaining -= n_accent * accent_mm + n_spacer * spacer_mm
    n_main = max(6, int(remaining / bead_size_mm))

    return {"主石": n_main, "配石": n_accent, "隔珠": n_spacer, "顶珠": n_focal}


# ---------------------------------------------------------------------------
# 角色分配：你的表里没有"类别"列，所以角色是从尺寸和名字推出来的
# ---------------------------------------------------------------------------


def split_by_role(pool: list[Material], bead_size_mm: float
                  ) -> dict[str, list[Material]]:
    """把材料池按尺寸分成四种角色。

    ★ 这套分界是我按你的数据定的（主石±1.5mm、配石比主石小、顶珠≥9mm），
      如果你实际做法不是这样，改这里。
    """
    spacers = [m for m in pool if any(k in m.name for k in SPACER_KEYWORDS)]
    rest = [m for m in pool if m not in spacers]

    mains = [m for m in rest if abs(m.size_mm - bead_size_mm) <= 1.5]
    accents = [m for m in rest if m.size_mm <= bead_size_mm - 0.5]
    focals = [m for m in rest if m.size_mm >= 9.0]

    # 没有专门的隔珠就退而求其次：用最小的珠子顶上
    if not spacers:
        spacers = sorted(rest, key=lambda m: m.size_mm)[:5]
    if not focals:
        focals = sorted(rest, key=lambda m: -m.size_mm)[:5]
    if not accents:
        accents = sorted(rest, key=lambda m: m.size_mm)[:8]

    return {"主石": mains, "配石": accents, "隔珠": spacers, "顶珠": focals}


# ---------------------------------------------------------------------------
# 打分与协调性判断
# ---------------------------------------------------------------------------


def score_material(m: Material, brief: CharacterBrief) -> float:
    """给单个材料打分（0.0-1.0）。全是确定性规则，没有"我觉得"。

    权重在文件顶部 WEIGHTS 里，★ 那四个数字是你的经营判断。
    """
    if m.name in brief.avoid:
        return 0.0
    # ★ 专用料闸门：没开许可就一律排除。这条直接对应你"不垫资"的规矩。
    if m.reuse == "专用" and not brief.allow_exclusive:
        return 0.0

    # 色相接近度：中性色给中间分（它跟谁都搭，不该因为"没色相"被罚）
    if brief.hue_center is None or m.is_neutral:
        hue_score = 0.5
    else:
        d = abs(m.hue - brief.hue_center) % 360.0
        d = min(d, 360.0 - d)
        hue_score = max(0.0, 1.0 - d / max(brief.hue_tolerance, 1.0))

    # 复用度：通用 1.0 / 较通用 0.7 / 专用 0.3（在 materials.py 里定义）
    reuse_score = m.reuse_weight

    # 关键词命中：角色关键词有没有出现在备注或颜色描述里
    haystack = f"{m.note} {m.color_desc} {m.name}"
    hits = sum(1 for k in brief.keywords if k and k in haystack)
    keyword_score = min(1.0, hits / max(len(brief.keywords), 1)) if brief.keywords else 0.4

    # 库存健康度：★ 30 颗以上算充足
    stock_score = min(1.0, m.stock / 30.0)

    total = (WEIGHTS["hue"] * hue_score
             + WEIGHTS["reuse"] * reuse_score
             + WEIGHTS["keyword"] * keyword_score
             + WEIGHTS["stock"] * stock_score)

    if m.name in brief.prefer:
        total += 0.15      # ★ 点名要的加分
    return min(1.0, total)


def check_harmony(main: Material, accent: Material, strategy: str) -> bool:
    """判断一对主石/配石符不符合某个策略。四个条件全过才算数。

    这个函数是整个引擎里最能体现审美判断的地方 ——
    面试问"你怎么保证配色不难看"，答案就在这里。
    """
    rule = STRATEGIES[strategy]
    lo, hi = rule["hue_window"]

    # 中性色不参与色相判断，但只在"同色系"里放行 —— 别让它污染另外两种策略
    if accent.is_neutral or main.is_neutral:
        if strategy != "同色系":
            return False
    else:
        gap = hue_distance(main.hex, accent.hex)
        if not (lo <= gap <= hi):
            return False

    if contrast_ratio(main.hex, accent.hex) < rule["min_contrast"]:
        return False

    return DELTA_E_MIN <= delta_e(main.hex, accent.hex) <= DELTA_E_MAX


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _assemble(main, accent, spacer, focal, strategy, structure, brief) -> Scheme | None:
    components = [
        Component(main, structure["主石"], "主石"),
        Component(accent, structure["配石"], "配石"),
        Component(spacer, structure["隔珠"], "隔珠"),
        Component(focal, structure["顶珠"], "顶珠"),
    ]

    # 库存校验：任何一种不够，整套作废。这一行让"推荐了买不到的方案"不可能发生。
    for c in components:
        if c.material.stock < c.qty:
            return None

    material_cost = sum(c.subtotal for c in components)
    reuse_mix: dict[str, int] = {}
    exclusive_cost = 0.0
    for c in components:
        reuse_mix[c.material.reuse] = reuse_mix.get(c.material.reuse, 0) + c.qty
        if c.material.reuse == "专用":
            exclusive_cost += c.subtotal

    q = quote(material_cost, reuse_mix, exclusive_cost)

    # ★ 综合分 = 主石 60% + 配石 40%（主石视觉占比大，所以权重高）
    score = score_material(main, brief) * 0.6 + score_material(accent, brief) * 0.4

    gap = hue_distance(main.hex, accent.hex)
    rationale = (
        f"{main.name}（{main.tone_desc}）作主色，配{accent.name}（{accent.tone_desc}），"
        f"色相相差 {gap:.0f}°属{strategy}；明暗对比 "
        f"{contrast_ratio(main.hex, accent.hex):.2f} 保证层次不糊，"
        f"{spacer.name}过渡、{focal.name}收顶。"
    )

    return Scheme(strategy, components, round(material_cost, 2), q,
                  round(score, 4), rationale)


# ★ 预算怎么分给四个角色。加起来 = 1.0，这四个数是经营判断，可调。
BUDGET_SPLIT = {"主石": 0.70, "顶珠": 0.15, "配石": 0.10, "隔珠": 0.05}


def design(brief: CharacterBrief, materials: list[Material] | None = None,
           top_n: int = 3, verbose: bool = False) -> list[Scheme]:
    """主入口：角色需求 → 若干套配色方案。"""
    pool = materials if materials is not None else available_materials()
    pool = [m for m in pool if m.name not in brief.avoid]
    pool = [m for m in pool if m.unit_price]
    if not brief.allow_exclusive:
        pool = [m for m in pool if m.reuse != "专用"]

    if verbose:
        print(f"[漏斗] 可用材料 {len(pool)} 条"
              f"（allow_exclusive={brief.allow_exclusive}）")

    structure = plan_structure(brief.wrist_cm, brief.bead_size_mm)
    roles = split_by_role(pool, brief.bead_size_mm)

    # ★ 预算约束：按角色配额算出每颗单价上限，超标的出池
    if brief.budget:
        for role, share in BUDGET_SPLIT.items():
            cap = brief.budget * share / max(structure.get(role, 1), 1)
            kept = [m for m in roles[role] if (m.unit_price or 0) <= cap]
            roles[role] = kept or sorted(
                roles[role], key=lambda m: m.unit_price or 0)[:3]
        if verbose:
            print(f"[漏斗] 预算 ¥{brief.budget} 过滤后 " +
                  "  ".join(f"{k}:{len(v)}" for k, v in roles.items()))

    if verbose:
        print(f"[漏斗] 结构 {structure}")
        print(f"[漏斗] 角色池 " +
              "  ".join(f"{k}:{len(v)}" for k, v in roles.items()))

    mains = sorted(roles["主石"], key=lambda m: score_material(m, brief), reverse=True)
    if not (mains and roles["配石"] and roles["隔珠"] and roles["顶珠"]):
        return []

    schemes: list[Scheme] = []
    for strategy in STRATEGIES:
        for main in mains[:8]:
            partners = [a for a in roles["配石"]
                        if a.id != main.id and check_harmony(main, a, strategy)]
            if not partners:
                continue
            accent = max(partners, key=lambda a: score_material(a, brief))

            # ↓ 你问的挑隔珠/顶珠就是这两行。改动：排除已被主石、配石占用的材料
            spacer_pool = [s for s in roles["隔珠"] if s.id not in (main.id, accent.id)]
            focal_pool = [f for f in roles["顶珠"] if f.id not in (main.id, accent.id)]
            if not spacer_pool or not focal_pool:
                continue
            spacer = min(spacer_pool, key=lambda s: abs(delta_e(main.hex, s.hex) - 45))
            focal = min(focal_pool, key=lambda f: abs(delta_e(main.hex, f.hex) - 25))

            s = _assemble(main, accent, spacer, focal, strategy, structure, brief)
            if s:
                schemes.append(s)
                break

    schemes.sort(key=lambda s: s.score, reverse=True)
    return schemes[:top_n]


if __name__ == "__main__":
    brief = CharacterBrief(
        name="望",
        keywords=["冷感", "工程师"],
        hue_center=FAMILY_HUE["蓝"],
        wrist_cm=15.0,
        bead_size_mm=8.0,
        raw_text="望：冷感、工程师气质，蓝色系为主",
    )
    print(f"角色：{brief.name}｜关键词：{'/'.join(brief.keywords)}｜"
          f"意向色相：{brief.hue_center}°")
    print("=" * 64)
    results = design(brief, verbose=True)
    if not results:
        print("\n没有符合条件的方案 —— 试试放宽 hue_tolerance 或开 allow_exclusive")
    for i, s in enumerate(results, 1):
        print(f"\n方案 {i}（综合分 {s.score}）\n{s.render()}")
