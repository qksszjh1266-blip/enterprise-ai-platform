"""Reproduce: uv run python -m tests.agent_matrix.store_sop.run_e2e."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from agent_matrix.store_sop.agent import StoreSopAgent
from agent_matrix.store_sop.offline import OfflineChatModel, InMemorySessionStore, InMemoryTicketPort
from data_platform.parser_chunker.markdown_chunker import HierarchicalMarkdownChunker
from data_platform.storage_sync.models import Base
from data_platform.storage_sync.sync_manager import VersionedSyncManager
from data_platform.retrieval_engine.models import SearchRequest
from data_platform.retrieval_engine.service import HybridRetrievalService
from eval_platform.models import EvalMode, EvalRunConfig, GateConfig
from eval_platform.offline import RuleBasedJudge
from eval_platform.ports import AgentSystemPort
from eval_platform.report_generator import MarkdownReportGenerator
from eval_platform.runner import EvalRunner, RegressionGate, load_eval_suite
from tests.fakes import InMemoryVectorStore, InMemoryKeywordStore, FakeEmbedder, InMemoryVectorSearch, InMemoryKeywordSearch, FakeReranker
from tests.eval_platform.corpus import ROOT
from tests.agent_matrix.store_sop.conftest import publish_store_corpus

SUITE = ROOT/'eval_platform/golden_datasets/store_sop_e2e_suite.json'


@asynccontextmanager
async def store_system():
    directory = await asyncio.to_thread(TemporaryDirectory)
    engine = create_async_engine(f'sqlite+aiosqlite:///{Path(directory.name)/"sop.sqlite"}')
    @event.listens_for(engine.sync_engine,'connect')
    def configure(connection,_):
        connection.execute('PRAGMA foreign_keys=ON')
    manager = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        manager = VersionedSyncManager(session_factory=async_sessionmaker(engine,expire_on_commit=False),
            chunker=HierarchicalMarkdownChunker(),vector_store=InMemoryVectorStore(),
            keyword_store=InMemoryKeywordStore(),embedder=FakeEmbedder())
        await publish_store_corpus(manager)
        service = HybridRetrievalService(session_factory=manager.session_factory,embedder=manager.embedder,
            vector_search=InMemoryVectorSearch(manager.vector_store.data),
            keyword_search=InMemoryKeywordSearch(manager.keyword_store.data),reranker=FakeReranker())
        yield service
    finally:
        if manager:
            await manager.aclose()
        await engine.dispose()
        await asyncio.to_thread(directory.cleanup)


def make_agent(service, model=None):
    agent = StoreSopAgent(chat_model=model or OfflineChatModel(),retrieval=service,
        session_store=InMemorySessionStore(),ticket_port=InMemoryTicketPort())
    assert isinstance(agent,AgentSystemPort)
    return agent


async def evaluate(agent,samples,version):
    return await EvalRunner(config=EvalRunConfig(mode=EvalMode.END_TO_END,system_name='StoreSopAgent',
        system_version=version,max_concurrency=4),agent=agent,judge=RuleBasedJudge()).run(
            samples,suite_name='store_sop_e2e_suite',run_id='store-sop-'+version)


def regression_gate():
    # Declared before the experiment: local SQLite timing is not a load benchmark.
    # All quality thresholds and the unwaivable ACL gate remain at evaluation platform defaults.
    return RegressionGate(GateConfig(max_p95_latency_increase=10,max_p95_latency_ms=1000))


class LeakingRetrieval:
    """Deliberately broken retrieval port for the ACL negative control (never used in baseline)."""
    def __init__(self,service,secret):
        self.service,self.secret=service,secret

    async def search(self,request):
        response=await self.service.search(request)
        if request.user_context.dept_id=='supply_chain':
            return response.model_copy(update={'results':[self.secret]})
        return response


async def compare(service,samples,model=None):
    gate=regression_gate()
    baseline=await evaluate(make_agent(service,model),samples,'baseline')
    candidate=await evaluate(make_agent(service,OfflineChatModel(tamper_numbers=True)),samples,'numeric-tamper')
    # Load an actual forbidden parent through an authorized identity, then inject it AFTER retrieval filtering.
    manager=samples[0].user_context.model_copy(update={'roles':['store_manager']})
    response=await service.search(SearchRequest(query='审批矩阵',user_context=manager))
    secret=next(p for p in response.results if p.doc_id=='fin_approval_matrix')
    leaked=await evaluate(make_agent(LeakingRetrieval(service,secret)),samples,'acl-injection')
    return baseline,candidate,gate.compare(baseline,candidate),leaked,gate.compare(baseline,leaked)


async def export_report(output):
    samples=await load_eval_suite(SUITE)
    async with store_system() as service:
        runs=await compare(service,samples)
    baseline,candidate,diff,leaked,leak_diff=runs
    renderer=MarkdownReportGenerator(decimals=2)
    report=(
        '# 门店 SOP 智能体离线运营回归\n\n'
        '这是离线工程验收及故障注入报告，不能替代生产模型、真实搜索引擎或人工业务验收。\n\n'
        '- 复现：`uv run python -m tests.agent_matrix.store_sop.run_e2e`。\n'
        '- 15 条单轮黄金样本：5 条饮品（含 45g/40g 诱捕）、5 条完整设备故障描述、5 条权限红线。'
        '多轮追问、工单和会话并发另由 pytest 验证；追问轮次返回空检索上下文。\n'
        '- 原手册通过统一知识发布链路入库；封口机条款来自 `mock_docs/store_sop_sealer_training.md`，明确为培训演示语料。'
        '真实运行 VersionedSyncManager、SQLite 台账/父块、HybridRetrievalService ACL 前置过滤、双路召回与重排。\n'
        '- `StoreSopAgent` 直接传给 `EvalRunner.agent`，未使用智能体包装层。生成模型为离线规则适配器，'
        '检索端嵌入与索引使用数据中台内存替身；模型不读取黄金答案、标签或期望文档 ID。\n'
        '- 裁判固定使用未修改的 `eval_platform.offline.RuleBasedJudge`；其忠实度以整句字面匹配计分，'
        '未剥离强制 `[来源n]` 标记，也只识别评测中台的一种拒答模板。因此会把带合法引用的回答和门店 SOP 拒答记为低忠实度。'
        '原始评分与绝对下限阻断完整保留，不调低质量阈值，不将其解释为真实模型幻觉率。\n'
        '- 每次离线模型调用固定记 10 输入 + 5 输出 Token，仅验证轨迹累计，不是真实供应商计费；规则裁判 Token 为 0。\n'
        '- 门禁保持评测中台默认质量阈值与 ACL 红线；仅为 SQLite 调度噪声预设 P95 相对增幅上限 1000%、绝对上限 1000ms。\n'
        '- 数值篡改候选预期被引用核验拒答，命中率/准确率回退触发 `METRIC_REGRESSION`；'
        '越权负控故意绕过检索过滤注入真实财务父块，预期触发 `ACL_REDLINE_BREACHED`。'
        '这两项阻断均是验收所需结果，不是发布成功结论。\n\n'
        '## 基线运行（未注入故障）\n\n' + renderer.render(baseline) +
        '\n## 数值幻觉注入候选与门禁\n\n' + renderer.render(candidate,diff) +
        '\n## 越权注入负控与门禁\n\n' + renderer.render(leaked,leak_diff))
    await renderer.write(output,report)
    # Full raw records make gate conclusions and metric provenance reviewable.
    for name,value in [('baseline',baseline),('numeric_candidate',candidate),('numeric_diff',diff),
                       ('acl_candidate',leaked),('acl_diff',leak_diff),('gate_config',regression_gate().config)]:
        await renderer.write(output.parent/f'store_sop_{name}.json',value.model_dump_json(indent=2)+'\n')
    return runs


async def main():
    output=ROOT/'eval_platform/reports/store_sop_agent_report.md'
    baseline,candidate,diff,leaked,leak_diff=await export_report(output)
    print(f'baseline: hit={baseline.summary.retrieval_hit_rate}, accuracy={baseline.summary.answer_accuracy_rate}, '
          f'ACL={baseline.summary.acl_redline_passed}, error_rate={baseline.summary.error_rate}')
    print(f'numeric candidate: hit={candidate.summary.retrieval_hit_rate}, ACL={candidate.summary.acl_redline_passed}, '
          f'gate={[v.code.value for v in diff.violations]}')
    print(f'ACL negative control: gate={[v.code.value for v in leak_diff.violations]}')
    print(f'report={output}')


if __name__=='__main__':
    asyncio.run(main())
