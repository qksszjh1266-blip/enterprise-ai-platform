"""Build documents through the data platform publication path."""
import asyncio
from pathlib import Path
from data_platform.storage_sync.models import DocumentIngestRequest, SyncOutcome

ROOT = Path(__file__).resolve().parents[2]


def split_manual(text):
    # Preserve source page markers when promoting sections to separate documents.
    spec = text.split('## 商品规格参数标准', 1)[1].split('<!-- page: 2 -->', 1)[0]
    equipment = text.split('## 设备巡检与维护', 1)[1].split('<!-- page: 3 -->', 1)[0]
    policy, matrix = text.split('## 运营费用报销权限', 1)[1].split('### 审批矩阵与数据可见范围', 1)
    return [
        ('sop_sku_spec', '## 商品规格参数标准'+spec, 'store_ops', ['clerk','store_manager']),
        ('sop_equipment', '<!-- page: 2 -->\n## 设备巡检与维护'+equipment, 'store_ops', ['clerk','store_manager']),
        ('fin_expense_policy', '<!-- page: 3 -->\n## 运营费用报销权限'+policy, 'store_ops', ['clerk','store_manager','finance']),
        ('fin_approval_matrix', '<!-- page: 3 -->\n### 审批矩阵与数据可见范围'+matrix, 'store_ops', ['store_manager','finance']),
    ]


async def publish_corpus(manager, *, extended=False):
    manual = await asyncio.to_thread((ROOT/'mock_docs/store_sop_manual.md').read_text, encoding='utf-8')
    documents = split_manual(manual)
    if extended:
        for filename, dept, ids, roles, private_roles in [
            ('supply_chain_inventory.md', 'supply_chain', ['sc_receiving','sc_rotation','sc_replenishment','sc_counting','sc_private'], ['warehouse','supply_manager'], ['procurement_manager']),
            ('finance_expense_policy.md', 'finance', ['finance_submission','finance_matrix','finance_travel','finance_payment','finance_private'], ['finance','finance_manager'], ['finance_manager']),
        ]:
            text = await asyncio.to_thread((ROOT/'mock_docs'/filename).read_text, encoding='utf-8')
            sections = text.split('\n## ')[1:]
            assert len(sections) == len(ids)
            documents.extend((doc, '## '+section, dept, private_roles if i == len(ids)-1 else roles)
                for i,(doc, section) in enumerate(zip(ids, sections)))
    for doc_id, content, dept, roles in documents:
        response = await manager.sync_document(DocumentIngestRequest(doc_id=doc_id,title=doc_id,
            content=content,source_system='oa',dept_id=dept,allowed_roles=roles))
        assert response.outcome == SyncOutcome.PUBLISHED, response.error_hint
    await manager.aclose()
    return documents
