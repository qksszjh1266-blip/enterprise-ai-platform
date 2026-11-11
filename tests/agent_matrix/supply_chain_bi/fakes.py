import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone

from agent_matrix.supply_chain_bi.models import *
from agent_matrix.supply_chain_bi.errors import ScopeDeniedError
from agent_matrix.supply_chain_bi.semantic_layer import SemanticLayer
from agent_matrix.supply_chain_bi.render import format_metric_value, _metric_column
from eval_platform.models import TokenUsage
from data_platform.retrieval_engine.models import UserContext

IDENTITIES = {
    'wh_sup_e01': ('supply_chain',['warehouse'],ScopeLevel.WAREHOUSE,'R_EAST',['WH_E01']),
    'wh_sup_e01e02': ('supply_chain',['warehouse'],ScopeLevel.WAREHOUSE,'R_EAST',['WH_E01','WH_E02']),
    'mgr_east': ('supply_chain',['supply_manager'],ScopeLevel.REGION,'R_EAST',[]),
    'hq_analyst': ('supply_chain',['hq_supply_analyst'],ScopeLevel.NATIONAL,None,[]),
    'proc_mgr': ('supply_chain',['procurement_manager'],ScopeLevel.NATIONAL,None,[]),
    'clerk_01': ('store_ops',['clerk'],None,None,[]),
}

def user(name='mgr_east'):
    dept,roles,*_=IDENTITIES[name]
    return UserContext(user_id=name,dept_id=dept,roles=roles)

def scope_for(name='mgr_east', national=False):
    _,_,level,region,warehouses=IDENTITIES[name]
    if national: level,region,warehouses=ScopeLevel.NATIONAL,None,[]
    return WarehouseScopeFilter(user_id=name,scope_level=level,region_id=region,authorized_warehouse_ids=warehouses,resolved_at=datetime(2026,11,1,tzinfo=timezone.utc))

class StaticScopeResolver:
    def __init__(self, *, fail=None, national=False):
        self.fail,self.national,self.calls=fail,national,[]
    async def resolve(self, user_context):
        self.calls.append(user_context)
        if self.fail: raise self.fail
        identity=IDENTITIES.get(user_context.user_id)
        if not identity or identity[2] is None or user_context.dept_id!=identity[0] or not set(identity[1])<=set(user_context.roles):
            raise ScopeDeniedError('当前身份无数据授权')
        return scope_for(user_context.user_id,self.national)

class SpySandbox:
    def __init__(self, delegate, *, fail=None, delay=0, status=None, alert_fail=False):
        self.delegate,self.fail,self.delay,self.status,self.alert_fail=delegate,fail,delay,status,alert_fail
        self.calls=[]
    @property
    def dialect(self): return self.delegate.dialect
    async def latest_snapshot_date(self): return await self.delegate.latest_snapshot_date()
    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay: await asyncio.sleep(self.delay)
        if self.fail: raise self.fail
        status=self.status
        if self.alert_fail and ('AS available_qty' in kwargs['sql'] or 'AS days_to_expiry' in kwargs['sql']): status=SqlExecutionStatus.TYPE_ERROR
        if status: return SqlExecutionResult(status=status, elapsed_ms=0, error_hint='[TEST_ERROR] 请修正查询')
        return await self.delegate.execute(**kwargs)


def metric_sql(metric, level, question=''):
    sem=SemanticLayer()
    p=lambda alias: sem.render_scope_predicate(alias,level)
    if metric=='material_loss_rate':
        if '格式化' in question:
            return "SELECT 0.0523 AS loss_rate FROM dim_material m WHERE m.material_id = 'MAT_PROBIOTIC'"
        return f"""WITH losses AS (
            SELECT l.warehouse_id, SUM(l.loss_qty) AS loss_qty FROM fact_material_loss l
            WHERE {p('l')} AND l.material_id='MAT_PROBIOTIC' AND l.loss_date BETWEEN date(:as_of_date,'-29 days') AND :as_of_date GROUP BY l.warehouse_id
        ), outbound AS (
            SELECT i.warehouse_id, SUM(i.outbound_qty) AS outbound_qty FROM fact_warehouse_inventory_daily i
            WHERE {p('i')} AND i.material_id='MAT_PROBIOTIC' AND i.snapshot_date BETWEEN date(:as_of_date,'-29 days') AND :as_of_date GROUP BY i.warehouse_id
        ) SELECT l.warehouse_id, 1.0*l.loss_qty/NULLIF(o.outbound_qty+l.loss_qty,0) AS loss_rate
        FROM losses l JOIN outbound o ON o.warehouse_id=l.warehouse_id ORDER BY l.warehouse_id"""
    if metric=='safety_stock_gap':
        narrowing = " AND i.warehouse_id='WH_S01'" if 'WH_S01' in question else " AND i.region_id='R_SOUTH'" if '华南' in question else ''
        return f"SELECT i.warehouse_id, i.material_id, MAX(i.safety_stock_qty-(i.on_hand_qty+i.in_transit_qty-i.reserved_qty),0) AS gap_qty FROM fact_warehouse_inventory_daily i WHERE {p('i')} AND i.snapshot_date=:as_of_date{narrowing} ORDER BY i.warehouse_id, i.material_id"
    if metric=='days_of_supply':
        return f"SELECT i.warehouse_id, (i.on_hand_qty+i.in_transit_qty-i.reserved_qty)/NULLIF(i.avg_daily_outbound_7d,0) AS days_of_supply FROM fact_warehouse_inventory_daily i WHERE {p('i')} AND i.snapshot_date=:as_of_date AND i.material_id='MAT_COFFEE' ORDER BY i.warehouse_id"
    if metric=='shelf_life_turnover_days':
        return f"SELECT b.warehouse_id,b.batch_no,b.remaining_qty/NULLIF(i.avg_daily_outbound_7d,0) AS turnover_days FROM fact_inventory_batch b JOIN fact_warehouse_inventory_daily i ON i.warehouse_id=b.warehouse_id AND i.material_id=b.material_id AND i.snapshot_date=b.snapshot_date WHERE {p('b')} AND {p('i')} AND b.snapshot_date=:as_of_date AND b.material_id='MAT_PROBIOTIC' ORDER BY b.warehouse_id"
    if metric=='near_expiry_qty':
        return f"SELECT b.warehouse_id, SUM(b.remaining_qty) AS near_expiry_qty FROM fact_inventory_batch b WHERE {p('b')} AND b.snapshot_date=:as_of_date AND julianday(b.expiry_date)-julianday(:as_of_date) BETWEEN 0 AND 7 GROUP BY b.warehouse_id ORDER BY b.warehouse_id"
    return f"SELECT s.warehouse_id,SUM(s.cups_sold) AS cups_sold FROM fact_store_sales_daily s WHERE {p('s')} AND s.biz_date BETWEEN date(:as_of_date,'-6 days') AND :as_of_date GROUP BY s.warehouse_id ORDER BY s.warehouse_id"

