"""SQL guard coverage for G1–G25 and scope bypass adversarial cases."""
import pytest
from agent_matrix.supply_chain_bi.guardrails import SqlStaticGuard, mask_sql_for_display
from agent_matrix.supply_chain_bi.semantic_layer import SemanticLayer, ALERT_NEAR_EXPIRY_BATCHES
from agent_matrix.supply_chain_bi.models import SqlViolationCode as V, ScopeLevel
from .fakes import scope_for

P = 'i.region_id = :scope_region_id AND i.warehouse_id IN :scope_warehouse_ids'
Q = f'SELECT i.warehouse_id FROM fact_warehouse_inventory_daily i WHERE {P}'
CASES = [
 ('G1', Q, None), ('G2', Q+' LIMIT 100000', None),
 ('G3', f'WITH a AS ({Q}) SELECT a.warehouse_id FROM a', None),
 ('G4','SELECT 1; DROP TABLE dim_material',V.WRITE_OPERATION),
 *[('G5',s,V.WRITE_OPERATION) for s in ['DELETE FROM dim_material','UPDATE dim_material SET material_name=\'x\'','INSERT INTO dim_material(material_id) VALUES (\'x\')','CREATE TABLE x AS SELECT 1','ATTACH DATABASE \'a\' AS a','PRAGMA table_info(dim_material)','WITH x AS (DELETE FROM dim_material RETURNING *) SELECT 1']],
 *[('G6',f'SELECT name FROM {table}',V.SYSTEM_CATALOG_ACCESS) for table in ['sqlite_master','"SQLITE_MASTER"','main.sqlite_schema','information_schema.columns','pg_catalog.pg_user']],
 ('G7','SELECT contract_no FROM dim_supplier_contract',V.FORBIDDEN_TABLE),
 *[('G8',s,V.SENSITIVE_COLUMN) for s in [Q.replace('i.warehouse_id FROM','i.supplier_cost_price FROM'), Q+' ORDER BY supplier_cost_price',Q.replace('i.warehouse_id FROM','SUM(i.supplier_cost_price) FROM'),Q.replace('i.warehouse_id FROM','i."Supplier_Cost_Price" FROM'),'SELECT (SELECT supplier_cost_price FROM fact_inventory_batch)']],
 ('G9',Q.replace('SELECT i.warehouse_id','SELECT *'),V.SELECT_STAR_FORBIDDEN),
 ('G9',Q.replace('SELECT i.warehouse_id','SELECT i.*'),V.SELECT_STAR_FORBIDDEN),
 ('G9-count',Q.replace('SELECT i.warehouse_id','SELECT COUNT(*)'),None),
 ('G10',Q.replace('fact_warehouse_inventory_daily','fact_material_loss').replace('SELECT i.warehouse_id','SELECT i.loss_id'),V.COLUMN_NOT_WHITELISTED),
 ('G11','SELECT x FROM fact_unknown',V.TABLE_NOT_WHITELISTED),
 *[('G12',f'SELECT {expr}',V.FORBIDDEN_FUNCTION) for expr in ["load_extension('a')",'randomblob(100)',"date('now')",'CURRENT_DATE','CURRENT_TIMESTAMP','CURRENT_TIME',"strftime('%s','now')"]],
 ('G12-date','SELECT julianday(:as_of_date)',None),
 ('G13','SELECT :foo',V.UNKNOWN_BIND_PARAM),('G13','SELECT ?',V.UNKNOWN_BIND_PARAM),
 ('G14','SELECT i.warehouse_id FROM fact_warehouse_inventory_daily i',V.SCOPE_PREDICATE_MISSING),
 ('G14',f'WITH a AS (SELECT i.warehouse_id FROM fact_warehouse_inventory_daily i) SELECT a.warehouse_id FROM a WHERE a.warehouse_id IN :scope_warehouse_ids',V.SCOPE_PREDICATE_MISSING),
 ('G14',Q+' UNION ALL SELECT j.warehouse_id FROM fact_warehouse_inventory_daily j',V.SCOPE_PREDICATE_MISSING),
 ('G14','SELECT i.warehouse_id FROM fact_warehouse_inventory_daily i WHERE EXISTS ('+Q.replace(' i ', ' j ').replace('i.','j.')+')',V.SCOPE_PREDICATE_MISSING),
 ('G15',Q+' OR 1=1',V.SCOPE_PREDICATE_WEAKENED),
 ('G15',Q.replace(P,f'NOT ({P})'),V.SCOPE_PREDICATE_WEAKENED),
 ('G15',Q.replace(P,f'CASE WHEN {P} THEN 1 ELSE 0 END'),V.SCOPE_PREDICATE_WEAKENED),
 ('G16',Q+" AND i.warehouse_id='WH_E01'",None),
 *[('G17',Q+' AND '+p,V.SCOPE_LITERAL_OUT_OF_SCOPE) for p in ["i.warehouse_id='WH_S01'","i.warehouse_id IN ('WH_E01','WH_S01')","i.warehouse_id LIKE 'WH_%'","lower(i.warehouse_id)='wh_e01'","i.region_id='R_SOUTH'"]],
 ('G18',Q.replace(P,"i.warehouse_id='WH_E01'"),V.SCOPE_PREDICATE_MISSING),
 ('G19','SELECT material_id FROM dim_material',None),
 ('G22',Q+' /* ; DROP TABLE x */',None),('G22',Q+' -- DELETE\n',None),
 ('G23','SELECT FROM WHERE',V.PARSE_ERROR),('G23','',V.EMPTY_STATEMENT),('G23',';',V.EMPTY_STATEMENT),
 ('limit-expr',Q+' LIMIT :as_of_date',V.PARSE_ERROR),('negative-limit',Q+' LIMIT -1',V.PARSE_ERROR),
 ('scope-join-only',f'SELECT i.warehouse_id FROM fact_warehouse_inventory_daily i JOIN dim_material m ON {P}',V.SCOPE_PREDICATE_MISSING),
 ('nested-collision',f'SELECT i.warehouse_id FROM fact_warehouse_inventory_daily i WHERE EXISTS ({Q})',V.SCOPE_PREDICATE_MISSING),
 ('scope-or-narrow',Q+" AND (i.warehouse_id='WH_E01' OR 1=1)",V.SCOPE_PREDICATE_WEAKENED),
 ('scope-left-leak',f'SELECT i.warehouse_id FROM fact_warehouse_inventory_daily i LEFT JOIN dim_material m ON {P}',V.SCOPE_PREDICATE_MISSING),
]

