"""Acceptance #81–86, including independent defect/precision controls."""
import asyncio
import json
from pathlib import Path
from decimal import Decimal
import pytest
from agent_matrix.finance_audit.agent import FinanceAuditAgent
from agent_matrix.finance_audit.eval_judge import FinanceAuditJudge
from agent_matrix.finance_audit.models import PiiType, ExpenseReasonReviewDraft, ModelRiskClaim, ViolationCode, RiskLevel
from agent_matrix.finance_audit.redaction import PiiRedactor
from agent_matrix.supply_chain_bi.eval_judge import DataAnalysisJudge
from data_platform.retrieval_engine.models import RetrievedParentChunk, RetrievalBranch
from eval_platform.models import EvalRunConfig, EvalMode, GateConfig, GateViolationCode, EvalScenario, FailureCategory
from eval_platform.runner import EvalRunner, RegressionGate, load_eval_suite
from eval_platform.offline import RuleBasedJudge
from eval_platform.report_generator import MarkdownReportGenerator
from tests.agent_matrix.finance_audit.fakes import DATA, ScriptedFinanceModel
from tests.agent_matrix.finance_audit.build_data import ID, PII


async def evaluate(agent,samples,judge=None,version='baseline'):
    return await EvalRunner(agent=agent,judge=judge or FinanceAuditJudge(),config=EvalRunConfig(
        mode=EvalMode.END_TO_END,system_name='FinanceAuditAgent',system_version=version,
        max_concurrency=4,accuracy_threshold=1.0,
        # Prefix keywords are checked strictly by FinanceAuditJudge, not lexical substring coverage.
        keyword_coverage_threshold=0,ranking_mrr_threshold=.2,
    )).run(samples,suite_name='finance-audit',run_id='finance-'+version)


def gate():
    return RegressionGate(GateConfig(max_p95_latency_increase=10,max_p95_latency_ms=3000))


def candidate(agent,**kwargs):
    return FinanceAuditAgent(review_model=kwargs.pop('review_model',ScriptedFinanceModel()),
        ledger=agent.ledger,approval_gateway=agent.approval_gateway,retrieval=agent.retrieval,**kwargs)


async def test_81_suite():
    samples=await load_eval_suite(DATA/'finance_audit_e2e_suite.json')
    assert len(samples)>=16
    assert all(s.scenario is EvalScenario.FINANCE_APPROVAL and s.question.startswith('FIN_AUDIT:') for s in samples)
    from collections import Counter
    routes=Counter(k for s in samples for k in s.expected_keywords if k.startswith('route:'))
    assert routes['route:AUTO_APPROVE']>=3 and routes['route:NEEDS_SUPPLEMENT']>=3
    assert routes['route:ESCALATE_HUMAN']>=4 and routes['route:REJECT']>=1
    assert sum(':contract:' in s.question for s in samples)>=4
    redlines=[s for s in samples if s.is_acl_redline]
    assert len(redlines)>=3
    assert set(next(s for s in redlines if s.sample_id=='PII-E16').forbidden_keywords)==set(PII)


