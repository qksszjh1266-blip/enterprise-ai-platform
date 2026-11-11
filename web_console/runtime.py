"""工作台运行时：加载知识库、挂载只读沙箱，并把三大智能体装配到真实端口上；模型端口可在离线与在线间切换。"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from agent_matrix.finance_audit.agent import FinanceAuditAgent
from agent_matrix.finance_audit.models import FinanceAuditConfig
from agent_matrix.finance_audit.redaction import PiiRedactor
from agent_matrix.store_sop.agent import StoreSopAgent
from agent_matrix.store_sop.models import StoreAgentConfig
from agent_matrix.store_sop.offline import InMemorySessionStore, InMemoryTicketPort
from agent_matrix.supply_chain_bi.agent import SupplyChainBIAgent
from agent_matrix.supply_chain_bi.models import SupplyChainAgentConfig
from agent_matrix.supply_chain_bi.sandbox_sqlite import SqliteReadOnlySandbox
from data_platform.adapters.memory import InMemoryVectorSearch
from data_platform.parser_chunker.markdown_chunker import HierarchicalMarkdownChunker
from data_platform.retrieval_engine.service import HybridRetrievalService
from data_platform.storage_sync.models import Base, DocumentIngestRequest, KBDocument, KBParentChunk, SyncOutcome
from data_platform.storage_sync.sync_manager import VersionedSyncManager
from web_console.adapters import (
    BigramKeywordSearch, BigramReranker, DemoApprovalGateway, DemoClaimLedger, DemoFinanceModel, DemoScopeResolver,
    DemoSqlModel, DemoStoreChatModel, LocalHashEmbedder, TracingSearch,
)
from web_console.demo_data.supply_seed import seed_supply_chain_database
from web_console.llm_gateway import CallMeter, ChatCompletionsClient, OnlineFinanceModel, OnlineSqlModel, OnlineStoreChatModel
from web_console.llm_settings import LlmSettings

ROOT = Path(__file__).resolve().parents[1]
MOCK_DOCS = ROOT / "mock_docs"
CONTRACT_POLICY = Path(__file__).with_name("demo_data") / "supplier_contract_policy.md"
SIMULATED_TABLES = 9
# 智能体的模型预算略大于网关超时，使超时优先由适配器报告明确原因。
AGENT_TIMEOUT_MARGIN_SECONDS = 2.0


class MemoryIndex:
    """内存子块索引写端口，实现向量与关键词写入协议，读端直接引用 data。"""

    def __init__(self) -> None:
        self.data = {}

    async def upsert_children(self, doc_id, version, children) -> int:
        for child in children:
            if child.doc_id != doc_id or child.version != version:
                raise ValueError("document/version mismatch")
            self.data[child.child_id] = child.model_copy(deep=True)
        return len(children)

    def _drop(self, match) -> int:
        keys = [k for k, c in self.data.items() if match(c)]
        for key in keys:
            del self.data[key]
        return len(keys)

    async def delete_version(self, doc_id, version) -> int:
        return self._drop(lambda c: c.doc_id == doc_id and c.version == version)

    async def delete_stale_versions(self, doc_id, keep_version) -> int:
        return self._drop(lambda c: c.doc_id == doc_id and c.version != keep_version)


def _corpus() -> list[tuple[str, str, str, str, list[str]]]:
    """返回 (doc_id, 标题, 正文, 部门, 角色)，权限划分与评测语料一致。"""
    manual = (MOCK_DOCS / "store_sop_manual.md").read_text(encoding="utf-8")
    spec = manual.split("## 商品规格参数标准", 1)[1].split("<!-- page: 2 -->", 1)[0]
    equipment = manual.split("## 设备巡检与维护", 1)[1].split("<!-- page: 3 -->", 1)[0]
    policy, matrix = manual.split("## 运营费用报销权限", 1)[1].split("### 审批矩阵与数据可见范围", 1)
    staff = ["clerk", "store_manager"]
    docs = [
        ("sop_sku_spec", "商品规格参数标准", "## 商品规格参数标准" + spec, "store_ops", staff),
        ("sop_equipment", "设备巡检与维护", "<!-- page: 2 -->\n## 设备巡检与维护" + equipment, "store_ops", staff),
        ("fin_expense_policy", "运营费用报销权限", "<!-- page: 3 -->\n## 运营费用报销权限" + policy, "store_ops", [*staff, "finance"]),
        ("fin_approval_matrix", "审批矩阵与数据可见范围", "<!-- page: 3 -->\n### 审批矩阵与数据可见范围" + matrix, "store_ops", ["store_manager", "finance"]),
        ("sop_scanner", "扫码枪培训资料", (MOCK_DOCS / "store_sop_scanner_training.md").read_text(encoding="utf-8"), "store_ops", staff),
    ]
    supply = ["warehouse", "supply_manager", "hq_supply_analyst"]
    finance = ["finance", "finance_manager"]
    contract = [*finance, "legal"]
    for path, dept, ids, roles, private in (
        (MOCK_DOCS / "supply_chain_inventory.md", "supply_chain",
         ["sc_receiving", "sc_rotation", "sc_replenishment", "sc_counting", "sc_private"], supply, ["procurement_manager"]),
        (MOCK_DOCS / "finance_expense_policy.md", "finance",
         ["finance_submission", "finance_matrix", "finance_travel", "finance_payment", "finance_private"], finance, ["finance_manager"]),
        (CONTRACT_POLICY, "finance",
         ["contract_payment", "contract_breach", "contract_approval", "contract_private"], contract, ["finance_manager"]),
    ):
        sections = path.read_text(encoding="utf-8").split("\n## ")[1:]
        if len(sections) != len(ids):
            raise ValueError(f"{path.name} 章节数与演示语料配置不一致")
        for index, (doc_id, section) in enumerate(zip(ids, sections)):
            docs.append((doc_id, section.split("\n", 1)[0], "## " + section, dept, private if index == len(ids) - 1 else roles))
    return docs


@dataclass
class ConsoleRuntime:
    workdir: tempfile.TemporaryDirectory
    engine: AsyncEngine
    session_factory: async_sessionmaker
    manager: VersionedSyncManager
    retrieval: HybridRetrievalService
    index: MemoryIndex
    sandbox: SqliteReadOnlySandbox
    session_store: InMemorySessionStore
    ticket_port: InMemoryTicketPort
    scope_resolver: DemoScopeResolver
    ledger: DemoClaimLedger
    approval_gateway: DemoApprovalGateway
    redactor: PiiRedactor
    documents: int
    parents: int
    http: httpx.AsyncClient
    meter: CallMeter = field(default_factory=CallMeter)
    env_settings: LlmSettings | None = None
    web_settings: LlmSettings | None = None
    active_settings: LlmSettings | None = None
    store_agent: StoreSopAgent = field(init=False)
    bi_agent: SupplyChainBIAgent = field(init=False)
    finance_agent: FinanceAuditAgent = field(init=False)

    def apply_llm(self, settings: LlmSettings | None) -> None:
        """按配置重建三大智能体；会话、工单与台账沿用同一实例，进行中的请求继续使用旧智能体完成。"""
        if settings is None:
            store_model, sql_model, finance_model = DemoStoreChatModel(), DemoSqlModel(), DemoFinanceModel()
            store_config = bi_config = finance_config = None
        else:
            client = ChatCompletionsClient(settings, self.http, self.meter)
            store_model, sql_model, finance_model = OnlineStoreChatModel(client), OnlineSqlModel(client), OnlineFinanceModel(client)
            budget = settings.timeout_seconds + AGENT_TIMEOUT_MARGIN_SECONDS
            store_config = StoreAgentConfig(model_timeout_s=budget)
            bi_config = SupplyChainAgentConfig(model_timeout_s=budget)
            finance_config = FinanceAuditConfig(model_timeout_s=budget)
        self.store_agent = StoreSopAgent(chat_model=store_model, retrieval=self.retrieval, session_store=self.session_store,
                                         ticket_port=self.ticket_port, config=store_config)
        self.bi_agent = SupplyChainBIAgent(sql_model=sql_model, sandbox=self.sandbox, scope_resolver=self.scope_resolver, config=bi_config)
        self.finance_agent = FinanceAuditAgent(review_model=finance_model, ledger=self.ledger, approval_gateway=self.approval_gateway,
                                               retrieval=self.retrieval, redactor=self.redactor, config=finance_config)
        self.active_settings = settings
        self.meter.last_error = None

    def stored_settings_for(self, base_url: str) -> LlmSettings | None:
        """返回同一网关地址下已保存的配置；密钥绝不复用到其他地址，防止被转发给非预期的服务。"""
        return next((s for s in (self.web_settings, self.env_settings) if s is not None and s.base_url == base_url), None)

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.manager.aclose()
        await self.engine.dispose()
        self.workdir.cleanup()

    async def ledger_rows(self) -> list[KBDocument]:
        async with self.session_factory() as session:
            return list((await session.scalars(select(KBDocument).order_by(KBDocument.dept_id, KBDocument.doc_id))).all())


async def build_runtime(llm_settings: LlmSettings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> ConsoleRuntime:
    workdir = tempfile.TemporaryDirectory(prefix="enterprise-console-")
    base = Path(workdir.name)
    engine = create_async_engine(f"sqlite+aiosqlite:///{base / 'knowledge.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    embedder, vector_index, keyword_index = LocalHashEmbedder(), MemoryIndex(), MemoryIndex()
    manager = VersionedSyncManager(session_factory=factory, chunker=HierarchicalMarkdownChunker(child_max_chars=120, child_overlap_chars=20),
                                   vector_store=vector_index, keyword_store=keyword_index, embedder=embedder)
    corpus = _corpus()
    for doc_id, title, content, dept, roles in corpus:
        result = await manager.sync_document(DocumentIngestRequest(doc_id=doc_id, title=title, source_system="mock_docs",
                                                                   content=content, dept_id=dept, allowed_roles=roles))
        if result.outcome is not SyncOutcome.PUBLISHED:
            raise RuntimeError(f"演示知识库发布失败：{doc_id}")
    await manager.aclose()
    async with factory() as session:
        parents = await session.scalar(select(func.count()).select_from(KBParentChunk))

    retrieval = HybridRetrievalService(
        session_factory=factory, embedder=embedder,
        vector_search=TracingSearch(InMemoryVectorSearch(vector_index.data), "vector"),
        keyword_search=TracingSearch(BigramKeywordSearch(keyword_index.data), "keyword"), reranker=BigramReranker())

    sandbox = SqliteReadOnlySandbox(await seed_supply_chain_database(base / "supply_chain.sqlite"))
    approval_gateway, redactor = DemoApprovalGateway(), PiiRedactor()
    # 注入传输层时关闭环境代理探测，否则代理挂载会绕过注入的传输层。
    http = httpx.AsyncClient(transport=transport, trust_env=transport is None, follow_redirects=False)
    runtime = ConsoleRuntime(
        workdir=workdir, engine=engine, session_factory=factory, manager=manager, retrieval=retrieval, index=keyword_index,
        sandbox=sandbox, session_store=InMemorySessionStore(), ticket_port=InMemoryTicketPort(), scope_resolver=DemoScopeResolver(),
        ledger=DemoClaimLedger(), approval_gateway=approval_gateway, redactor=redactor, documents=len(corpus), parents=parents or 0,
        http=http, env_settings=llm_settings)
    runtime.apply_llm(llm_settings)
    return runtime
