import asyncio
import json
from collections import Counter
from pathlib import Path
import pytest
from data_platform.retrieval_engine.models import SearchRequest
from eval_platform.models import AgentInvocation, EvalScenario
from eval_platform.offline import RuleBasedAgent, RuleBasedJudge, REFUSAL
from eval_platform.ports import AgentSystemPort, LLMJudgePort
from eval_platform.runner import load_eval_suite
from tests.eval_platform.conftest import chunk, sample
from tests.eval_platform.corpus import ROOT
from tests.eval_platform.run_catering import catering_system, export_report


async def test_golden_suite_quality_and_grounding():
    samples=await load_eval_suite(ROOT/'eval_platform/golden_datasets/catering_suite.json')
    assert len(samples)==32
    assert Counter(s.scenario for s in samples).keys()==set(EvalScenario)
    assert sum(s.is_acl_redline for s in samples)==8
    async with catering_system() as service:
        for s in samples:
            response=await service.search(SearchRequest(query=s.question,user_context=s.user_context,top_k=50))
            ids={p.doc_id for p in response.results}
            if s.is_acl_redline:
                assert not ids.intersection(s.forbidden_doc_ids)
            else:
                assert set(s.expected_doc_ids)<=ids
                # Every golden key fact is present in authorized source documents.
                text='\n'.join(p.full_content for p in response.results if p.doc_id in s.expected_doc_ids)
                from eval_platform.evaluators import RetrievalMetricsCalculator
                assert RetrievalMetricsCalculator.keyword_coverage(text,s.expected_keywords)==1,s.sample_id
            if 'page_2' in s.tags:
                assert any(p.doc_id=='sop_equipment' and p.page_number==2 for p in response.results)


async def test_real_catering_comparison_and_artifacts(tmp_path):
    baseline,candidate,diff,e2e=await export_report(tmp_path/'latest_regression_report.md')
    assert baseline.summary.total_samples==candidate.summary.total_samples==32
    assert candidate.summary.mean_mrr>=baseline.summary.mean_mrr
    assert candidate.summary.retrieval_hit_rate>=baseline.summary.retrieval_hit_rate
    assert candidate.summary.acl_redline_blocked==8 and candidate.summary.acl_redline_passed
    assert diff.gate_passed,diff.blocking_reasons
    assert e2e.summary.mean_satisfaction is not None
    assert e2e.summary.mean_faithfulness==1 and e2e.summary.acl_redline_passed
    assert all(r.satisfaction_score is None for r in e2e.results if r.is_acl_redline)
    assert (tmp_path/'candidate_run.json').exists()
    raw=json.loads((tmp_path/'candidate_run.json').read_text())
    assert raw['suite_fingerprint']==candidate.suite_fingerprint
    report=(tmp_path/'latest_regression_report.md').read_text()
    assert '非' in report and '未连接真实 Milvus' in report and '1–5' in report


async def test_rule_judge_exact_scores_and_determinism():
    judge=RuleBasedJudge()
    assert isinstance(judge,LLMJudgePort)
    kwargs=dict(question='操作？',answer='检查阀门。虚构事实。',contexts=[chunk(content='检查阀门。')])
    a,b=await asyncio.gather(judge.score_faithfulness(**kwargs),judge.score_faithfulness(**kwargs))
    assert a==b and a.score==.5
    assert (await judge.score_accuracy(question='?',answer='40g',reference_answer=None,expected_keywords=['40g','25ml'])).score==.5
    assert (await judge.score_faithfulness(question='?',answer=REFUSAL,contexts=[])).score==1
    assert (await judge.score_faithfulness(question='?',answer='事实',contexts=[])).score==0
    for answer,expected in [(REFUSAL,1),('x'*1001,2),('x'*501,3),('简洁答案',4),('检查记录',5)]:
        score=await judge.simulate_satisfaction(question='?',answer=answer,scenario=EvalScenario.STORE_SOP,user_context=sample().user_context)
        assert score.score==expected and score.token_usage.total_tokens==0


async def test_rule_agent_uses_real_acl_without_labels():
    async with catering_system() as service:
        agent=RuleBasedAgent(service)
        assert isinstance(agent,AgentSystemPort)
        s=sample(user_context=dict(user_id='outsider',dept_id='unknown',roles=['clerk']))
        answer=await agent.invoke(AgentInvocation(trace_id='t',scenario=s.scenario,question='给出受限价格',user_context=s.user_context))
        assert answer.refused and answer.answer==REFUSAL and not answer.retrieved_contexts