async def test_82_86_e2e_report_and_controls(agent,monkeypatch,tmp_path):
    samples=await load_eval_suite(DATA/'finance_audit_e2e_suite.json')
    baseline=await evaluate(agent,samples)
    assert baseline.summary.acl_redline_passed
    assert baseline.summary.error_rate==0
    assert baseline.summary.retrieval_hit_rate>=.8
    assert baseline.summary.mean_faithfulness>=.9
    assert baseline.summary.answer_accuracy_rate==1.0
    assert all(r.answer_accuracy_score==1 for r in baseline.results if not r.is_acl_redline)
    assert all(r.judge_token_cost==0 for r in baseline.results)
    assert all(not r.failure_category.is_failure for r in baseline.results)
    renderer=MarkdownReportGenerator()
    reports=[renderer.render(baseline,gate().compare(baseline,baseline))]
    legacy=await evaluate(agent,samples,RuleBasedJudge(),version='legacy-judge')
    assert legacy.summary.mean_faithfulness<.5
    reports.append(f'\n## 裁判对照\n\n旧版纯文本规则裁判误判：faithfulness={legacy.summary.mean_faithfulness:.4f}；财务专用裁判={baseline.summary.mean_faithfulness:.4f}。金额采用 Decimal 精确比较；裁判均为离线规则评价，非真人或大模型评价。\n')

    # Missing split detection must remove both the finding and its reconciliation evidence.
    watered=candidate(agent)
    monkeypatch.setattr(watered.rule_engine,'detect_split_claim',lambda *a,**kw:None)
    run=await evaluate(watered,samples,version='split-rule-missing')
    e09=next(r for r in run.results if r.sample_id=='E09')
    assert e09.answer_accuracy_score==0
    diff=gate().compare(baseline,run)
    assert not diff.gate_passed
    assert any(v.code is GateViolationCode.METRIC_REGRESSION and v.metric=='answer_accuracy_rate' for v in diff.violations)
    report=renderer.render(run,diff)
    assert 'E09' in report
    reports.append('\n## 拆单规则放水阻断对照\n\n'+report)

    class MissingIdRedactor(PiiRedactor):
        def __init__(self): super().__init__(enabled_types=frozenset({PiiType.BANK_CARD,PiiType.MOBILE_PHONE}))
        def ensure_clean(self,text,*,sink): pass

    class FindingDetailDisplay(FinanceAuditAgent):
        """Test-only display of findings, to exercise the evaluation answer leak detector.

        Production conclusions never echo rationale; disabling redaction alone cannot
        bypass that independent template defense. Displaying finding details emulates
        the workflow's report rendering, which does expose finding.detail.
        """
        def _to_agent_response(self,outcome):
            result=super()._to_agent_response(outcome)
            return result.model_copy(update={'answer':result.answer+'\n'+'\n'.join(f.detail for f in outcome.response.findings)})

    leaking_model=ScriptedFinanceModel({'E16':ExpenseReasonReviewDraft(reason_matches_categories=True,claims=[
        ModelRiskClaim(code=ViolationCode.PERSONAL_CONSUMPTION_SUSPECTED,risk_level=RiskLevel.HIGH,rationale='原文证据 '+ID)])})
    leaky=FindingDetailDisplay(review_model=leaking_model,ledger=agent.ledger,approval_gateway=agent.approval_gateway,
        retrieval=agent.retrieval,redactor=MissingIdRedactor())
    run=await evaluate(leaky,samples,version='privacy-defect')
    leak=next(r for r in run.results if r.sample_id=='PII-E16')
    assert ID in leak.answer and leak.acl_leaked
    diff=gate().compare(baseline,run)
    assert any(v.code is GateViolationCode.ACL_REDLINE_BREACHED for v in diff.violations)
    reports.append('\n## 隐私泄露阻断对照\n\n故障注入：漏身份证脱敏、关闭出口守卫，并模拟 Workflow 展示 finding.detail。生产 conclusion 模板自身不回显模型 rationale，是额外防线；仅关闭前两道防线不会导致 conclusion 泄露。报告对合成隐私值再次脱敏。\n\n'+renderer.render(run,diff))

    from tests.agent_matrix.finance_audit.conftest import MANAGER
    from data_platform.retrieval_engine.models import SearchRequest
    privileged = await agent.retrieval.search(SearchRequest(query='合同底价保密', user_context=MANAGER, top_k=20))
    private = next(p for p in privileged.results if p.doc_id == 'contract_private')
    class CompromisedRetrieval:
        async def search(self, request):
            result = await agent.retrieval.search(request)
            return result.model_copy(update={'results': [*result.results, private]})
    unauthorized = candidate(agent)
    unauthorized.retrieval = CompromisedRetrieval()
    run = await evaluate(unauthorized, samples, version='acl-context-injection')
    diff = gate().compare(baseline, run)
    assert next(r for r in run.results if r.sample_id == 'ACL-C01').acl_leaked
    assert any(v.code is GateViolationCode.ACL_REDLINE_BREACHED for v in diff.violations)
    reports.append('\n## 越权制度注入阻断对照\n\n'+renderer.render(run,diff))

    faulty=candidate(agent,review_model=ScriptedFinanceModel(fail='unavailable'))
    run=await evaluate(faulty,samples,version='model-outage')
    diff=gate().compare(baseline,run)
    assert any(v.code is GateViolationCode.ERROR_RATE_EXCEEDED and v.metric=='degraded_rate' for v in diff.violations)
    reports.append('\n## 模型故障降级阻断对照\n\n'+renderer.render(run,diff))

    hallucinating=candidate(agent)
    render=hallucinating._render_conclusion
    def wrong_amount(**kwargs):
        answer=render(**kwargs)
        return '\n'.join('定档金额：¥999,999.99' if line.startswith('定档金额：') else line for line in answer.splitlines())
    monkeypatch.setattr(hallucinating,'_render_conclusion',wrong_amount)
    run=await evaluate(hallucinating,samples,version='amount-hallucination')
    diff=gate().compare(baseline,run)
    assert any(v.code is GateViolationCode.METRIC_REGRESSION and v.metric=='mean_faithfulness' for v in diff.violations)
    reports.append('\n## 金额幻觉阻断对照\n\n'+renderer.render(run,diff))

    report=PiiRedactor().redact_text('\n'.join(reports)).text
    report+='\n\n规格说明：E03 前两行小计 0.50 元，第三行含税 14.13 元，整单申报 14.63 元；INV-PAID 使用符合发票格式约束的 INVPAID01。结构化关键词由 FinanceAuditJudge 校验（accuracy_threshold=1）；通用纯文本关键词归因阈值为 0，原始 keyword_coverage 指标照常展示。\n'
    await renderer.write(tmp_path/'finance_audit_e2e_report.md',report)
    assert (await asyncio.to_thread((tmp_path/'finance_audit_e2e_report.md').read_text))==report


async def test_83_exact_money_vs_bi_tolerance():
    def parent(text):
        return RetrievedParentChunk(doc_id='evidence',parent_id='p',version=1,full_content=text,score=1,rrf_score=0,source_branches=[RetrievalBranch.KEYWORD])
    bi=parent('[BI_RESULT_V1]\ncolumns: amount\nrow: 3200.004')
    fin=parent('FIN_RULE_V1\ndeclared_total: 3200.004')
    a=await DataAnalysisJudge().score_faithfulness(question='',answer='合计3200.00元',contexts=[bi])
    b=await FinanceAuditJudge().score_faithfulness(question='',answer='合计3200.00元',contexts=[fin])
    assert a.score==1 and b.score==0
    # Opposite direction must not round away an invented fraction of a cent either.
    c=await FinanceAuditJudge().score_faithfulness(question='',answer='合计3200.004元',contexts=[parent('FIN_RULE_V1\ndeclared_total: 3200.00')])
    assert c.score==0
