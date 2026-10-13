"""beadcraft MCP server：接单扣料 / 缺料补货 / 渠道管理 / 只读查询。
规则：CSV 是唯一事实来源。写操作改 CSV，然后重建数据库。
stdio 模式下禁止 print（会弄坏通信），日志走 stderr。
"""
import contextlib
import csv
import logging
import math
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from mcp.server import MCPServer
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import build_db  # noqa: E402

DATA = ROOT / "data"
DB = DATA / "beadcraft.db"
USAGE = DATA / "usage.csv"
EXTRA = DATA / "materials_extra.csv"
SOURCES = DATA / "sources.csv"
MAT_FILES = [DATA / "materials.csv", EXTRA]
USAGE_COLS = ["order_ref", "used_on", "material_id", "qty", "note"]
SRC_COLS = ["material_id", "来源店", "单价", "购买规格", "备注"]
MARGIN = 0.10      # 余量：瑕疵 + 算错
MAX_ROWS = 200
# 演示模式：录屏/给别人看时，不输出单价、店铺、备注（备注里常写着进价）
DEMO = os.environ.get("BEADCRAFT_DEMO") == "1"
HIDE = {"unit_price", "supplier", "备注", "buy_spec", "note", "备选渠道"}
PREFIX = {"黑": "B", "白": "W", "灰": "G", "蓝": "U", "紫": "P", "黄": "Y", "绿": "E",
          "茶": "T", "粉": "K", "红": "R", "五金": "H", "绳材": "C", "包装": "PK"}

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("beadcraft")
mcp = MCPServer("beadcraft")


class Item(BaseModel):
    material_id: str
    qty: float = Field(gt=0, description="这一单用多少颗（绳材为米）")


def rebuild():
    with contextlib.redirect_stdout(sys.stderr):   # build_db 会 print，改道
        try:
            build_db.main()
        except SystemExit:
            raise RuntimeError("CSV 已写入，但 build_db 校验没通过：CSV 内容有问题，看日志")
        except Exception as e:
            raise RuntimeError(f"CSV 已写入，但数据库重建失败（{type(e).__name__}: {e}）。"
                               "可能是数据库被别的程序占用（如 DB Browser），关掉后让我重试一次。")


def ro():
    if not DB.exists():
        rebuild()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)   # 只读连接
    con.row_factory = sqlite3.Row
    return con


def rows(sql, params=()):
    con = ro()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchmany(MAX_ROWS)]
    finally:
        con.close()   # 读完立刻关，不长期占着文件


def append_csv(path, fieldnames, new_rows):
    new = not path.exists()
    if not new:
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            with open(path, "ab") as f:
                f.write(b"\n")
    with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(new_rows)


def now():
    """精确到秒：同一天先盘点后接单，这单也要扣。"""
    return datetime.now().isoformat(timespec="seconds")


def fmt(x):
    """整数写成 12 不写 12.0——配色引擎用 int() 读库存颗数，12.0 会报错。"""
    return int(x) if float(x).is_integer() else x


