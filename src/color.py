"""
色彩数学模块 —— 全部是确定性计算，一行 LLM 调用都没有。

【为什么这个文件是整个项目的地基】
模型不"看见"颜色，它只是在操作"雾霾蓝"这三个字。
而这里的每个函数都在真正地算颜色：算色相差了多少度、算两个颜色人眼看着差多远。
配色决策交给这里，结果就是 100% 可复现的 —— 同样输入永远同样输出。

只用 Python 标准库，不需要装任何东西。
"""

import colorsys
import math

# ---------------------------------------------------------------------------
# 第一层：格式转换
# ---------------------------------------------------------------------------


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """把 '#7A9BB5' 变成 (122, 155, 181)。

    计算机里颜色就是三个 0-255 的数：红、绿、蓝各有多少。
    #7A9BB5 是十六进制写法，7A=122、9B=155、B5=181。
    """
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:  # 支持 #abc 这种缩写
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"不是合法的 hex 颜色：{hex_color}")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """反过来，(122, 155, 181) 变成 '#7A9BB5'。"""
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, int(c))) for c in rgb))


def hex_to_hsv(hex_color: str) -> tuple[float, float, float]:
    """把 hex 变成 HSV：色相(0-360度)、饱和度(0-1)、明度(0-1)。

    HSV 比 RGB 符合人的直觉：
      - 色相 H = 这是什么颜色（0红 / 120绿 / 240蓝，是个圆环）
      - 饱和度 S = 有多鲜艳（0是灰的，1是最艳）
      - 明度 V = 有多亮（0是黑的，1是最亮）
    客户说"我要清冷一点"，翻译过来就是：色相偏蓝绿、饱和度低、明度中高。
    """
    r, g, b = (c / 255.0 for c in hex_to_rgb(hex_color))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return (h * 360.0, s, v)


# ---------------------------------------------------------------------------
# 第二层：LAB 色彩空间（用来算"人眼觉得差多远"）
# ---------------------------------------------------------------------------


def _srgb_to_linear(c: float) -> float:
    """撤销显示器的伽马校正。屏幕显示颜色时做过一次非线性处理，算数学之前要还原。"""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_to_lab(hex_color: str) -> tuple[float, float, float]:
    """把 hex 变成 LAB。

    【为什么不直接用 RGB 算距离】
    RGB 里两个颜色数值差得远，人眼看着不一定差得远（人眼对绿色特别敏感，对蓝色迟钝）。
    LAB 是专门设计成"数值距离 ≈ 人眼感觉距离"的空间。
    要判断"这两颗珠子放一起会不会糊成一片"，必须在 LAB 里算。

    L = 亮度(0-100)，a = 绿→红，b = 蓝→黄
    """
    r, g, b = (_srgb_to_linear(c / 255.0) for c in hex_to_rgb(hex_color))

    # sRGB -> XYZ（D65 标准光源下的转换矩阵，这是国际标准，照抄即可）
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041

    # 用 D65 白点归一化
    x, y, z = x / 0.95047, y / 1.00000, z / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t) + (16 / 116)

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(hex_a: str, hex_b: str) -> float:
    """两个颜色的"人眼可感知差异"，业内叫 ΔE。

    经验刻度：
      ΔE < 2   几乎看不出区别（放一起会糊）
      ΔE 10-25 明显不同但协调（配色的甜区）
      ΔE > 50  强烈对比（要小心，容易花）
    """
    la, aa, ba = hex_to_lab(hex_a)
    lb, ab, bb = hex_to_lab(hex_b)
    return math.sqrt((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2)


# ---------------------------------------------------------------------------
# 第三层：配色规则要用的判断
# ---------------------------------------------------------------------------


def hue_distance(hex_a: str, hex_b: str) -> float:
    """两个颜色在色相环上差多少度（0-180）。

    色相是个圆环，所以 350度 和 10度 只差 20度，不是 340度。
    这个函数是三种配色策略的判据。
    """
    ha = hex_to_hsv(hex_a)[0]
    hb = hex_to_hsv(hex_b)[0]
    d = abs(ha - hb) % 360.0
    return min(d, 360.0 - d)


def relative_luminance(hex_color: str) -> float:
    """相对亮度(0-1)，用来算对比度。这是 W3C 无障碍标准里的公式。"""
    r, g, b = (_srgb_to_linear(c / 255.0) for c in hex_to_rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """明暗对比度(1-21)。

    手串上用来判断：主石和配石会不会因为深浅太接近而"看不出层次"。
    经验值：< 1.3 层次不足，1.5-4 舒适，> 8 很跳。
    """
    la, lb = relative_luminance(hex_a), relative_luminance(hex_b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def is_neutral(hex_color: str, sat_threshold: float = 0.12) -> bool:
    """是不是中性色（白/灰/黑/米色）。

    中性色是配色里的"万能胶"，跟谁都不打架，
    所以下面的配色规则会给它开绿灯，不参与色相冲突判断。
    """
    return hex_to_hsv(hex_color)[1] < sat_threshold


# ---------------------------------------------------------------------------
# 第四层：给人看的输出
# ---------------------------------------------------------------------------


def swatch_block(hex_color: str, width: int = 4) -> str:
    """在终端里打印一个真实的彩色方块（ANSI 转义码）。

    这样你跑脚本的时候能直接看见配色效果，不用脑补。
    """
    r, g, b = hex_to_rgb(hex_color)
    return f"\033[48;2;{r};{g};{b}m{' ' * width}\033[0m"


def describe_tone(hex_color: str) -> str:
    """把 hex 翻译成中文色彩描述 —— 注意方向：代码→文字，不是文字→代码。

    这是安全的（确定性映射），反过来让模型把"雾霾蓝"猜成 hex 才是不可靠的。
    """
    h, s, v = hex_to_hsv(hex_color)

    # 极暗和极淡的颜色，色相已经没有意义了，先拦掉
    if v < 0.16:
        return "近黑"
    if s < 0.12:
        return "浅灰白" if v > 0.8 else "中性灰" if v > 0.35 else "深灰"
    if s < 0.22 and v > 0.85:
        return "米白"

    hue_names = [
        (15, "红"), (45, "橙"), (70, "黄"), (160, "绿"),
        (200, "青"), (255, "蓝"), (290, "紫"), (345, "品红"), (360, "红"),
    ]
    hue = next(name for bound, name in hue_names if h < bound)

    lightness = "浅" if v > 0.75 else "深" if v < 0.4 else ""
    vividness = "淡" if s < 0.35 else "浓" if s > 0.7 else ""
    return f"{lightness}{vividness}{hue}".strip() or hue


if __name__ == "__main__":
    # 直接跑 `python src/color.py` 就能看到这段自检，确认色彩数学是对的
    pairs = [
        ("#1B3A5C", "#7A9BB5", "深蓝 + 雾霾蓝（同色系）"),
        ("#1B3A5C", "#C97B4A", "深蓝 + 赭橙（对比色）"),
        ("#7A9BB5", "#7E9EB8", "两个几乎一样的蓝（会糊）"),
    ]
    print("色彩数学自检\n" + "=" * 52)
    for a, b, label in pairs:
        print(f"{swatch_block(a)}{swatch_block(b)}  {label}")
        print(f"    色相差 {hue_distance(a, b):5.1f}°   "
              f"ΔE {delta_e(a, b):5.1f}   "
              f"对比度 {contrast_ratio(a, b):.2f}")
        print(f"    描述：{describe_tone(a)} / {describe_tone(b)}\n")
