"""Reproduce the offline architectural comparison: uv run python -m tests.eval_platform.run_ecommerce."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from data_platform.parser_chunker.markdown_chunker import HierarchicalMarkdownChunker
from data_platform.storage_sync.models import Base
from data_platform.storage_sync.sync_manager import VersionedSyncManager
from data_platform.retrieval_engine.models import RetrievalConfig
from data_platform.retrieval_engine.service import HybridRetrievalService
from eval_platform.models import EvalRunConfig, EvalMode, GateConfig
from eval_platform.offline import RuleBasedAgent, RuleBasedJudge
from eval_platform.ports import RetrievalSystemPort
from eval_platform.report_generator import MarkdownReportGenerator
from eval_platform.runner import EvalRunner, RegressionGate, load_eval_suite
from tests.fakes import InMemoryVectorStore, InMemoryKeywordStore, FakeEmbedder, InMemoryVectorSearch, InMemoryKeywordSearch, FakeReranker
from tests.eval_platform.corpus import ROOT, publish_corpus


@asynccontextmanager
async def ecommerce_system():
    directory = await asyncio.to_thread(TemporaryDirectory)
    engine = create_async_engine(f'sqlite+aiosqlite:///{Path(directory.name)/"ecommerce.sqlite"}')
    @event.listens_for(engine.sync_engine, 'connect')
    def configure(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
    manager = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        manager = VersionedSyncManager(session_factory=factory, chunker=HierarchicalMarkdownChunker(),
            vector_store=InMemoryVectorStore(), keyword_store=InMemoryKeywordStore(), embedder=FakeEmbedder())
        await publish_corpus(manager, extended=True)
        service = HybridRetrievalService(session_factory=factory, embedder=manager.embedder,
            vector_search=InMemoryVectorSearch(manager.vector_store.data),
            keyword_search=InMemoryKeywordSearch(manager.keyword_store.data), reranker=FakeReranker(),
            config=RetrievalConfig())
        assert isinstance(service, RetrievalSystemPort)
        yield service
    finally:
        if manager is not None:
            await manager.aclose()
        await engine.dispose()
        await asyncio.to_thread(directory.cleanup)


async def compare_ecommerce():
    samples = await load_eval_suite(ROOT/'eval_platform/golden_datasets/ecommerce_suite.json')
    async with ecommerce_system() as service:
        common = dict(system_name='HybridRetrievalService', top_k=3, max_concurrency=4)
        # Warm up SQLite and adapters equally. All measured runs use the same published corpus.
        await EvalRunner(config=EvalRunConfig(**common,system_version='warmup'),retrieval=service).run(samples,suite_name='warmup')
        baseline = await EvalRunner(config=EvalRunConfig(**common,system_version='hybrid-rrf-no-rerank',enable_rerank=False),
            retrieval=service).run(samples,suite_name='ecommerce_suite',run_id='ecommerce-baseline')
        candidate = await EvalRunner(config=EvalRunConfig(**common,system_version='hybrid-rrf-rerank',enable_rerank=True),
            retrieval=service).run(samples,suite_name='ecommerce_suite',run_id='ecommerce-candidate')
        e2e = await EvalRunner(config=EvalRunConfig(**common,system_version='offline-extractive-rules',mode=EvalMode.END_TO_END),
            agent=RuleBasedAgent(service,top_k=3),judge=RuleBasedJudge()).run(samples,suite_name='ecommerce_suite',run_id='ecommerce-rules')
    # Predeclared local experiment budget, not a production latency acceptance criterion.
    gate = RegressionGate(GateConfig(max_p95_latency_increase=10,max_p95_latency_ms=1000))
    return baseline, candidate, gate.compare(baseline,candidate), e2e, gate.config


async def export_report(output: Path):
    baseline,candidate,diff,e2e,gate_config = await compare_ecommerce()
    renderer = MarkdownReportGenerator(decimals=2)
    methodology = (
        '\n## 实验口径与复现\n\n'
        '- 复现命令：`uv run python -m tests.eval_platform.run_ecommerce`。\n'
        '- 语料：三份虚构电商制度（自营商品与仓配运营手册、仓储作业规范、财务报销制度），按权限边界拆为14篇文档；38条黄金样本，28条业务问答、10条红线。\n'
        '- 真实执行数据中台切分、VersionedSyncManager 发布、SQLite 台账与父块回表、ACL 双重过滤、双路召回、RRF 和精排编排。'
        'HybridRetrievalService 直接传给 EvalRunner.retrieval，无适配器。\n'
        '- Baseline：双路混合检索、关闭精排；Candidate：相同双路和语料、开启精排。Top K=3，评测并发=4，先预热。\n'
        '- 索引、嵌入与精排复用数据中台内存替身：SHA-256 假向量、字符词频匹配、规则精排；未连接真实 Milvus、Elasticsearch 或 LLM。'
        '结果仅衡量这套离线架构实验，不代表线上模型收益或百万单负载性能。\n'
        '- Hit/Recall/MRR 按返回父块的首次出现顺序折叠为文档排名；检索端返回上限仍是3个父块。红线不参与检索均值。\n'
        '- 门禁质量与安全阈值保持默认；本地延迟预先设为相对增幅上限1000%、绝对P95上限1000ms，'
        '用于排除SQLite微基准调度噪声；耗时为实测值，每次运行可能变化。\n'
        '- Token 为实际模型调用计数：纯检索和规则裁判均未调用模型，故为0；Token分账另有非零注入测试验证。\n'
        '- 同目录 baseline_run.json、candidate_run.json、regression_diff.json、rule_e2e_run.json 保存逐样本原始结果；gate_config.json 保存门禁阈值。\n'
        '\n## 离线端到端规则评分（补充实验）\n\n'
        '以下回答由可见上下文抽取，裁判采用逐句证据匹配、关键词覆盖和1–5可执行性规则，未读取黄金文档ID生成回答。'
        '这些分数用于验证评分流水线，不是人工满意度或真实LLM准确率。\n\n'
    )
    report = renderer.render(candidate,diff) + methodology
    report += renderer._render_kpi_dashboard(e2e.summary,None) + '\n\n'
    report += renderer._render_failure_distribution(e2e.summary) + '\n\n'
    report += renderer._render_bad_cases(e2e,None) + '\n'
    await renderer.write(output,report)
    for name, model in [('baseline_run',baseline),('candidate_run',candidate),('regression_diff',diff),
                        ('rule_e2e_run',e2e),('gate_config',gate_config)]:
        await renderer.write(output.parent/f'{name}.json',model.model_dump_json(indent=2)+'\n')
    return baseline,candidate,diff,e2e


async def main():
    output=ROOT/'eval_platform/reports/latest_regression_report.md'
    baseline,candidate,diff,_=await export_report(output)
    print(f'baseline hit={baseline.summary.retrieval_hit_rate:.4f} mrr={baseline.summary.mean_mrr:.4f}')
    print(f'candidate hit={candidate.summary.retrieval_hit_rate:.4f} mrr={candidate.summary.mean_mrr:.4f} ACL={candidate.summary.acl_block_rate:.4f}')
    print(f'gate_passed={diff.gate_passed}; report={output}')
    if diff.violations:
        print('\n'.join(diff.blocking_reasons))


if __name__ == '__main__':
    asyncio.run(main())
