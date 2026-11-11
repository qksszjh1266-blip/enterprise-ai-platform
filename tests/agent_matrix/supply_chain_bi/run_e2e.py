"""Reproducible offline supply chain BI evaluation; writes only to the requested output directory.

Run: PYTHONDONTWRITEBYTECODE=1 uv run python -m tests.agent_matrix.supply_chain_bi.run_e2e
"""
import asyncio
import json
import tempfile
from pathlib import Path
from agent_matrix.supply_chain_bi.agent import SupplyChainBIAgent
from agent_matrix.supply_chain_bi.eval_judge import DataAnalysisJudge
from agent_matrix.supply_chain_bi.sandbox_sqlite import SqliteReadOnlySandbox
from eval_platform.models import EvalSample, EvalRunConfig, EvalMode, GateConfig
from eval_platform.runner import EvalRunner, RegressionGate
from eval_platform.offline import RuleBasedJudge
from eval_platform.report_generator import MarkdownReportGenerator
from .fakes import ScriptedSqlModel, StaticScopeResolver
from .sim_db import seed_database

DATA = Path(__file__).parent/'data'

async def load_samples():
    text=await asyncio.to_thread((DATA/'supply_chain_bi_e2e_suite.json').read_text)
    return [EvalSample.model_validate(s) for s in json.loads(text)]

async def run_experiments(db_path):
    samples=await load_samples()
    sandbox=SqliteReadOnlySandbox(db_path)
    async def run(name, *, model=None, resolver=None, judge=None):
        agent=SupplyChainBIAgent(sql_model=model or ScriptedSqlModel(),sandbox=sandbox,scope_resolver=resolver or StaticScopeResolver())
        config=EvalRunConfig(mode=EvalMode.END_TO_END,system_name='supply_chain_bi',system_version=name,max_concurrency=4)
        return await EvalRunner(config=config,agent=agent,judge=judge or DataAnalysisJudge()).run(samples,suite_name='supply_chain_bi_e2e',run_id=name)
    baseline=await run('baseline')
    old=await run('old_rule',judge=RuleBasedJudge())
    numeric=await run('numeric_candidate',model=ScriptedSqlModel(corrupt=True))
    acl=await run('acl_candidate',resolver=StaticScopeResolver(national=True))
    gate=RegressionGate(GateConfig(max_p95_latency_increase=10))
    return baseline,old,numeric,gate.compare(baseline,numeric),acl,gate.compare(baseline,acl)

async def export_report(output: Path):
    with tempfile.TemporaryDirectory(prefix='supply-chain-bi-') as tmp:
        path=await seed_database(Path(tmp)/'sc_sim.db')
        baseline,old,numeric,numeric_diff,acl,acl_diff=await run_experiments(path)
    renderer=MarkdownReportGenerator()
    report=(
        '# 供应链 BI 端到端验收\n\n'
        '基准日：2026-11-01。使用真实 aiosqlite 文件库、连接层只读保护和 AST 守卫；'
        '模型与权限服务使用确定性测试替身，未连接生产模型或组织权限系统。裁判为离线规则裁判，非真人 / LLM 评价。\n\n'
        '## 裁判对照实验\n\n'
        f'同一黄金集与确定性 baseline：RuleBasedJudge mean_faithfulness = {old.summary.mean_faithfulness:.4f}；'
        f'DataAnalysisJudge = {baseline.summary.mean_faithfulness:.4f}。\n\n'
        '格式化样本 loss_format 的原始结果为 0.0523，回答为 5.2%。旧版纯文本规则裁判误判；'
        'DataAnalysisJudge 使用原始单元格与按显示精度缩放的百分比容差，准确判分。\n\n'
        '## 基线运行\n\n'+renderer.render(baseline)+'\n\n'
        '## 数值幻觉注入：结论数字乘以 1.5\n\n'
        '智能体核验失败后回退模板结论；忠实度维持，degraded_rate 超阈值触发 ERROR_RATE_EXCEEDED。\n\n'
        +renderer.render(numeric,numeric_diff)+'\n\n'
        '## 权限缺陷注入：resolver 错误返回全国范围\n\n'
        '模拟权限系统错误放宽范围，结果上下文暴露未授权仓库；ACL_REDLINE_BREACHED 阻断。\n\n'
        +renderer.render(acl,acl_diff)
    )
    await renderer.write(output,report)
    for name,value in [('baseline',baseline),('old_rule',old),('numeric_candidate',numeric),('numeric_diff',numeric_diff),('acl_candidate',acl),('acl_diff',acl_diff)]:
        await renderer.write(output.parent/f'supply_chain_bi_{name}.json',value.model_dump_json(indent=2)+'\n')
    return baseline,old,numeric,numeric_diff,acl,acl_diff

async def main():
    output=DATA/'supply_chain_bi_report.md'
    baseline,old,numeric,numeric_diff,acl,acl_diff=await export_report(output)
    print(f'baseline: faithfulness={baseline.summary.mean_faithfulness}, hit={baseline.summary.retrieval_hit_rate}, errors={baseline.summary.error_rate}, acl={baseline.summary.acl_redline_passed}')
    print(f'old_rule: faithfulness={old.summary.mean_faithfulness}')
    print(f'numeric: degraded={numeric.summary.degraded_rate}, gate={numeric_diff.gate_passed}')
    print(f'acl: redline={acl.summary.acl_redline_passed}, gate={acl_diff.gate_passed}')
    print(f'report={output}')

if __name__=='__main__':asyncio.run(main())