def pub(data):
    """演示模式下把成本、店铺、备注字段去掉；平时原样返回。"""
    if not DEMO:
        return data
    if isinstance(data, list):
        return [pub(x) for x in data]
    if isinstance(data, dict):
        return {k: pub(v) for k, v in data.items() if k not in HIDE}
    return data


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def write_csv(path, header, data):
    """整表写回。只用于换首选店这类必须改旧行的操作。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)


BASE = """SELECT m.material_id, m.name, m.category, m.size_mm, m.unit_price,
                 m.supplier, m.note AS 备注, s.now AS 现有, s.counted_on AS 盘点日
          FROM materials m JOIN stock_now s USING (material_id)"""


@mcp.tool()
def find_material(keyword: str, size_mm: float | None = None) -> list[dict]:
    """按名称关键字找材料（名称里包含即可）。给了 size_mm 就只看 ±0.6mm 以内。
    现有=None 表示没盘点过。返回空列表 = 库里没有，要明确告诉用户"这种材料库里没有，需要去找"。
    匹配不上时先换更短的关键字再试一次（如"银曜石"→"曜石"）。"""
    return pub(rows(BASE + " WHERE m.name LIKE ? AND (? IS NULL OR ABS(m.size_mm - ?) <= 0.6)"
                " ORDER BY m.name, m.size_mm", (f"%{keyword}%", size_mm, size_mm)))


@mcp.tool()
def list_styles() -> list[dict]:
    """列出所有有配方的款式和档位。"""
    return rows("SELECT style, tier, COUNT(*) AS 材料种数 FROM bom GROUP BY style, tier")


@mcp.tool()
def get_recipe(style: str, tier: str) -> list[dict]:
    """取某款式某档位的通用配方（模糊匹配款式名）。
    这是起点不是结论：定制单要把配方列给用户勾选/改颗数，再去 check_order。"""
    return pub(rows("""SELECT b.style, b.tier, b.qty AS 配方颗数, x.* FROM bom b
                   JOIN (""" + BASE + """) x USING (material_id)
                   WHERE b.style LIKE ? AND b.tier = ?""", (f"%{style}%", tier)))


@mcp.tool()
def check_order(items: list[Item]) -> dict:
    """预览一单：够不够、缺多少、找哪家补。不写入任何东西。
    结果必须给用户看；用户确认后才能调用 commit_order。"""
    lines, shop, unknown = [], {}, []
    for it in items:
        r = rows(BASE + " WHERE m.material_id = ?", (it.material_id,))
        if not r:
            unknown.append(it.material_id)
            continue
        r = r[0]
        # 余量只加在珠子上（会有瑕疵、会算错）；五金/整串/包装按实数，1 串不会变成 2 串
        need = math.ceil(it.qty * (1 + MARGIN)) if r["category"] == "珠子" else it.qty
        have = r["现有"]
        if have is None:
            status, buy = "没盘点，先数一下", None
        elif have >= need:
            status, buy = "够", 0
        elif have >= it.qty:
            status, buy = "刚好够，没余量", need - have
        else:
            status, buy = "缺", need - have
        buy = math.ceil(buy) if buy else buy
        line = {"material_id": r["material_id"], "名称": r["name"], "尺寸": r["size_mm"],
                "本单用": it.qty, "含余量": need, "现有": have, "状态": status, "建议买(颗)": buy}
        if buy:
            alt = rows("SELECT supplier, unit_price, buy_spec, note FROM sources"
                       " WHERE material_id = ?", (r["material_id"],))
            if alt:
                line["备选渠道"] = alt
            if DEMO:
                shop.setdefault("首选店", []).append(f'{r["name"]} {r["size_mm"]}mm 买 {buy} 颗')
            else:
                shop.setdefault(r["supplier"] or "未记店铺", []).append(
                    f'{r["name"]} {r["size_mm"]}mm 买 {buy} 颗（备注：{r["备注"] or "无"}）')
        lines.append(line)
    return {"明细": pub(lines), "补货单(按首选店)": shop,
            "库里没有的编号": unknown, "余量规则": f"珠子：向上取整(本单用 × {1 + MARGIN})；五金等按实数"}


@mcp.tool()
def commit_order(order_ref: str, items: list[Item], note: str = "") -> str:
    """把一单的用料记进 usage.csv（扣库存）。只在用户明确确认 check_order 结果之后调用。
    order_ref 用订单编号或"日期+客户简称"，同一个编号不能记两次。"""
    ids = {r["material_id"] for r in rows("SELECT material_id FROM materials")}
    bad = [i.material_id for i in items if i.material_id not in ids]
    if bad:
        return f"没写入：这些编号库里没有 {bad}"
    if rows("SELECT 1 FROM usage WHERE order_ref = ?", (order_ref,)):
        return f"没写入：{order_ref} 已经记过。要改先 undo_order。"
    today = now()
    append_csv(USAGE, USAGE_COLS,
               [{"order_ref": order_ref, "used_on": today, "material_id": i.material_id,
                 "qty": i.qty, "note": note} for i in items])
    rebuild()
    return f"已记 {order_ref}：{len(items)} 种材料，日期 {today}"


@mcp.tool()
def undo_order(order_ref: str) -> str:
    """撤回一单的扣料（追加负数行，不删历史）。"""
    net = rows("SELECT material_id, SUM(qty) AS q FROM usage WHERE order_ref = ?"
               " GROUP BY material_id HAVING q != 0", (order_ref,))
    if not net:
        return f"{order_ref} 没有可撤回的记录"
    append_csv(USAGE, USAGE_COLS,
               [{"order_ref": order_ref, "used_on": now(),
                 "material_id": r["material_id"], "qty": -r["q"], "note": "撤回"} for r in net])
    rebuild()
    return f"已撤回 {order_ref}：{len(net)} 种材料"


@mcp.tool()
def receive_stock(material_id: str, qty: float, note: str = "") -> str:
    """进货入库：买回来的珠子加进库存（在 usage.csv 记负数 = 库存增加）。
    qty 用用户实际收到的颗数，不是下单数。"""
    if qty <= 0:
        return "qty 要大于 0"
    if not rows("SELECT 1 FROM materials WHERE material_id = ?", (material_id,)):
        return f"没写入：库里没有 {material_id}，先 add_material"
    today = now()
    append_csv(USAGE, USAGE_COLS,
               [{"order_ref": f"进货-{today}", "used_on": today, "material_id": material_id,
                 "qty": -qty, "note": note or "进货"}])
    rebuild()
    return f"已入库 {material_id} {qty}"


@mcp.tool()
def count_stock(material_id: str, qty: float) -> str:
    """盘点：用户亲手数过某材料，现在还剩 qty（单位与配方一致：珠子按颗，整串用的五金按串，绳按米）。这是覆盖，不是加减；同时记下盘点时间，
    之前的扣料/进货记录从此不再影响这个数。买回来的货用 receive_stock，不要用这个。"""
    if qty < 0:
        return "qty 不能是负数"
    for path in MAT_FILES:
        header, data = read_csv(path)
        hit = [r for r in data if r["id"] == material_id]
        if hit:
            break
    else:
        return f"没写入：库里没有 {material_id}，先 add_material"
    if "盘点日期" not in header:
        header = header + ["盘点日期"]
    hit[0]["库存颗数"] = fmt(qty)
    hit[0]["盘点日期"] = now()
    write_csv(path, header, data)
    rebuild()
    return f"已盘点 {material_id}：{fmt(qty)}（单位同配方：珠子按颗，整串五金按串，绳按米；{hit[0]['盘点日期']}）"


@mcp.tool()
def add_material(name: str, family: str, size_mm: float | None = None,
                 unit_price: float | None = None, supplier: str = "",
                 buy_spec: str = "", stock: float | None = None,
                 thread_mm: float | None = None) -> str:
    """新材料入库（写进 materials_extra.csv）。family 取：黑/白/灰/蓝/紫/黄/绿/茶/粉/红/五金/绳材/包装。
    同一种珠子（同名同尺寸）换店/加店不是新材料，用 add_source。
    用户没说的字段留空，不许猜。buy_spec 写店铺卖法，如"20元/30颗"。
    stock 只在用户数过时填；填了就记今天为盘点日。
    thread_mm 是一颗在绳上实际占多长：圆珠不填（等于直径），隔片/扁珠/堵头等用户量过才填。"""
    if family not in PREFIX:
        return f"family 只能是 {list(PREFIX)}"
    same = rows("SELECT material_id, name, size_mm, supplier FROM materials WHERE name = ? AND"
                " (size_mm IS ? OR ABS(size_mm - ?) < 0.05)", (name, size_mm, size_mm or 0))
    if same:
        return (f"没写入：已有同名同尺寸 {same}。换店/加店用 add_source，"
                "要改首选店用 set_primary。")
    p = PREFIX[family]
    nums = [int(r["material_id"][len(p):]) for r in rows(
        "SELECT material_id FROM materials WHERE material_id LIKE ?", (p + "%",))
        if r["material_id"][len(p):].isdigit()]
    mid = f"{p}{(max(nums) if nums else 0) + 1:0{2 if p == 'PK' else 3}d}"
    header, _ = read_csv(EXTRA)
    append_csv(EXTRA, header, [{
        "id": mid, "名称": name, "色系": family, "尺寸mm": "" if size_mm is None else size_mm,
        "穿线长mm": "" if thread_mm is None else thread_mm,
        "单价": "" if unit_price is None else unit_price, "来源店": supplier,
        "状态": "在库" if stock else "待购", "备注": buy_spec,
        "库存颗数": "" if stock is None else fmt(stock),
        "盘点日期": "" if stock is None else now()}])
    rebuild()
    return f"已加入 {mid} {name}"


@mcp.tool()
def add_source(material_id: str, supplier: str, note: str,
               unit_price: float | None = None, buy_spec: str = "") -> str:
    """给已有材料加一个备选购买渠道（同一种珠子的另一家店）。note 必填：为什么留这家
    （如"首选断货时用""贵 0.2 但发货快"）。用户没说的价格/规格留空，不许猜。"""
    supplier = supplier.strip()
    if not note.strip():
        return "没写入：备选渠道必须写备注，先问用户为什么留这家"
    m = rows("SELECT supplier FROM materials WHERE material_id = ?", (material_id,))
    if not m:
        return f"没写入：库里没有 {material_id}，先 add_material"
    if m[0]["supplier"] == supplier:
        return f"没写入：{supplier} 已经是 {material_id} 的首选店"
    if rows("SELECT 1 FROM sources WHERE material_id = ? AND supplier = ?", (material_id, supplier)):
        return f"没写入：{material_id} 已有 {supplier} 这个备选渠道"
    append_csv(SOURCES, SRC_COLS, [{"material_id": material_id, "来源店": supplier,
               "单价": "" if unit_price is None else unit_price, "购买规格": buy_spec, "备注": note}])
    rebuild()
    return f"已给 {material_id} 加备选渠道 {supplier}"


@mcp.tool()
def set_primary(material_id: str, supplier: str, old_note: str) -> str:
    """把某个备选渠道升为首选店；原首选店自动降为备选。old_note 必填：原首选店为什么还留着
    （如"原首选，涨价后换掉"）。只在用户明确要求换店时调用。"""
    if not old_note.strip():
        return "没写入：原首选店降级要写备注，先问用户"
    alt = rows("SELECT * FROM sources WHERE material_id = ? AND supplier = ?", (material_id, supplier))
    if not alt:
        return f"没写入：{material_id} 没有 {supplier} 这个备选渠道，先 add_source"
    alt = alt[0]
    for path in MAT_FILES:
        header, data = read_csv(path)
        hit = [r for r in data if r["id"] == material_id]
        if hit:
            break
    old = dict(hit[0])
    hit[0]["来源店"] = supplier
    hit[0]["单价"] = "" if alt["unit_price"] is None else alt["unit_price"]
    hit[0]["备注"] = alt["buy_spec"] or ""
    write_csv(path, header, data)
    sh, sd = read_csv(SOURCES)
    sd = [r for r in sd if not (r["material_id"] == material_id and r["来源店"] == supplier)]
    if (old.get("来源店") or "").strip():
        sd.append({"material_id": material_id, "来源店": old["来源店"], "单价": old.get("单价", ""),
                   "购买规格": old.get("备注", ""), "备注": old_note})
    write_csv(SOURCES, sh, sd)
    rebuild()
    return f"{material_id} 首选店已换成 {supplier}；原首选 {old.get('来源店') or '无'} 已降为备选"


@mcp.tool()
def quote_order(items: list[Item]) -> dict:
    """给一单报价：按这单【实际用料】算材料成本，再交给配色报价引擎原有的 pricing.quote 出价。
    定制单先确认用料（可从 get_recipe 起步再改），再调用这个。只读，不扣库存。"""
    if DEMO:
        return {"错误": "演示模式下不输出成本和报价"}
    from src import pricing
    cost, excl, mix, no_price, no_reuse, lines = 0.0, 0.0, {}, [], [], []
    for it in items:
        r = rows("SELECT material_id, name, unit_price, reuse FROM materials WHERE material_id = ?",
                 (it.material_id,))
        if not r:
            no_price.append(f"{it.material_id}（库里没有）")
            continue
        r = r[0]
        if r["unit_price"] is None:
            no_price.append(f'{r["material_id"]} {r["name"]}')
            continue
        sub = it.qty * r["unit_price"]
        cost += sub
        tag = r["reuse"] or "未标"
        if tag == "未标":
            no_reuse.append(f'{r["material_id"]} {r["name"]}')
        mix[tag] = mix.get(tag, 0) + it.qty
        if tag == "专用":
            excl += sub
        lines.append({"material_id": r["material_id"], "名称": r["name"], "用量": it.qty,
                      "小计": round(sub, 2), "复用度": tag})
    q = pricing.quote(cost, mix, excl)
    out = {"明细": lines, "材料成本": round(cost, 2), "档位": q.tier, "建议价": q.suggested_price,
           "议价底线": q.floor_price, "定金": q.deposit_required, "说明": q.notes}
    if no_price:
        out["⚠ 缺单价，未计入成本"] = no_price
    if no_reuse:
        out["⚠ 未标复用度，按通用料判档"] = no_reuse
    return out


@mcp.tool()
def query_db(sql: str) -> list[dict]:
    """只读 SQL 查询（SELECT / WITH），最多返回 200 行。
    表：materials, orders, style_map, bom, usage, sources；视图：stock_now。"""
    if DEMO:
        return [{"错误": "演示模式下关闭自由查询（避免查出成本）"}]
    if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
        return [{"错误": "只允许 SELECT / WITH"}]
    return rows(sql)


if __name__ == "__main__":
    mcp.run(transport="stdio")