class ScriptedSqlModel:
    def __init__(self, *, intents=(), candidates=(), narratives=(), fail=None, delay=0, corrupt=False):
        self.intents,self.candidates,self.narratives=deque(intents),deque(candidates),deque(narratives)
        self.fail,self.delay,self.corrupt=fail,delay,corrupt
        self.calls=defaultdict(list)
    async def _start(self, name, kwargs):
        self.calls[name].append(kwargs)
        delay=self.delay.get(name,0) if isinstance(self.delay,dict) else self.delay
        if delay: await asyncio.sleep(delay)
        fail=self.fail.get(name) if isinstance(self.fail,dict) else self.fail
        if fail: raise fail
    def _result(self, output): return SqlModelResult(output=output,token_usage=TokenUsage(prompt_tokens=10,completion_tokens=5))
    async def classify_intent(self, **kwargs):
        await self._start('classify_intent',kwargs)
        if self.intents: return self._result(self.intents.popleft())
        q=kwargs['question']; metrics=SemanticLayer().match_metrics(q)
        metric=metrics[0].metric_id if metrics else 'safety_stock_gap'
        intent=AnalysisIntent(intent_type=AnalysisIntentType.SENSITIVE_REQUEST if '采购底价' in q else AnalysisIntentType.METRIC_QUERY,confidence=1,metric_ids=[metric],asks_national_view='全国' in q,mentioned_region_ids=['R_SOUTH'] if '华南' in q else [],mentioned_warehouse_ids=['WH_S01'] if 'WH_S01' in q else [])
        return self._result(intent)
    async def generate_sql(self, **kwargs):
        await self._start('generate_sql',kwargs)
        return self._candidate(kwargs)
    async def repair_sql(self, **kwargs):
        await self._start('repair_sql',kwargs)
        return self._candidate(kwargs)
    def _candidate(self, kwargs):
        if self.candidates:
            candidate=self.candidates.popleft()
            return self._result(candidate if isinstance(candidate,SqlCandidate) else SqlCandidate(sql=candidate,metric_ids=kwargs['intent'].metric_ids))
        if '危险SQL' in kwargs['question']:
            return self._result(SqlCandidate(sql='DROP TABLE dim_material'))
        metric=kwargs['intent'].metric_ids[0]
        return self._result(SqlCandidate(sql=metric_sql(metric,kwargs['context'].scope_level,kwargs['question']),metric_ids=[metric]))
    async def summarize_analysis(self, **kwargs):
        await self._start('summarize_analysis',kwargs)
        if self.narratives: return self._result(self.narratives.popleft())
        table=kwargs['result']; metric=SemanticLayer().get_metric(kwargs['intent'].metric_ids[0])
        columns=[c.name for c in table.columns]
        index=next((i for i,c in enumerate(columns) if _metric_column(c,metric.metric_id)),len(columns)-1)
        parts=[f"基准日 {kwargs['context'].as_of_date}，授权范围内分析结果："]
        figures=[]
        for ri,row in enumerate(table.rows[:5]):
            value=row[index]
            if not isinstance(value,(int,float)): continue
            value=value*1.5 if self.corrupt else value
            label=' · '.join(str(v) for v in row if isinstance(v,str))
            display='5.2%' if '格式化' in kwargs['question'] and not self.corrupt else format_metric_value(value,metric)
            if display.endswith('%'):
                display=display[:-1].rstrip('0').rstrip('.')+'%'
            parts.append(f'{label} {metric.display_name} {display}。')
            figures.append(KeyFigure(label=metric.display_name,value=value,unit=metric.unit,row_index=ri,column=columns[index]))
        return self._result(AnalysisNarrativeDraft(conclusion='\n'.join(parts),key_figures=figures,follow_up_suggestions=['请按需补货并优先消化临期库存。']))
