"""离线演示运行时：加载知识库、挂载只读沙箱，并把三大智能体装配到真实端口上。"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from agent_matrix.finance_audit.agent import FinanceAuditAgent
from agent_matrix.finance_audit.redaction import PiiRedactor
from agent_matrix.store_sop.agent import StoreSopAgent
from agent_matrix.store_sop.offline import InMemorySessionStore, InMemoryTicketPort
from agent_matrix.supply_chain_bi.agent import SupplyChainBIAgent
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

ROOT = Path(__file__).resolve().parents[1]
MOCK_DOCS = ROOT / "mock_docs"
CONTRACT_POLICY = Path(__file__).with_name("demo_data") / "franchise_contract_policy.md"
SIMULATED_TABLES = 9


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
    drink = manual.split("## 饮品出品标准", 1)[1].split("<!-- page: 2 -->", 1)[0]
    equipment = manual.split("## 设备巡检与维护", 1)[1].split("<!-- page: 3 -->", 1)[0]
    policy, matrix = manual.split("## 门店费用报销权限", 1)[1].split("### 审批矩阵与数据可见范围", 1)
    staff = ["clerk", "store_manager"]
    docs = [
        ("sop_drink", "饮品出品标准", "## 饮品出品标准" + drink, "store_ops", staff),
        ("sop_equipment", "设备巡检与维护", "<!-- page: 2 -->\n## 设备巡检与维护" + equipment, "store_ops", staff),
        ("fin_expense_policy", "门店费用报销权限", "<!-- page: 3 -->\n## 门店费用报销权限" + policy, "store_ops", [*staff, "finance"]),
        ("fin_approval_matrix", "审批矩阵与数据可见范围", "<!-- page: 3 -->\n### 审批矩阵与数据可见范围" + matrix, "store_ops", ["store_manager", "finance"]),
        ("sop_sealer", "封口机培训资料", (MOCK_DOCS / "store_sop_sealer_training.md").read_text(encoding="utf-8"), "store_ops", staff),
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
    store_agent: StoreSopAgent
    bi_agent: SupplyChainBIAgent
    finance_agent: FinanceAuditAgent
    approval_gateway: DemoApprovalGateway
    redactor: PiiRedactor
    documents: int
    parents: int

    async def aclose(self) -> None:
        await self.manager.aclose()
        await self.engine.dispose()
        self.workdir.cleanup()

    async def ledger_rows(self) -> list[KBDocument]:
        async with self.session_factory() as session:
            return list((await session.scalars(select(KBDocument).order_by(KBDocument.dept_id, KBDocument.doc_id))).all())


async def build_runtime() -> ConsoleRuntime:
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
    return ConsoleRuntime(
        workdir=workdir, engine=engine, session_factory=factory, manager=manager, retrieval=retrieval, index=keyword_index,
        sandbox=sandbox,
        store_agent=StoreSopAgent(chat_model=DemoStoreChatModel(), retrieval=retrieval,
                                  session_store=InMemorySessionStore(), ticket_port=InMemoryTicketPort()),
        bi_agent=SupplyChainBIAgent(sql_model=DemoSqlModel(), sandbox=sandbox, scope_resolver=DemoScopeResolver()),
        finance_agent=FinanceAuditAgent(review_model=DemoFinanceModel(), ledger=DemoClaimLedger(),
                                        approval_gateway=approval_gateway, retrieval=retrieval, redactor=redactor),
        approval_gateway=approval_gateway, redactor=redactor, documents=len(corpus), parents=parents or 0)
