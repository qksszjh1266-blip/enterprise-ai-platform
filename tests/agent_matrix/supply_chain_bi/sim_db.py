"""File-backed supply chain schema, seeded exclusively in temporary directories."""
from datetime import date, timedelta
from pathlib import Path
import aiosqlite
import asyncio

AS_OF = date(2026, 11, 1)
WAREHOUSES = [('WH_E01','杭州区域仓','R_EAST','华东','regional'), ('WH_E02','宁波前置仓','R_EAST','华东','front'), ('WH_E03','上海区域仓','R_EAST','华东','regional'), ('WH_S01','广州区域仓','R_SOUTH','华南','regional'), ('WH_S02','深圳前置仓','R_SOUTH','华南','front')]
MATERIALS = [('MAT_BLACK_TEA','红茶','dry','kg',365,'ambient'), ('MAT_SYRUP','糖浆','dry','L',180,'ambient'), ('MAT_MILK_BASE','奶基底','fresh','L',30,'chilled'), ('MAT_LEMON','柠檬','fresh','kg',14,'chilled'), ('MAT_PEARL','珍珠','dry','kg',90,'ambient')]
SENTINELS = ['13.1415','0.0371','91.8281','77.7713','86.8613','95.9513','13800138000']

async def seed_database(path: Path):
    schema_path = Path(__file__).with_name('schema.sql')
    ddl = await asyncio.to_thread(schema_path.read_text, encoding='utf-8')
    async with aiosqlite.connect(path) as db:
        await db.executescript(ddl)
        await db.executemany('INSERT INTO dim_warehouse VALUES (?,?,?,?,?,1)', WAREHOUSES)
        await db.executemany('INSERT INTO dim_material VALUES (?,?,?,?,?,?)', MATERIALS)
        stores, sales, inventory, losses, batches = [], [], [], [], []
        for wi, (warehouse, name, region, region_name, kind) in enumerate(WAREHOUSES):
            for si in range(2):
                store = f'ST_{wi+1:02d}{si+1:02d}'
                stores.append((store, name+str(si+1)+'店', region, warehouse, name[:2], 'A', '13800138000'))
                for day in range(7):
                    for sku, sku_name, cups in [('SKU_LEMON_TEA','鲜柠檬茶',100),('SKU_MILK_TEA','珍珠奶茶',80)]:
                        sold = cups + wi*10 + si*5 + day
                        sales.append(((AS_OF-timedelta(days=6-day)).isoformat(),store,region,warehouse,sku,sku_name,sold,sold*6,95.9513))
            for mi, (material, *_rest) in enumerate(MATERIALS):
                safety = [60,90,120,40,50][mi]
                on_hand, transit, reserved, avg = safety*2, 0, 0, 20
                if (warehouse,material)==('WH_E01','MAT_BLACK_TEA'): on_hand,transit,reserved,avg=20,10,6,20
                if (warehouse,material)==('WH_E03','MAT_BLACK_TEA'): on_hand,avg=400,20
                if material=='MAT_MILK_BASE': on_hand,avg=(30,40) if warehouse=='WH_E02' else (150,40)
                if material=='MAT_LEMON': on_hand,avg=(80,10) if warehouse=='WH_E01' else (40,20) if warehouse=='WH_E02' else (120,10)
                if (warehouse,material)==('WH_S01','MAT_SYRUP'): on_hand=10
                for day in range(30):
                    # Exact integral totals 285 and 196; last 7 days agree with S3's average.
                    outbound = avg
                    if material=='MAT_LEMON' and warehouse=='WH_E01': outbound=10 if day>=23 else 9 if day<15 else 10
                    if material=='MAT_LEMON' and warehouse=='WH_E02': outbound=20 if day>=23 else 2 if day<13 else 3
                    inventory.append(((AS_OF-timedelta(days=29-day)).isoformat(),region,warehouse,material,on_hand,transit,reserved,safety,outbound,avg,77.7713))
                batch = f'B_{wi}_{mi}'
                expiry, remaining, production = AS_OF+timedelta(days=60), 10, AS_OF-timedelta(days=2)
                if (warehouse,material)==('WH_E01','MAT_LEMON'): batch,expiry,remaining,production='B_LEMON_1027',date(2026,11,4),80,date(2026,10,21)
                if (warehouse,material)==('WH_E02','MAT_PEARL'): batch,expiry,remaining,production='B_PEARL_0801',date(2026,10,31),5,date(2026,8,2)
                batches.append((AS_OF.isoformat(),batch,region,warehouse,material,'SUP_01',production.isoformat(),expiry.isoformat(),'2026-10-27',max(100,remaining),remaining,13.1415))
                if material=='MAT_LEMON':
                    loss = 15 if warehouse=='WH_E01' else 4 if warehouse=='WH_E02' else 1
                    losses.append((len(losses)+1,'2026-10-29',region,warehouse,material,batch,loss,'damaged',86.8613))
        await db.executemany('INSERT INTO dim_store VALUES (?,?,?,?,?,?,?)',stores)
        await db.executemany('INSERT INTO fact_store_sales_daily VALUES (?,?,?,?,?,?,?,?,?)',sales)
        await db.executemany('INSERT INTO fact_warehouse_inventory_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)',inventory)
        await db.executemany('INSERT INTO fact_inventory_batch VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',batches)
        await db.executemany('INSERT INTO fact_material_loss VALUES (?,?,?,?,?,?,?,?,?)',losses)
        await db.execute("INSERT INTO dim_supplier_contract VALUES ('SUP_01','MAT_LEMON',13.1415,0.0371,'CONTRACT_01')")
        await db.execute("INSERT INTO fin_purchase_settlement VALUES ('SETTLE_01','SUP_01',91.8281,'2026-10')")
        await db.commit()
    return path
