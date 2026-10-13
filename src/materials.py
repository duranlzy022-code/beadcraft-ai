"""
材料表读写 —— 这是管道，不是业务逻辑。我写好了，你读懂就行。

【这个文件解决的是"它会忘"那个问题】
模型的记忆随对话窗口滑走，CSV 文件不会。
所有"你有什么料、多少钱、还剩几颗、是不是专用料"的事实只存在 data/materials.csv 一处。
引擎每次都来这里查，所以永远不会编造一个你没有的石头 —— 这就是幻觉率能压到 0 的原因。

⚠️ 注意：hex 那一列是我根据你的"颜色描述"估的，**不准**。
   这一列是整个系统的地基（代码只认 hex，不认"柔灰带毫丝"），
   所以校准它是你的第一个任务，见 学习路线.md 练习 0。
"""

import csv
from dataclasses import dataclass
from pathlib import Path

from .color import describe_tone, hex_to_hsv, is_neutral

_DIR = Path(__file__).resolve().parent.parent / "data"


def _resolve_data_path() -> Path:
    """本地有真实材料表就用它；别人 clone 下来没有，就回退到脱敏样例。"""
    real = _DIR / "materials.csv"
    sample = _DIR / "materials.sample.csv"
    if real.exists():
        return real
    if sample.exists():
        return sample
    raise FileNotFoundError(f"缺少 {real} 或 {sample}")


DATA_PATH = _resolve_data_path()
# 复用度权重：这个字典的数值是**你的经营判断**，不是我的。
# 专用料 = 死钱风险，所以引擎应该优先消耗通用料。具体差多少，你来定。
REUSE_WEIGHT = {"通用": 1.0, "较通用": 0.7, "专用": 0.3}


@dataclass(frozen=True)
class Material:
    """一种珠子。frozen=True 表示创建后不可改，防止代码手滑改坏库存。"""

    id: str
    name: str
    color_family: str      # 色系：黑/白/灰/蓝/紫/黄/红/绿/茶/粉
    color_desc: str        # 你原本写的颜色描述，如"柔灰带毫丝"
    hex: str
    size_mm: float
    stock: int
    unit_price: float | None   # 待购的没有价格，所以可能是 None
    store: str
    reuse: str             # 通用 / 较通用 / 专用
    status: str            # 在库 / 待购
    note: str = ""
    # 穿线长：一颗在绳上占多长。圆珠 = 直径，可留空；隔片、扁珠、桶珠的规格尺寸不是穿线长，要单独填
    thread_mm: float | None = None

    @property
    def along_mm(self) -> float:
        """算手串长度用这个，不要用 size_mm。size_mm 是店家标的规格，只用来匹配材料。"""
        return self.thread_mm if self.thread_mm is not None else self.size_mm

    # --- 算出来的属性，不存在 CSV 里，用到时现算 ---

    @property
    def hue(self) -> float:
        """色相（0-360 度）"""
        return hex_to_hsv(self.hex)[0]

    @property
    def saturation(self) -> float:
        return hex_to_hsv(self.hex)[1]

    @property
    def value(self) -> float:
        """明度（0-1）"""
        return hex_to_hsv(self.hex)[2]

    @property
    def is_neutral(self) -> bool:
        """是不是中性色（白/灰/黑）。中性色是配色里的万能胶。"""
        return is_neutral(self.hex)

    @property
    def tone_desc(self) -> str:
        """代码算出来的色彩描述。方向是 代码→文字，安全；反过来让模型猜 hex 才不可靠。"""
        return describe_tone(self.hex)

    @property
    def available(self) -> bool:
        """能不能现在就用：得在库、有货、有价格。"""
        return self.status == "在库" and self.stock > 0 and self.unit_price is not None

    @property
    def reuse_weight(self) -> float:
        return REUSE_WEIGHT.get(self.reuse, 0.5)

    def __str__(self) -> str:
        price = f"¥{self.unit_price:.2f}" if self.unit_price is not None else "无价"
        return f"{self.name}({self.size_mm:g}mm/{self.reuse}/{price})"


def load_materials(path: Path | str = DATA_PATH) -> list[Material]:
    """从 CSV 读出所有材料。

    【读代码提示】encoding="utf-8-sig" 和 newline="" 是 Windows 上的两个坑：
      · utf-8-sig 吃掉 Excel 存 CSV 时偷加在开头的 BOM（不加的话第一列列名会变成 "﻿id"）
      · newline="" 防止 Windows 换行符造成空行
    你以后自己导表格，这两个参数照抄就不会出问题。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到材料表：{path}")

    out: list[Material] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            if not (row.get("id") or "").strip():
                continue
            try:
                raw_price = (row.get("单价") or "").strip()
                out.append(
                    Material(
                        id=row["id"].strip(),
                        name=row["名称"].strip(),
                        color_family=row["色系"].strip(),
                        color_desc=row["颜色描述"].strip(),
                        hex=row["hex"].strip(),
                        size_mm=float(row["尺寸mm"]),
                        stock=int(row["库存颗数"] or 0),
                        unit_price=float(raw_price) if raw_price else None,
                        store=row["来源店"].strip(),
                        reuse=row["复用度"].strip(),
                        status=row["状态"].strip(),
                        note=(row.get("备注") or "").strip(),
                        thread_mm=float(t) if (t := (row.get("穿线长mm") or "").strip()) else None,
                    )
                )
            except (KeyError, ValueError) as e:
                raise ValueError(f"材料表第 {line_no} 行有问题：{e}") from e

    if not out:
        raise ValueError("材料表是空的")
    return out


def available_materials(path: Path | str = DATA_PATH) -> list[Material]:
    """只返回现在能用的（在库 + 有货 + 有价）。引擎默认用这个。"""
    return [m for m in load_materials(path) if m.available]


if __name__ == "__main__":
    from .color import swatch_block

    mats = load_materials()
    usable = [m for m in mats if m.available]
    print(f"共 {len(mats)} 条，其中可用 {len(usable)} 条，"
          f"待购/无价 {len(mats) - len(usable)} 条\n")

    for fam in ("黑", "白", "灰", "蓝", "紫", "黄", "红", "绿", "茶", "粉"):
        group = [m for m in mats if m.color_family == fam]
        if not group:
            continue
        print(f"【{fam}系】{len(group)} 种")
        for m in group:
            price = f"¥{m.unit_price:>6.2f}" if m.unit_price is not None else "  待购  "
            flag = "" if m.available else "  ⚠不可用"
            print(f"  {swatch_block(m.hex)} {m.name:<12} {m.hex} "
                  f"{m.tone_desc:<5} {m.size_mm:>4.1f}mm {price} "
                  f"×{m.stock:<4} {m.reuse:<4}{flag}")
        print()
