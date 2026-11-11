import pytest
from agent_matrix.supply_chain_bi.semantic_layer import SemanticLayer, SUPPLY_CHAIN_CATALOG
from agent_matrix.supply_chain_bi.guardrails import SqlStaticGuard
from agent_matrix.supply_chain_bi.errors import SemanticCatalogError
from agent_matrix.supply_chain_bi.models import ScopeLevel
from .sim_db import AS_OF

def test_06_match_and_filter():
    s=SemanticLayer()
    assert [m.metric_id for m in s.match_metrics('近30天益生菌粉报损率')]==['material_loss_rate']
    assert [m.metric_id for m in s.match_metrics('还能撑几天')]==['days_of_supply']
    assert s.filter_metric_ids(['bad','days_of_supply','days_of_supply'])==['days_of_supply']
    assert s.get_table('DIM_MATERIAL').table_name=='dim_material'
    assert s.get_table('dim_supplier_contract') is None
    assert s.is_sensitive_column('FACT_INVENTORY_BATCH','Supplier_Cost_Price')
    assert s.is_system_catalog('main.SQLITE_MASTER')

@pytest.mark.parametrize('level',list(ScopeLevel))
def test_07_context_public_self_check(level):
    s=SemanticLayer();c=s.build_context(metric_ids=['material_loss_rate'],scope_level=level,as_of_date=AS_OF,max_rows=20)
    for forbidden in SUPPLY_CHAIN_CATALOG.global_sensitive_columns+SUPPLY_CHAIN_CATALOG.forbidden_tables:
        assert forbidden not in c.model_dump_json()
        assert forbidden not in s.public_catalog().model_dump_json()
    assert 'fact_store_sales_daily' not in [t.table_name for t in c.tables]
    s.self_check(SqlStaticGuard(s,max_rows=20))
    p=s.render_scope_predicate('i',level)
    assert (':scope_region_id' in p)==(level is not ScopeLevel.NATIONAL)
    assert (':scope_warehouse_ids' in p)==(level is ScopeLevel.WAREHOUSE)
    assert SUPPLY_CHAIN_CATALOG.global_sensitive_columns!=['***']

def test_07_bad_template():
    tpl=SUPPLY_CHAIN_CATALOG.alert_templates[1]
    bad=tpl.model_copy(update={'sql':tpl.sql.replace('${scope_b}','1=1')})
    catalog=SUPPLY_CHAIN_CATALOG.model_copy(update={'alert_templates':[SUPPLY_CHAIN_CATALOG.alert_templates[0],bad]})
    s=SemanticLayer(catalog)
    with pytest.raises(SemanticCatalogError):s.self_check(SqlStaticGuard(s,max_rows=20))
    bad=tpl.model_copy(update={'expected_columns':['unknown']})
    s=SemanticLayer(SUPPLY_CHAIN_CATALOG.model_copy(update={'alert_templates':[bad]}))
    with pytest.raises(SemanticCatalogError):s.self_check(SqlStaticGuard(s,max_rows=20))
