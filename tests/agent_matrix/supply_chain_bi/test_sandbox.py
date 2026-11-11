import asyncio
import hashlib
from time import perf_counter
import aiosqlite
import pytest
from agent_matrix.supply_chain_bi.models import SqlExecutionStatus as S
from agent_matrix.supply_chain_bi.sandbox_sqlite import SqliteReadOnlySandbox
from agent_matrix.supply_chain_bi.errors import SandboxUnavailableError
from .fakes import scope_for
from .sim_db import AS_OF

async def execute(sandbox,sql,**kw):
    return await sandbox.execute(sql=sql,bind_params=kw.pop('bind_params',{}),timeout_s=kw.pop('timeout_s',2),max_rows=kw.pop('max_rows',500),**kw)

@pytest.mark.parametrize('sql',["DELETE FROM dim_material","UPDATE dim_material SET material_name='changed'","DROP TABLE dim_material","PRAGMA query_only=OFF","ATTACH DATABASE '/tmp/sandbox_forbidden.db' AS evil"])
async def test_09_independent_readonly(sandbox,db_path,sql):
    digest=hashlib.sha256(db_path.read_bytes()).hexdigest()
    async with aiosqlite.connect(db_path) as observer:
        before=(await (await observer.execute('PRAGMA data_version')).fetchone())[0]
        r=await execute(sandbox,sql)
        after=(await (await observer.execute('PRAGMA data_version')).fetchone())[0]
    assert r.status is S.READ_ONLY_VIOLATION
    assert before==after
    assert digest==hashlib.sha256(db_path.read_bytes()).hexdigest()

async def test_10_timeout(sandbox):
    t=perf_counter()
    r=await execute(sandbox,'WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT SUM(n) FROM x',timeout_s=.2)
    assert r.status is S.TIMEOUT
    assert perf_counter()-t<1

async def test_11_truncation(sandbox):
    r=await execute(sandbox,'SELECT store_id FROM dim_store LIMIT 8',max_rows=5)
    assert r.row_count==5 and r.truncated

async def test_12_unknown_column(sandbox,db_path):
    r=await execute(sandbox,'SELECT missing_col FROM dim_material')
    assert r.status is S.UNKNOWN_COLUMN and 'missing_col' in r.error_hint
    assert str(db_path) not in r.error_hint

@pytest.mark.parametrize('paren',[False,True])
async def test_13_expanding(sandbox,paren):
    p='(:scope_warehouse_ids)' if paren else ':scope_warehouse_ids'
    r=await execute(sandbox,f'SELECT warehouse_id,snapshot_date FROM fact_warehouse_inventory_daily WHERE warehouse_id IN {p} AND region_id=:scope_region_id AND snapshot_date=:as_of_date',bind_params=scope_for('wh_sup_e01e02').bind_params(as_of_date=AS_OF))
    assert r.status is S.SUCCESS,r.error_hint
    assert {row[0] for row in r.rows}=={'WH_E01','WH_E02'}
    assert all(row[1]=='2026-11-01' for row in r.rows)
    assert await sandbox.latest_snapshot_date()==AS_OF

async def test_seed_exact_totals(sandbox):
    r=await execute(sandbox,"SELECT warehouse_id,SUM(outbound_qty) FROM fact_warehouse_inventory_daily WHERE material_id='MAT_PROBIOTIC' GROUP BY warehouse_id")
    assert dict(r.rows)['WH_E01']==285
    assert dict(r.rows)['WH_E02']==196
    r=await execute(sandbox,'SELECT COUNT(*) FROM fact_warehouse_inventory_daily')
    assert r.rows==[[750]]
    r=await execute(sandbox,'SELECT COUNT(*) FROM fact_store_sales_daily')
    assert r.rows==[[140]]

async def test_unavailable(tmp_path):
    with pytest.raises(SandboxUnavailableError):await SqliteReadOnlySandbox(tmp_path/'missing.db').latest_snapshot_date()
