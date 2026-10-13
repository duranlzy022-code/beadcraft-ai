"""
build_db.py —— 把 CSV 灌进 SQLite。

规则：CSV 是唯一事实来源，数据库每次从零重建，只是副本。
      要改数据就改 CSV，再跑一次这个脚本。永远不要直接改数据库。

用法：python build_db.py
"""
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DB = DATA / "beadcraft.db"

# 款式映射：订单里的 product 原文 → 标准款式名。放在 data/style_map.csv（不进 git，含真实款式名）。
# 列：product,style。同一款在不同平台记账写法不同时，多行映射到同一个 style。
def load_style_map():
    p = DATA / "style_map.csv"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8-sig", newline="") as f:
        return {r["product"]: r["style"] for r in csv.DictReader(f)}


NON_BEAD = {"五金", "绳材", "包装"}   # 色系列里填这几个的，类别就是它本身，其余算珠子

SCHEMA = """
CREATE TABLE materials (
    material_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,          -- 珠子 / 五金 / 绳材 / 包装
    family      TEXT,
    size_mm     REAL,
    unit_price  REAL,                   -- 元/颗；绳材为 元/米
    supplier    TEXT,
    reuse       TEXT,
    status      TEXT,
    note        TEXT,
    stock       REAL,                   -- 盘点数；空 = 没数过，0 = 数过没有
    counted_on  TEXT                    -- 盘点日期 YYYY-MM-DD
);
CREATE TABLE orders (
    row_id      INTEGER PRIMARY KEY,    -- 行号。customer_id 有重复（C001），不能当主键
    order_date  TEXT,
    channel     TEXT,
    source      TEXT,
    product     TEXT,
    ip_source   TEXT,
    tier        TEXT,
    amount      REAL,
    customer_id TEXT,
    order_type  TEXT
);
CREATE TABLE style_map (
    product     TEXT PRIMARY KEY,
    style       TEXT NOT NULL
);
CREATE TABLE bom (
    style       TEXT NOT NULL,
    tier        TEXT NOT NULL,
    material_id TEXT NOT NULL REFERENCES materials(material_id),
    qty         REAL NOT NULL,          -- 单价不存这里，只存在 materials，避免两处不一致
    source      TEXT,
    PRIMARY KEY (style, tier, material_id)
);
-- 一行 = 一次库存变动：正数 = 用掉，负数 = 进货或撤回；无业务主键，用 rowid
CREATE TABLE usage (
    order_ref   TEXT NOT NULL,
    used_on     TEXT NOT NULL,
    material_id TEXT NOT NULL REFERENCES materials(material_id),
    qty         REAL NOT NULL,
    note        TEXT
);
-- 一行 = 某材料的一个备选购买渠道（首选渠道仍在 materials 的 来源店/单价）
-- 主键 = (material_id, supplier)；回答：还能去哪买 / 为什么留这家 / 首选断货换哪家
CREATE TABLE sources (
    material_id TEXT NOT NULL REFERENCES materials(material_id),
    supplier    TEXT NOT NULL,
    unit_price  REAL,
    buy_spec    TEXT,
    note        TEXT NOT NULL,
    PRIMARY KEY (material_id, supplier)
);
-- 现有颗数 = 盘点数 − 盘点日之后的扣料；没盘点过 → NULL
CREATE VIEW stock_now AS
SELECT m.material_id, m.name, m.size_mm, m.stock, m.counted_on,
       m.stock - COALESCE((SELECT SUM(u.qty) FROM usage u
                           WHERE u.material_id = m.material_id
                             AND u.used_on > m.counted_on), 0) AS now
FROM materials m;
"""


def fail(msg):
    print(f"\n✗ {msg}")
    sys.exit(1)


def read(path, need):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if rows:
        miss = [c for c in need if c not in rows[0]]
        if miss:
            fail(f"{path.name} 缺少列：{miss}\n  实际的列是：{list(rows[0].keys())}")
    return rows


def num(x):
    x = (x or "").strip()
    return float(x) if x else None


