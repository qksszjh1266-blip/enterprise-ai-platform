import pytest
from agent_matrix.supply_chain_bi.render import *
from agent_matrix.supply_chain_bi.models import ResultColumn, MetricUnit, AnalysisNarrativeDraft, KeyFigure
from agent_matrix.supply_chain_bi.semantic_layer import MATERIAL_LOSS_RATE
from agent_matrix.supply_chain_bi.eval_judge import DataAnalysisJudge
from .sim_db import AS_OF

@pytest.mark.parametrize('text,expected',[
 ('2026-11-01 WH_E01 MAT_LEMON B_LEMON_1027 5.2%',[(5.2,1,True)]),
 ('2026年11月1日 损耗率5.00%，数量1,234.5',[(5,2,True),(1234.5,1,False)]),
 ('剩余 -1 天 0.0523', [(-1,0,False),(.0523,4,False)]),
])
def test_numeric_extraction(text,expected):assert extract_numeric_tokens(text)==expected

def test_rounding_tolerance():
    assert numeric_match((5.2,1,True),.0523)
    assert not numeric_match((7.5,1,True),.05)
    assert not numeric_match((float('nan'),2,False),1)
    assert format_metric_value(.05,MATERIAL_LOSS_RATE)=='5.00%'
    assert format_metric_value(None,MATERIAL_LOSS_RATE)=='—'

def test_context_round_trip():
    values=['a | b','line\nnext',r'back\slash',None,.0523]
    table=ResultTable(columns=[ResultColumn(name=f'col{i}') for i in range(len(values))],rows=[values],total_row_count=1,displayed_row_count=1)
    text=render_result_context(catalog_version=1,metric_ids=['material_loss_rate'],as_of_date=AS_OF,scope_summary='全国',final_sql='SELECT 1 LIMIT 1',table=table)
    assert parse_result_context(text)==([f'col{i}' for i in range(len(values))],[['a | b','line\nnext',r'back\slash','NULL','0.0523']])
    assert parse_result_context('invalid') is None
    assert parse_result_context('[BI_RESULT_V1]\nrow: broken') is None

async def test_judge_accuracy_and_satisfaction():
    judge=DataAnalysisJudge()
    assert (await judge.score_accuracy(question='损耗率',answer='柠檬 5.0%',reference_answer=None,expected_keywords=['柠檬','5%'])).score==1
    assert (await judge.score_accuracy(question='',answer='5.0%',reference_answer=None,expected_keywords=['7.5%'])).score==0
    with pytest.raises(ValueError):await judge.score_accuracy(question='',answer='hello',reference_answer=None,expected_keywords=[])

async def test_invalid_narrative_fields_and_followups(make_agent):
    agent=make_agent()
    table=ResultTable(columns=[ResultColumn(name='loss_rate')],rows=[[.05]],total_row_count=1,displayed_row_count=1)
    good=KeyFigure(label='rate',value=.05,unit=MetricUnit.RATIO,row_index=0,column='loss_rate')
    cases=[AnalysisNarrativeDraft(conclusion='损耗率 5%',key_figures=[good],follow_up_suggestions=['补货 999kg']),AnalysisNarrativeDraft(conclusion='invented_cost 为 5%',key_figures=[good]),AnalysisNarrativeDraft(conclusion='损耗率 5%',key_figures=[good.model_copy(update={'row_index':9})])]
    for draft in cases:assert agent._verify_numeric_consistency(draft,table,[MATERIAL_LOSS_RATE],[],AS_OF)

async def test_accuracy_reference_rounding_tolerance():
    judge=DataAnalysisJudge()
    result=await judge.score_accuracy(question='损耗率',answer='损耗率5.23%',reference_answer=None,expected_keywords=['5.2%'])
    assert result.score==1
    result=await judge.score_accuracy(question='损耗率',answer='损耗率7.5%',reference_answer=None,expected_keywords=['5.2%'])
    assert result.score==0
