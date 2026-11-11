"""生成供应链 9 张模拟表。全部数据为确定性虚构数据，写入进程临时目录，不落仓库。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import aiosqlite

AS_OF = date(2026, 11, 1)
SCHEMA_PATH = Path(__file__).with_name("supply_chain_schema.sql")

WAREHOUSES = [
    ("WH_E01", "杭州区域仓", "R_EAST", "华东", "regional"),
    ("WH_E02", "宁波前置仓", "R_EAST", "华东", "front"),
    ("WH_E03", "上海区域仓", "R_EAST", "华东", "regional"),
    ("WH_S01", "广州区域仓", "R_SOUTH", "华南", "regional"),
    ("WH_S02", "深圳前置仓", "R_SOUTH", "华南", "front"),
    ("WH_N01", "北京区域仓", "R_NORTH", "华北", "regional"),
    ("WH_N02", "天津前置仓", "R_NORTH", "华北", "front"),
    ("WH_W01", "成都区域仓", "R_WEST", "西南", "regional"),
    ("WH_W02", "重庆前置仓", "R_WEST", "西南", "front"),
]
MATERIALS = [
    ("MAT_COFFEE", "速溶咖啡粉", "dry", "kg", 365, "ambient", 60, 20),
    ("MAT_FLAVOR", "风味液", "dry", "L", 180, "ambient", 90, 30),
    ("MAT_MILK", "纯牛奶", "fresh", "L", 30, "chilled", 120, 40),
    ("MAT_PROBIOTIC", "益生菌粉", "fresh", "kg", 14, "chilled", 40, 12),
    ("MAT_OAT", "燕麦颗粒", "dry", "kg", 90, "ambient", 50, 15),
]
REGION_LOSS_FACTOR = {"R_EAST": 16, "R_SOUTH": 10, "R_NORTH": 7, "R_WEST": 13}
LOSS_REASONS = ("expired", "damaged", "temperature", "other")
SKUS = (("SKU_PROTEIN_CUP", "蛋白粉随行杯", 100), ("SKU_OAT_CUP", "燕麦拿铁随行杯", 80))
# (仓库, 商品) -> (在库, 在途, 占用)：刻意构造的断货风险与积压样本。
INVENTORY_OVERRIDES = {
    ("WH_E01", "MAT_COFFEE"): (20, 10, 6),
    ("WH_E02", "MAT_MILK"): (30, 0, 0),
    ("WH_E03", "MAT_COFFEE"): (900, 0, 0),
    ("WH_S01", "MAT_FLAVOR"): (10, 0, 0),
    ("WH_N02", "MAT_OAT"): (640, 20, 0),
    ("WH_W01", "MAT_PROBIOTIC"): (18, 0, 4),
}
# (仓库, 商品) -> (批次号, 到期日, 剩余量, 生产日)：临期积压样本。
BATCH_OVERRIDES = {
    ("WH_E01", "MAT_PROBIOTIC"): ("B_PROBIO_1027", date(2026, 11, 4), 80, date(2026, 10, 21)),
    ("WH_E02", "MAT_OAT"): ("B_OAT_0801", date(2026, 10, 31), 5, date(2026, 8, 2)),
    ("WH_W02", "MAT_MILK"): ("B_MILK_1006", date(2026, 11, 6), 260, date(2026, 10, 7)),
}


def _rows():
    stores, sales, inventory, batches, losses = [], [], [], [], []
    for wi, (warehouse, name, region, _region_name, _kind) in enumerate(WAREHOUSES):
        for si in range(2):
            store = f"ST_{wi + 1:02d}{si + 1:02d}"
            stores.append((store, f"{name[:2]}{'一二'[si]}店", region, warehouse, name[:2], "AB"[si], "13800138000"))
            for day in range(7):
                biz_date = (AS_OF - timedelta(days=6 - day)).isoformat()
                for sku, sku_name, cups in SKUS:
                    sold = cups + wi * 10 + si * 5 + day
                    sales.append((biz_date, store, region, warehouse, sku, sku_name, sold, sold * 6, 3.25))
        for mi, (material, _n, _c, _u, _life, _s, safety, avg) in enumerate(MATERIALS):
            on_hand, transit, reserved = safety * 2 + (wi * 13 + mi * 7) % 40, 0, 0
            on_hand, transit, reserved = INVENTORY_OVERRIDES.get((warehouse, material), (on_hand, transit, reserved))
            for day in range(30):
                snapshot = (AS_OF - timedelta(days=29 - day)).isoformat()
                inventory.append((snapshot, region, warehouse, material, on_hand, transit, reserved, safety, avg, avg, 77.5))
            batch, expiry, remaining, production = BATCH_OVERRIDES.get(
                (warehouse, material), (f"B_{wi}_{mi}", AS_OF + timedelta(days=60), 10, AS_OF - timedelta(days=2)))
            batches.append((AS_OF.isoformat(), batch, region, warehouse, material, "SUP_01", production.isoformat(),
                            expiry.isoformat(), "2026-10-27", max(100, remaining), remaining, 13.5))
            loss_qty = max(1, ((wi * 7 + mi * 5) % 11 + 2) * REGION_LOSS_FACTOR[region] // 10)
            losses.append((len(losses) + 1, "2026-10-29", region, warehouse, material, batch, loss_qty,
                           LOSS_REASONS[(wi + mi) % 4], 86.5))
    return stores, sales, inventory, batches, losses


async def seed_supply_chain_database(path: Path) -> Path:
    """按模拟库 DDL 建表并写入样本，返回数据库文件路径。"""
    stores, sales, inventory, batches, losses = _rows()
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await db.executemany("INSERT INTO dim_warehouse VALUES (?,?,?,?,?,1)", WAREHOUSES)
        await db.executemany("INSERT INTO dim_material VALUES (?,?,?,?,?,?)", [m[:6] for m in MATERIALS])
        await db.executemany("INSERT INTO dim_store VALUES (?,?,?,?,?,?,?)", stores)
        await db.executemany("INSERT INTO fact_store_sales_daily VALUES (?,?,?,?,?,?,?,?,?)", sales)
        await db.executemany("INSERT INTO fact_warehouse_inventory_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)", inventory)
        await db.executemany("INSERT INTO fact_inventory_batch VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", batches)
        await db.executemany("INSERT INTO fact_material_loss VALUES (?,?,?,?,?,?,?,?,?)", losses)
        await db.execute("INSERT INTO dim_supplier_contract VALUES ('SUP_01','MAT_PROBIOTIC',13.5,0.03,'CONTRACT_01')")
        await db.execute("INSERT INTO fin_purchase_settlement VALUES ('SETTLE_01','SUP_01',91.5,'2026-10')")
        await db.commit()
    return path