@pytest.mark.parametrize('case,sql,code',CASES,ids=[c[0]+'-'+str(i) for i,c in enumerate(CASES)])
def test_guard_matrix(case,sql,code):
    result=SqlStaticGuard(SemanticLayer(),max_rows=5).validate(sql,scope=scope_for('wh_sup_e01e02'))
    if code is None:
        assert result.is_valid, result.error_hint
        assert result.normalized_sql.endswith('LIMIT 5')
        assert result.limit_applied==5
        assert '/*' not in result.normalized_sql and '--' not in result.normalized_sql
        if case in ('G1','G2'): assert result.limit_rewritten
        if case=='G3': assert result.referenced_tables==['fact_warehouse_inventory_daily']
    else:
        assert not result.is_valid
        assert code in [v.code for v in result.violations],result.error_hint
        if case=='G4': assert V.MULTI_STATEMENT in [v.code for v in result.violations]
        if result.fatal:
            assert 'supplier_cost_price' not in result.model_dump_json().lower()
            assert 'dim_supplier_contract' not in result.model_dump_json().lower()
        if case=='G10': assert 'loss_qty' in result.error_hint

def test_g20_g21():
    sem=SemanticLayer();guard=SqlStaticGuard(sem,max_rows=10)
    assert guard.validate('SELECT warehouse_id FROM fact_warehouse_inventory_daily',scope=scope_for('hq_analyst')).is_valid
    for level,name in [(ScopeLevel.WAREHOUSE,'wh_sup_e01'),(ScopeLevel.REGION,'mgr_east'),(ScopeLevel.NATIONAL,'hq_analyst')]:
        r=guard.validate(sem.render_alert_sql(ALERT_NEAR_EXPIRY_BATCHES,level),scope=scope_for(name))
        assert r.is_valid,r.error_hint

def test_g24_all_recoverable():
    r=SqlStaticGuard(SemanticLayer(),max_rows=5).validate('SELECT i.loss_id,:foo FROM fact_material_loss i',scope=scope_for('mgr_east'))
    assert len(r.violations)==3
    assert r.error_hint.startswith('[SQL_GUARD_REJECTED] 共 3 处问题')

def test_g25_mask_idempotent():
    s="SELECT :as_of_date, '"+'x'*33+"' AS note"
    masked=mask_sql_for_display(s)
    assert "'***'" in masked and ':as_of_date' in masked
    assert mask_sql_for_display(masked)==masked

def test_parser_diagnostics_do_not_leak(caplog):
    import logging
    guard=SqlStaticGuard(SemanticLayer(),max_rows=10)
    with caplog.at_level(logging.WARNING):
        result=guard.validate('EXPLAIN SELECT supplier_cost_price FROM dim_supplier_contract',scope=scope_for())
    assert not result.is_valid
    assert 'supplier_cost_price' not in caplog.text
    assert 'dim_supplier_contract' not in caplog.text

@pytest.mark.parametrize('expression',['random()','sqlite_version()','date()','julianday()'])
def test_nondeterministic_functions(expression):
    result=SqlStaticGuard(SemanticLayer(),max_rows=10).validate('SELECT '+expression,scope=scope_for())
    assert result.fatal
    assert V.FORBIDDEN_FUNCTION in [v.code for v in result.violations]