def main():
    # ---------- 找文件 ----------
    mat_main = DATA / "materials.csv"
    if not mat_main.exists():
        mat_main = DATA / "materials.sample.csv"
    mat_extra = DATA / "materials_extra.csv"
    bom_path = DATA / "bom.csv"                 # 只认 data 里的，根目录那个是旧的引擎产物
    ord_path = DATA / "orders.csv" if (DATA / "orders.csv").exists() else ROOT / "orders.csv"

    if not mat_main.exists():
        fail(f"找不到 {mat_main}")
    # 其余三张表缺了就当空表：新用户从自己的空库开始，不依赖任何人的商家数据
    for p, cols in ((mat_extra, "id,名称,色系,颜色描述,hex,尺寸mm,穿线长mm,库存颗数,单价,来源店,复用度,状态,备注,盘点日期"),
                    (bom_path, "款式,档位,material_id,材料名,颗数,单价,小计,来源"),
                    (ord_path, "order_date,channel,source,product,ip_source,tier,amount,customer_id,order_type")):
        if not p.exists():
            p.write_text(cols + "\n", encoding="utf-8-sig")
            print(f"  新建空表 {p.relative_to(ROOT)}")
    print("读取文件：")
    for p in (mat_main, mat_extra, bom_path, ord_path):
        print(f"  {p.relative_to(ROOT)}")

    mat_cols = ["id", "名称", "色系", "单价"]
    materials = read(mat_main, mat_cols) + read(mat_extra, mat_cols)
    bom = read(bom_path, ["款式", "档位", "material_id", "颗数"])
    orders = read(ord_path, ["order_date", "product", "tier", "amount", "order_type"])

    # ---------- 校验 1：材料编号不能重复 ----------
    seen, dup = set(), []
    for m in materials:
        if m["id"] in seen:
            dup.append(m["id"])
        seen.add(m["id"])
    if dup:
        fail(f"材料编号重复：{dup}\n  materials.csv 和 materials_extra.csv 里有相同的 id，改掉一个再跑")

    price = {m["id"]: num(m["单价"]) for m in materials}

    # ---------- 校验 2：配方里的材料必须存在 ----------
    missing = sorted({b["material_id"] for b in bom if b["material_id"] not in price})
    if missing:
        fail(f"bom.csv 用到了材料表里没有的编号：{missing}")

    usage_path = DATA / "usage.csv"
    usage = read(usage_path, ["order_ref", "used_on", "material_id", "qty"]) if usage_path.exists() else []

    # ---------- 校验 4：扣料记录的材料必须存在；有盘点数就必须有盘点日期 ----------
    bad = sorted({u["material_id"] for u in usage if u["material_id"] not in price})
    if bad:
        fail(f"usage.csv 用到了材料表里没有的编号：{bad}")
    # 没有盘点日期的库存数一律当"没盘点"：旧表里的数字是买入量，不能当剩余量用
    nodate = [m["id"] for m in materials if (m.get("库存颗数") or "").strip() and not (m.get("盘点日期") or "").strip()]

    src_path = DATA / "sources.csv"
    sources = read(src_path, ["material_id", "来源店", "单价", "购买规格", "备注"]) if src_path.exists() else []

    # ---------- 校验 5：备选渠道——材料存在、备注必填、不能和首选同店、不能重复 ----------
    primary = {m["id"]: (m.get("来源店") or "").strip() for m in materials}
    seen_src = set()
    for s_ in sources:
        k = (s_["material_id"], s_["来源店"].strip())
        if s_["material_id"] not in primary:
            fail(f"sources.csv 用到了材料表里没有的编号：{k}")
        if not (s_["备注"] or "").strip():
            fail(f"sources.csv 备选渠道必须写备注（为什么留这家）：{k}")
        if k[1] == primary[k[0]]:
            fail(f"sources.csv 里的店和首选店重复：{k}")
        if k in seen_src:
            fail(f"sources.csv 重复：{k}")
        seen_src.add(k)

    # ---------- 校验 3：配方里记的单价 vs 材料表单价（只提醒，不中断） ----------
    diff = []
    for b in bom:
        written, table = num(b.get("单价")), price[b["material_id"]]
        if written is not None and table is not None and abs(written - table) > 0.005:
            diff.append((b["款式"], b["档位"], b["material_id"], b.get("材料名", ""), written, table))

    # ---------- 建库 ----------
    # 不删文件，原地清空重建：Windows 上数据库被别的程序打开时删不掉（MCP 服务器、DB Browser）
    con = sqlite3.connect(DB, timeout=10)
    old = con.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    for kind, name in sorted(old, key=lambda x: x[0] != 'view'):   # 先删视图再删表
        con.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
    con.commit()
    con.executescript(SCHEMA)

    con.executemany(
        "INSERT INTO materials VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(m["id"], m["名称"],
          m["色系"] if m["色系"] in NON_BEAD else "珠子",
          m["色系"], num(m.get("尺寸mm")), num(m["单价"]),
          m.get("来源店", ""), m.get("复用度", ""), m.get("状态", ""), m.get("备注", ""),
          num(m.get("库存颗数")) if (m.get("盘点日期") or "").strip() else None,
          (m.get("盘点日期") or "").strip() or None)
         for m in materials])

    con.executemany(
        "INSERT INTO orders (order_date,channel,source,product,ip_source,tier,amount,customer_id,order_type)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(o["order_date"], o.get("channel", ""), o.get("source", ""), o["product"],
          o.get("ip_source", ""), o["tier"], num(o["amount"]),
          o.get("customer_id", ""), o["order_type"])
         for o in orders])

    con.executemany("INSERT INTO style_map VALUES (?,?)", load_style_map().items())

    con.executemany(
        "INSERT INTO bom VALUES (?,?,?,?,?)",
        [(b["款式"], b["档位"], b["material_id"], num(b["颗数"]), b.get("来源", ""))
         for b in bom])
    con.executemany(
        "INSERT INTO usage VALUES (?,?,?,?,?)",
        [(u["order_ref"], u["used_on"], u["material_id"], num(u["qty"]), u.get("note", ""))
         for u in usage])
    con.executemany(
        "INSERT INTO sources VALUES (?,?,?,?,?)",
        [(s_["material_id"], s_["来源店"].strip(), num(s_["单价"]), s_["购买规格"], s_["备注"])
         for s_ in sources])
    con.commit()

    # ---------- 汇报 ----------
    print(f"\n✓ 已生成 {DB.relative_to(ROOT)}")
    for t in ("materials", "orders", "style_map", "bom", "usage", "sources"):
        print(f"  {t:10} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>4} 行")

    if nodate:
        print(f"\n⚠ {len(nodate)} 种材料有库存数但没盘点日期，按“没盘点”处理（盘点后补日期即可）")

    if diff:
        print("\n⚠ bom.csv 里写的单价和材料表对不上（数据库按材料表算）：")
        for s, t, mid, name, w, tb in diff:
            print(f"  {s}/{t} {mid} {name}：bom 写 {w}，材料表是 {tb}")

    print("\n【成本结构】bom → materials")
    q1 = """
    SELECT b.style, b.tier,
           ROUND(SUM(b.qty*m.unit_price),2),
           ROUND(SUM(CASE WHEN m.category='珠子' THEN b.qty*m.unit_price ELSE 0 END),2),
           ROUND(SUM(CASE WHEN m.category='五金' THEN b.qty*m.unit_price ELSE 0 END),2),
           ROUND(SUM(CASE WHEN m.category='绳材' THEN b.qty*m.unit_price ELSE 0 END),2)
    FROM bom b JOIN materials m USING (material_id)
    GROUP BY b.style, b.tier ORDER BY b.style, b.tier DESC
    """
    print(f"  {'款式':6}{'档位':4}{'材料成本':>8}{'珠子':>8}{'五金':>8}{'绳材':>6}")
    for r in con.execute(q1):
        print(f"  {r[0]:6}{r[1]:4}{r[2]:>8}{r[3]:>8}{r[4]:>8}{r[5]:>6}")

    print("\n【订单对上配方】orders → style_map → bom → materials")
    q2 = """
    WITH cost AS (
        SELECT b.style, b.tier, SUM(b.qty*m.unit_price) AS c
        FROM bom b JOIN materials m USING (material_id)
        GROUP BY b.style, b.tier)
    SELECT s.style, o.tier, COUNT(*), ROUND(AVG(o.amount),1), ROUND(c.c,2),
           ROUND(AVG(o.amount)/c.c,2)
    FROM orders o
    JOIN style_map s ON s.product = o.product
    JOIN cost c      ON c.style = s.style AND c.tier = o.tier
    WHERE o.order_type = '商品'
    GROUP BY s.style, o.tier ORDER BY s.style, o.tier DESC
    """
    print(f"  {'款式':6}{'档位':4}{'单数':>5}{'均价':>8}{'材料成本':>9}{'均价/成本':>9}")
    for r in con.execute(q2):
        print(f"  {r[0]:6}{r[1]:4}{r[2]:>5}{r[3]:>8}{r[4]:>9}{r[5]:>9}")

    total = con.execute("SELECT COUNT(*) FROM orders WHERE order_type='商品'").fetchone()[0]
    hit = con.execute("""
        SELECT COUNT(*) FROM orders o
        JOIN style_map s ON s.product = o.product
        WHERE o.order_type='商品'
          AND EXISTS (SELECT 1 FROM bom b WHERE b.style=s.style AND b.tier=o.tier)
    """).fetchone()[0]
    print(f"\n  配方覆盖：{hit} / {total} 单商品订单")
    con.close()


if __name__ == "__main__":
    main()