"""本地零配置演示适配器。

全部实现现有端口协议，只用确定性规则与本地知识库：无外部调用、无随机性、无需任何密钥。
演示模型不是生产大模型，其 token 用量为固定记账样本，仅用于验证轨迹累计。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from agent_matrix.finance_audit.models import (
    CitationSupportDraft, CitationSupportVerdict, ContractClauseReviewDraft, ContractTermExtraction,
    ExpenseReasonReviewDraft, FinanceModelResult, HistoricalClaimRecord, HistoricalContractRecord,
    HumanApprovalTicketDraft, HumanApprovalTicketReceipt, InvoiceUsageRecord, ModelRiskClaim, RiskLevel, ViolationCode,
)
from agent_matrix.finance_audit.rule_engine import _invoice_key
from agent_matrix.store_sop.models import GroundedAnswerDraft
from agent_matrix.store_sop.offline import OfflineChatModel
from agent_matrix.supply_chain_bi.errors import ScopeDeniedError
from agent_matrix.supply_chain_bi.models import (
    AnalysisIntent, AnalysisIntentType, AnalysisNarrativeDraft, KeyFigure, ScopeLevel, SqlCandidate, SqlModelResult,
    WarehouseScopeFilter,
)
from agent_matrix.supply_chain_bi.render import _metric_column, format_metric_value
from agent_matrix.supply_chain_bi.semantic_layer import SemanticLayer
from data_platform.adapters.memory import InMemoryKeywordSearch, InMemoryReranker
from data_platform.retrieval_engine.models import RetrievalBranch, RetrievalCandidate, RetrievalFilter, UserContext
from eval_platform.models import TokenUsage

DEMO_MODEL_NAME = "local-deterministic-demo-v1"
_FIXTURE_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


# ---------------------------------------------------------------------------
# 检索：字符二元组特征（向量 / 关键词 / 精排），并记录真实召回轨迹
# ---------------------------------------------------------------------------


def _bigrams(text: str) -> Counter[str]:
    chars = [c for c in text.lower() if c.isalnum()]
    return Counter(a + b for a, b in zip(chars, chars[1:])) or Counter(chars)


class LocalHashEmbedder:
    """字符二元组哈希向量（单位化）；同义改写弱于真实语义模型，但结果稳定且完全离线。"""

    dimension = 256

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for gram, count in _bigrams(text).items():
            slot = int.from_bytes(hashlib.sha256(gram.encode()).digest()[:4], "big") % self.dimension
            vector[slot] += 1.0 + math.log(count)
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


class BigramKeywordSearch(InMemoryKeywordSearch):
    """按字符二元组词频打分的本地关键词召回，分值为 0 的子块不进入候选。"""

    async def search(self, query: str, retrieval_filter: RetrievalFilter, top_k: int) -> list[RetrievalCandidate]:
        grams = _bigrams(query)

        def score(child) -> float:
            text = _bigrams(child.child_text)
            return float(sum(min(count, text[gram]) for gram, count in grams.items()))

        found = self._search(retrieval_filter, top_k, score, RetrievalBranch.KEYWORD)
        return [c for c in found if c.raw_score > 0]


class BigramReranker(InMemoryReranker):
    """Dice 系数精排：长度归一，避免长章节仅凭字数占优。"""

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        grams = _bigrams(query)
        scores = []
        for passage in passages:
            text = _bigrams(passage)
            overlap = sum(min(count, text[gram]) for gram, count in grams.items())
            scores.append(2 * overlap / (sum(grams.values()) + min(sum(text.values()), 4 * sum(grams.values())) or 1))
        return scores


search_trace: ContextVar[dict[str, Any] | None] = ContextVar("console_search_trace", default=None)


class TracingSearch:
    """透明记录单路召回的候选与 ACL 过滤条件，供引擎透视抽屉展示。"""

    def __init__(self, inner, branch: str) -> None:
        self.inner, self.branch = inner, branch

    async def search(self, query, retrieval_filter, top_k):
        result = await self.inner.search(query, retrieval_filter, top_k)
        trace = search_trace.get()
        if trace is not None:
            trace[self.branch] = result
            trace["filter"] = retrieval_filter
        return result


# ---------------------------------------------------------------------------
# 门店 SOP
# ---------------------------------------------------------------------------


class DemoStoreChatModel(OfflineChatModel):
    """复用离线规则抽取，把命中的表格行改写为可读回答（数字与单位逐字保留，以通过引用核验）。"""

    def _answer(self, text: str) -> GroundedAnswerDraft:
        if not self._has_evidence(text):
            return GroundedAnswerDraft(answer="手册中未找到相关规定", insufficient_evidence=True)
        draft = super()._answer(text)
        lines = text.splitlines()
        headers = {}
        for index, line in enumerate(lines[:-1]):
            if line.startswith("|") and lines[index + 1].startswith("| ---"):
                for row in lines[index + 2:]:
                    if not row.startswith("|"):
                        break
                    headers[row] = line
        return draft.model_copy(update={"answer": "\n".join(self._readable(line, headers) for line in draft.answer.split("\n"))})

    @staticmethod
    def _has_evidence(text: str) -> bool:
        """询问对象未出现在可见资料中、或问题与资料几无重合时视为无依据，避免答非所问。"""
        evidence = "\n".join(re.findall(r"\[来源\d+\] 章节：([^\n]+\n.*?)(?=\n\n\[来源|\n已知槽位：|\Z)", text, re.S))
        known = re.search(r"已知槽位：([^\n]+)", text)
        slots = json.loads(known.group(1)) if known else {}
        subject = slots.get("drink_name") or slots.get("equipment_model")
        if subject:
            return subject in evidence
        question = _bigrams(text.rsplit("用户问题：", 1)[-1].split("\n", 1)[0])
        return sum(min(count, _bigrams(evidence)[gram]) for gram, count in question.items()) >= 2

    @staticmethod
    def _readable(line: str, headers: dict[str, str]) -> str:
        match = re.fullmatch(r"(\|.*\|) (\[来源\d+\])", line)
        if not match or match.group(1) not in headers:
            return line
        names = [c.strip() for c in headers[match.group(1)].strip("|").split("|")]
        cells = [c.strip() for c in match.group(1).strip("|").split("|")]
        return "；".join(f"{n}：{c}" for n, c in zip(names, cells)) + " " + match.group(2)


# ---------------------------------------------------------------------------
# 供应链 BI
# ---------------------------------------------------------------------------

SCOPE_TABLE: dict[str, tuple[ScopeLevel, str | None, list[str]]] = {
    "hq_analyst": (ScopeLevel.NATIONAL, None, []),
    "mgr_east": (ScopeLevel.REGION, "R_EAST", []),
    "wh_sup_e01": (ScopeLevel.WAREHOUSE, "R_EAST", ["WH_E01"]),
}
REGION_NAMES = {"华东": "R_EAST", "华南": "R_SOUTH", "华北": "R_NORTH", "西南": "R_WEST"}
_SQL_ATTACK = re.compile(r"\b(?:select|with|drop|update|delete|insert|alter|attach|pragma|truncate|create)\b[\s\S]*", re.I)
_SQL_WRITE = re.compile(r"\b(?:drop|update|delete|insert|alter|attach|pragma|truncate|create)\b|;\s*\S|\bfrom\b", re.I)
_SENSITIVE_TERMS = ("采购底价", "合同价", "进货价", "供应商报价", "返利", "毛利底表", "加盟商手机")
_OUT_OF_SCOPE_TERMS = ("报销", "薪酬", "配方", "制冰机", "天气")


class DemoScopeResolver:
    """静态组织权限表；不在表内的身份一律无数据授权（fail-closed）。"""

    async def resolve(self, user_context: UserContext) -> WarehouseScopeFilter:
        entry = SCOPE_TABLE.get(user_context.user_id)
        if entry is None or user_context.dept_id != "supply_chain":
            raise ScopeDeniedError("当前身份无供应链数据授权")
        level, region, warehouses = entry
        return WarehouseScopeFilter(user_id=user_context.user_id, scope_level=level, region_id=region,
                                    authorized_warehouse_ids=warehouses, resolved_at=datetime.now(timezone.utc))


class DemoSqlModel:
    """规则版 Text-to-SQL。

    问题中夹带 SQL 时原样作为候选输出，模拟模型被提示词注入攻破，用于演示静态守卫与只读沙箱的拦截能力。
    """

    def __init__(self, semantic_layer: SemanticLayer | None = None) -> None:
        self.semantic_layer = semantic_layer or SemanticLayer()

    @staticmethod
    def _result(output) -> SqlModelResult:
        return SqlModelResult(output=output, token_usage=_FIXTURE_USAGE, model_name=DEMO_MODEL_NAME)

    @staticmethod
    def _embedded_sql(question: str) -> str | None:
        match = _SQL_ATTACK.search(question)
        return match.group() if match and _SQL_WRITE.search(match.group()) else None

    def _metric_ids(self, question: str) -> list[str]:
        ids = [m.metric_id for m in self.semantic_layer.match_metrics(question)]
        if not ids and "周转" in question:
            ids = ["days_of_supply"]
        if not ids and "损耗" in question:
            ids = ["material_loss_rate"]
        return ids

    async def classify_intent(self, *, question: str, catalog) -> SqlModelResult[AnalysisIntent]:
        if self._embedded_sql(question):
            return self._result(AnalysisIntent(intent_type=AnalysisIntentType.METRIC_QUERY, confidence=1,
                                               metric_ids=["safety_stock_gap"]))
        if any(t in question for t in _SENSITIVE_TERMS):
            return self._result(AnalysisIntent(intent_type=AnalysisIntentType.SENSITIVE_REQUEST, confidence=1))
        metric_ids = self._metric_ids(question)
        if not metric_ids and any(t in question for t in _OUT_OF_SCOPE_TERMS):
            return self._result(AnalysisIntent(intent_type=AnalysisIntentType.OUT_OF_SCOPE, confidence=1))
        return self._result(AnalysisIntent(
            intent_type=AnalysisIntentType.METRIC_QUERY, confidence=1, metric_ids=metric_ids[:1],
            asks_national_view="全国" in question or "全集团" in question,
            mentioned_region_ids=[r for name, r in REGION_NAMES.items() if name in question],
            mentioned_warehouse_ids=re.findall(r"\bWH_[A-Z]\d{2}\b", question),
            clarification_question=None if metric_ids else "请说明要分析的指标，例如原料损耗率、安全库存缺口或库存可供天数。"))

    async def generate_sql(self, *, question: str, intent: AnalysisIntent, context) -> SqlModelResult[SqlCandidate]:
        embedded = self._embedded_sql(question)
        if embedded:
            return self._result(SqlCandidate(sql=embedded[:8000], metric_ids=intent.metric_ids))
        return self._result(SqlCandidate(sql=self._template(question, intent, context.scope_level), metric_ids=intent.metric_ids))

    async def repair_sql(self, *, question: str, intent: AnalysisIntent, context, previous_attempts) -> SqlModelResult[SqlCandidate]:
        return await self.generate_sql(question=question, intent=intent, context=context)

    def _template(self, question: str, intent: AnalysisIntent, level: ScopeLevel) -> str:
        scope = lambda alias: self.semantic_layer.render_scope_predicate(alias, level)  # noqa: E731

        def narrow(alias: str) -> str:
            clauses = [f" AND {alias}.region_id = '{r}'" for r in intent.mentioned_region_ids[:1]]
            clauses += [f" AND {alias}.warehouse_id = '{w}'" for w in intent.mentioned_warehouse_ids[:1]]
            return "".join(clauses)

        available = "(i.on_hand_qty + i.in_transit_qty - i.reserved_qty)"
        metric = intent.metric_ids[0]
        if metric == "material_loss_rate":
            key = "region_id" if "大区" in question else "warehouse_id"
            return (
                f"WITH losses AS (SELECT l.{key}, l.material_id, SUM(l.loss_qty) AS loss_qty FROM fact_material_loss l "
                f"WHERE {scope('l')}{narrow('l')} AND l.loss_date BETWEEN date(:as_of_date, '-29 days') AND :as_of_date "
                f"GROUP BY l.{key}, l.material_id), "
                f"outbound AS (SELECT i.{key}, i.material_id, SUM(i.outbound_qty) AS outbound_qty FROM fact_warehouse_inventory_daily i "
                f"WHERE {scope('i')}{narrow('i')} AND i.snapshot_date BETWEEN date(:as_of_date, '-29 days') AND :as_of_date "
                f"GROUP BY i.{key}, i.material_id) "
                f"SELECT l.{key}, m.material_name, l.loss_qty, o.outbound_qty, "
                f"1.0 * l.loss_qty / NULLIF(o.outbound_qty + l.loss_qty, 0) AS loss_rate "
                f"FROM losses l JOIN outbound o ON o.{key} = l.{key} AND o.material_id = l.material_id "
                f"JOIN dim_material m ON m.material_id = l.material_id ORDER BY loss_rate DESC")
        if metric == "days_of_supply" and "门店" in question:
            days = f"{available} / NULLIF(i.avg_daily_outbound_7d, 0)"
            return (
                f"SELECT s.store_id, s.store_name, i.warehouse_id, m.material_name, {days} AS days_of_supply "
                f"FROM dim_store s JOIN fact_warehouse_inventory_daily i ON i.warehouse_id = s.warehouse_id "
                f"JOIN dim_material m ON m.material_id = i.material_id "
                f"WHERE {scope('s')} AND {scope('i')}{narrow('i')} AND i.snapshot_date = :as_of_date "
                f"AND ({days} < 3 OR {days} > 30) ORDER BY days_of_supply, s.store_id")
        if metric == "days_of_supply":
            return (
                f"SELECT i.warehouse_id, m.material_name, {available} / NULLIF(i.avg_daily_outbound_7d, 0) AS days_of_supply "
                f"FROM fact_warehouse_inventory_daily i JOIN dim_material m ON m.material_id = i.material_id "
                f"WHERE {scope('i')}{narrow('i')} AND i.snapshot_date = :as_of_date ORDER BY days_of_supply")
        if metric == "safety_stock_gap":
            return (
                f"SELECT i.warehouse_id, m.material_name, MAX(i.safety_stock_qty - {available}, 0) AS gap_qty "
                f"FROM fact_warehouse_inventory_daily i JOIN dim_material m ON m.material_id = i.material_id "
                f"WHERE {scope('i')}{narrow('i')} AND i.snapshot_date = :as_of_date "
                f"AND i.safety_stock_qty > {available} ORDER BY gap_qty DESC")
        if metric == "shelf_life_turnover_days":
            return (
                f"SELECT b.warehouse_id, b.batch_no, m.material_name, b.remaining_qty / NULLIF(i.avg_daily_outbound_7d, 0) AS turnover_days "
                f"FROM fact_inventory_batch b JOIN fact_warehouse_inventory_daily i ON i.warehouse_id = b.warehouse_id "
                f"AND i.material_id = b.material_id AND i.snapshot_date = b.snapshot_date "
                f"JOIN dim_material m ON m.material_id = b.material_id "
                f"WHERE {scope('b')} AND {scope('i')}{narrow('b')} AND b.snapshot_date = :as_of_date ORDER BY turnover_days DESC")
        if metric == "near_expiry_qty":
            return (
                f"SELECT b.warehouse_id, SUM(b.remaining_qty) AS near_expiry_qty FROM fact_inventory_batch b "
                f"WHERE {scope('b')}{narrow('b')} AND b.snapshot_date = :as_of_date "
                f"AND julianday(b.expiry_date) - julianday(:as_of_date) BETWEEN 0 AND 7 "
                f"GROUP BY b.warehouse_id ORDER BY near_expiry_qty DESC")
        return (
            f"SELECT s.warehouse_id, SUM(s.cups_sold) AS cups_sold FROM fact_store_sales_daily s "
            f"WHERE {scope('s')}{narrow('s')} AND s.biz_date BETWEEN date(:as_of_date, '-6 days') AND :as_of_date "
            f"GROUP BY s.warehouse_id ORDER BY cups_sold DESC")

    async def summarize_analysis(self, *, question, intent, context, result, alerts, scope_summary) -> SqlModelResult[AnalysisNarrativeDraft]:
        metric = self.semantic_layer.get_metric(intent.metric_ids[0])
        columns = [c.name for c in result.columns]
        index = next((i for i, c in enumerate(columns) if _metric_column(c, metric.metric_id)), len(columns) - 1)
        parts, figures = [f"基准日 {context.as_of_date}，授权范围内 {metric.display_name} 排名靠前的结果："], []
        for row_index, row in enumerate(result.rows[:5]):
            value = row[index]
            if not isinstance(value, (int, float)):
                continue
            label = " · ".join(str(v) for v in row if isinstance(v, str) and not re.search(r"[一-鿿]+[一二]店$", v))
            display = format_metric_value(value, metric)
            if display.endswith("%"):
                display = display[:-1].rstrip("0").rstrip(".") + "%"
            parts.append(f"{label}：{display}。")
            figures.append(KeyFigure(label=metric.display_name, value=value, unit=metric.unit, row_index=row_index, column=columns[index]))
        return self._result(AnalysisNarrativeDraft(conclusion="\n".join(parts), key_figures=figures,
                                                   follow_up_suggestions=["请结合下方预警清单安排补货、调拨或临期消化。"]))


# ---------------------------------------------------------------------------
# 财务预审
# ---------------------------------------------------------------------------

_PERSONAL_TERMS = ("私人", "家庭聚餐", "个人消费", "朋友聚会")
_PLACEHOLDER = re.compile(r"\[(?:银行卡|身份证|手机号)#\d+\]")


def _first_clause(contexts, keyword: str):
    """在已检索制度中定位含关键词的父块与逐字短句，供闭锁引用校验。"""
    for parent in contexts:
        for sentence in re.split(r"[。\n]", parent.full_content):
            if keyword in sentence and "|" not in sentence:
                return parent, sentence.strip()[:200]
    return None, ""


class DemoFinanceModel:
    """规则版语义审查；只接收脱敏视图，输出全部经过 agent 的闭锁引用校验。"""

    @staticmethod
    def _result(output) -> FinanceModelResult:
        return FinanceModelResult(output=output, token_usage=_FIXTURE_USAGE, model_name=DEMO_MODEL_NAME)

    async def review_expense_reason(self, *, view, policy_contexts) -> FinanceModelResult[ExpenseReasonReviewDraft]:
        texts = [view.reason, *(line.description for line in view.lines)]
        claims: list[ModelRiskClaim] = []
        parent, quote = _first_clause(policy_contexts, "发票")
        personal = next((t for t in texts if any(term in t for term in _PERSONAL_TERMS)), None)
        placeholder = next((m.group() for t in texts if (m := _PLACEHOLDER.search(t))), None)
        if parent is not None and personal:
            claims.append(ModelRiskClaim(code=ViolationCode.PERSONAL_CONSUMPTION_SUSPECTED, risk_level=RiskLevel.HIGH,
                rationale="事由描述疑似个人消费，与门店经营用途不符。", evidence_excerpt=personal[:200],
                citation_parent_id=parent.parent_id, citation_quote=quote))
        if parent is not None and placeholder:
            claims.append(ModelRiskClaim(code=ViolationCode.PERSONAL_CONSUMPTION_SUSPECTED, risk_level=RiskLevel.HIGH,
                rationale="事由文本夹带个人证件或收款信息，无法确认费用归属，需人工复核收款主体。",
                evidence_excerpt=placeholder, citation_parent_id=parent.parent_id, citation_quote=quote))
        return self._result(ExpenseReasonReviewDraft(claims=claims, reason_matches_categories=not claims,
                                                     summary="未发现语义风险。" if not claims else "发现需人工确认的语义风险。"))

    async def review_contract_clauses(self, *, view, policy_contexts) -> FinanceModelResult[ContractClauseReviewDraft]:
        text = "\n".join((view.payment_terms_text, view.breach_clause_text, view.other_key_clauses))
        claims: list[ModelRiskClaim] = []
        if excerpt := next((k for k in ("不设上限", "无上限") if k in text), None):
            parent, quote = _first_clause(policy_contexts, "无上限责任")
            if parent is not None:
                claims.append(ModelRiskClaim(code=ViolationCode.UNLIMITED_LIABILITY, risk_level=RiskLevel.HIGH,
                    rationale="条款约定我方承担无上限责任。", evidence_excerpt=excerpt,
                    citation_parent_id=parent.parent_id, citation_quote=quote))
        days = re.search(r"账期\s*(\d{1,4})\s*天|(\d{1,4})\s*天内付款", view.payment_terms_text)
        stated = int(days.group(1) or days.group(2)) if days else None
        return self._result(ContractClauseReviewDraft(claims=claims, extracted_terms=ContractTermExtraction(
            stated_payment_term_days=stated, has_liability_cap=False if claims else None)))

    async def verify_citation_support(self, *, claims, policy_contexts) -> FinanceModelResult[CitationSupportDraft]:
        return self._result(CitationSupportDraft(verdicts=[
            CitationSupportVerdict(claim_index=i, supported=True) for i in range(len(claims))]))


_TZ_SEED = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


class DemoClaimLedger:
    """内存历史台账，预置已支付发票与近期报销，用于重复发票和拆单演示。"""

    def __init__(self) -> None:
        self.invoice_usages = [InvoiceUsageRecord(invoice_no="INV20261001999", claim_id="EXP-HIST-001",
            applicant_user_id="store_clerk_02", status="paid", submitted_at=_TZ_SEED)]
        self.claims: list[HistoricalClaimRecord] = []
        self.contracts: list[HistoricalContractRecord] = []

    async def find_invoice_usages(self, *, invoice_nos, exclude_claim_id):
        keys = {_invoice_key(n) for n in invoice_nos}
        return [r for r in self.invoice_usages if _invoice_key(r.invoice_no) in keys and r.claim_id != exclude_claim_id]

    async def list_recent_claims(self, *, applicant_user_id, store_code, categories, window_start, window_end, exclude_claim_id):
        return [r for r in self.claims if r.claim_id != exclude_claim_id and r.applicant_user_id == applicant_user_id
                and r.store_code == store_code and r.category in categories and window_start <= r.submitted_at <= window_end]

    async def list_recent_contracts(self, *, counterparty_credit_code, window_start, window_end, exclude_contract_id):
        return [r for r in self.contracts if r.contract_id != exclude_contract_id
                and r.counterparty_credit_code == counterparty_credit_code and window_start <= r.submitted_at <= window_end]

    async def fetch_submission(self, *, subject_type, reference_id, user_context):
        return None


class DemoApprovalGateway:
    """幂等的内存人工复核队列；不发送任何真实审批或通知。"""

    def __init__(self) -> None:
        self.tickets: dict[str, HumanApprovalTicketReceipt] = {}
        self.drafts: dict[str, HumanApprovalTicketDraft] = {}

    async def create_review_ticket(self, draft: HumanApprovalTicketDraft) -> HumanApprovalTicketReceipt:
        key = draft.idempotency_key
        if key in self.tickets:
            return self.tickets[key].model_copy(update={"deduplicated": True})
        receipt = HumanApprovalTicketReceipt(ticket_id="FA-" + hashlib.sha256(key.encode()).hexdigest()[:16].upper(),
                                             queue=draft.queue, created_at=draft.created_at)
        self.tickets[key] = receipt
        self.drafts[receipt.ticket_id] = draft
        return receipt


