"""财务确定性规则引擎。

纯 CPU、无 I/O、不读系统时钟（基准时间一律取 submission.submitted_at），全部金额运算使用 decimal.Decimal：

Decimal 精度铁律
- 禁止 float 参与任何金额 / 税率 / 数量运算；禁止 Decimal(float)；常量一律 Decimal("…") 字符串构造。
- 中间乘积用 to_money()（quantize(Decimal("0.01"), ROUND_HALF_UP)）归一后再比较；比较一律 Decimal 精确比较，
  容差来自 FinanceRuleConfig（同为 Decimal）。
- 运算在 decimal.localcontext(DECIMAL_CONTEXT) 中进行（prec=28，traps: InvalidOperation / DivisionByZero / Overflow）。

规则矩阵（rule_id → 编码；处置下限见 models.VIOLATION_POLICIES，章节提示见 RULE_POLICY_SECTIONS）

    rule_id   对象    检查                                                         编码
    FIN-R01   报销    行：to_money(quantity×unit_price) vs amount_excl_tax（容差）      LINE_AMOUNT_MISMATCH
                     行：amount_excl_tax + tax_amount == amount_incl_tax（精确）
    FIN-R02   报销    行：tax_rate ∈ allowed_tax_rates                                TAX_RATE_NOT_ALLOWED
    FIN-R03   报销    行：|to_money(excl×rate) − tax_amount| <= line_tax_tolerance      TAX_AMOUNT_MISMATCH
    FIN-R04   报销    按发票汇总：含税合计 == 票面含税；|税额合计 − 票面税额| <= 尾差      INVOICE_TOTAL_MISMATCH
    FIN-R05   报销    |declared_total − Σ 行含税| <= declared_total_tolerance          DECLARED_TOTAL_MISMATCH
    FIN-R06   报销    invoices 内发票号重复（NFKC + 大写后比较）                         DUPLICATE_INVOICE_IN_SUBMISSION
    FIN-R07   报销    台账占用：PAID / APPROVED → _PAID；SUBMITTED → _IN_FLIGHT         DUPLICATE_INVOICE_PAID / _IN_FLIGHT
    FIN-R08   报销    拆单：见 detect_split_claim                                      SUSPECTED_SPLIT_CLAIM
    FIN-R09   报销    类别单笔额度：该类别明细含税合计 > max_single_amount                 CATEGORY_LIMIT_EXCEEDED
    FIN-R10   报销    附件：各出现类别 required_attachments ⊆ attachments                MISSING_ATTACHMENT
    FIN-R11   报销    时限：submitted_at.date() − min(occurred_on) > deadline_days       SUBMISSION_OVERDUE
    FIN-R12   报销    住宿：TRAVEL_LODGING 行 unit_price > cap 且 pre_approval_ref 为空    LODGING_OVER_CAP
    FIN-R13   通用    自审：caller.user_id == applicant.user_id                          SELF_REVIEW_CONFLICT
    FIN-R14   通用    注入关键词：reason / 明细 / 条款文本（NFKC+casefold）命中 markers      PROMPT_INJECTION_SUSPECTED
    CTR-R01   合同    付款计划：schedule 非空时 Σ amount == subject_amount（精确）         PAYMENT_SCHEDULE_MISMATCH
    CTR-R02   合同    账期：max(payment_term_days, max due_in_days) > max_payment_term_days PAYMENT_TERM_EXCEEDED
    CTR-R03   合同    违约条款：breach_clause_text.strip() 为空                          BREACH_CLAUSE_MISSING
    CTR-R04   合同    拆分：见 detect_split_contract                                    SUSPECTED_SPLIT_CONTRACT
    CTR-R05   合同    subject_amount > contract_legal_review_threshold                  LEGAL_REVIEW_REQUIRED

审批层级判定（resolve_approver_tier）
- 报销：approval_amount = max(declared_total, 拆单窗口合计)；按 approval_bands 左开右闭定档；
  定档为 STORE_MANAGER 且申请人 roles 含 "store_manager" → 升级为 REGIONAL_MANAGER（制度：店长本人申请转区域经理）。
- 合同：subject_amount（或拆分窗口合计）> contract_legal_review_threshold → LEGAL_AND_FINANCE_HEAD，
  否则按 approval_bands 定档。

本模块同时提供金额工具函数与 FIN_RULE_V1 评测上下文渲染 / 解析（agent 与 eval_judge 共用同一口径）。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import timedelta
from agent_matrix.finance_audit.errors import FinanceRuleConfigError
from agent_matrix.finance_audit.models import (
    ACTIVE_INVOICE_USAGE_STATUSES, VIOLATION_POLICIES, ApprovalRoute, AuditSubjectType,
    AttachmentType, ExpenseCategory, FindingSource, InvoiceUsageStatus,
)

import decimal
from collections.abc import Sequence
from decimal import Decimal

from agent_matrix.finance_audit.models import (
    AmountReconciliationResult,
    ApproverTier,
    ComplianceFinding,
    ContractAuditSubmission,
    DuplicateInvoiceFlag,
    ExpenseAuditSubmission,
    FinanceRuleConfig,
    InvoiceTotalCheck,
    LedgerSnapshot,
    LineAmountCheck,
    PaymentScheduleCheck,
    RuleEvaluation,
    SplitClaimEvidence,
    ViolationCode,
)
from data_platform.retrieval_engine.models import UserContext

DECIMAL_CONTEXT = decimal.Context(
    prec=28,
    rounding=decimal.ROUND_HALF_UP,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
)
"""规则引擎专用 Decimal 上下文；所有运算在 decimal.localcontext(DECIMAL_CONTEXT) 内进行。"""

RULE_POLICY_SECTIONS: dict[ViolationCode, str] = {
    ViolationCode.LINE_AMOUNT_MISMATCH: "付款复核与归档",
    ViolationCode.TAX_RATE_NOT_ALLOWED: "费用审批矩阵",
    ViolationCode.TAX_AMOUNT_MISMATCH: "费用审批矩阵",
    ViolationCode.INVOICE_TOTAL_MISMATCH: "付款复核与归档",
    ViolationCode.DECLARED_TOTAL_MISMATCH: "付款复核与归档",
    ViolationCode.DUPLICATE_INVOICE_IN_SUBMISSION: "报销时限与凭证",
    ViolationCode.DUPLICATE_INVOICE_PAID: "报销时限与凭证",
    ViolationCode.DUPLICATE_INVOICE_IN_FLIGHT: "报销时限与凭证",
    ViolationCode.SUSPECTED_SPLIT_CLAIM: "费用审批矩阵",
    ViolationCode.CATEGORY_LIMIT_EXCEEDED: "费用审批矩阵",
    ViolationCode.MISSING_ATTACHMENT: "费用审批矩阵",
    ViolationCode.SUBMISSION_OVERDUE: "报销时限与凭证",
    ViolationCode.LODGING_OVER_CAP: "差旅标准与超标处理",
    ViolationCode.SELF_REVIEW_CONFLICT: "报销时限与凭证",
}
"""RULE_ENGINE 编码 → 《财务费用报销制度》章节名（与 mock_docs/finance_expense_policy.md 二级标题逐字一致）。

agent 用它在本轮检索到的父块中按 section_path 末级精确匹配，确定性绑定 SopCitation；未检索到对应章节时
finding 保留 rule_id + policy_section、citations 为空（确定性规则不依赖引用成立）。合同类编码的章节映射见
CONTRACT_POLICY_SECTIONS（对应 tests/agent_matrix/finance_audit/data 中的合同管理制度测试文档）。
"""

CONTRACT_POLICY_SECTIONS: dict[ViolationCode, str] = {
    ViolationCode.PAYMENT_SCHEDULE_MISMATCH: "付款与账期",
    ViolationCode.PAYMENT_TERM_EXCEEDED: "付款与账期",
    ViolationCode.BREACH_CLAUSE_MISSING: "违约责任",
    ViolationCode.SUSPECTED_SPLIT_CONTRACT: "合同审批权限",
    ViolationCode.LEGAL_REVIEW_REQUIRED: "合同审批权限",
}

RULE_CONTEXT_HEADER = "FIN_RULE_V1"
"""评测上下文首行。格式（逐行，键固定、顺序固定）：

    FIN_RULE_V1
    subject: expense|contract <reference_id>
    declared_total: 3200.00
    computed_total: 3200.00
    total_deviation: 0.00
    approval_amount: 3200.00
    approver_tier: regional_manager_and_finance
    split_window_total: 3800.00          （仅 split_suspected）
    finding: FIN_SUSPECTED_SPLIT_CLAIM | CRITICAL | FIN-R08 | 0.00

金额一律两位小数、无千分位、无货币符号；finding 行末字段为 amount_delta（无则 "-"）。
"""


# ---------------------------------------------------------------------------
# 金额工具（agent / eval_judge 共用）
# ---------------------------------------------------------------------------


def to_money(value: Decimal) -> Decimal:
    """quantize(Decimal("0.01"), ROUND_HALF_UP)；value 非 Decimal（含 float）抛 TypeError。"""
    if not isinstance(value, Decimal):
        raise TypeError('金额必须为 Decimal')
    with decimal.localcontext(DECIMAL_CONTEXT):
        return value.quantize(Decimal('0.01'), rounding=decimal.ROUND_HALF_UP)


def format_money(value: Decimal) -> str:
    """展示格式：¥ + 千分位 + 两位小数，如 Decimal("3200") → "¥3,200.00"；负数为 "-¥12.50"。"""
    with decimal.localcontext(DECIMAL_CONTEXT):
        value = to_money(value)
        return ('-¥' if value < 0 else '¥') + f'{abs(value):,.2f}'


def extract_money_tokens(text: str) -> list[Decimal]:
    """从回答文本中抽取金额 token（"¥3,200.00" / "3200.00 元" / "3,200元"），去千分位后转 Decimal（经 to_money）。

    不抽取：日期、百分比、发票号 / 单号等含字母或长度 >= 8 且无小数点的纯数字串、占位符内的编号。
    """
    tokens = []
    # Preserve excess decimal places: rounding evidence would hide sub-cent hallucinations.
    pattern = r'(?<![A-Za-z0-9_.:#-])(?P<sign>-)?(?P<currency>[¥￥])?(?P<number>-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)(?P<unit>\s*元)?(?![A-Za-z0-9_.%％-])'
    for match in re.finditer(pattern, text):
        number = (match.group('sign') or '') + match.group('number').replace(',', '')
        if '.' not in number and (len(number.lstrip('-')) >= 8 or not (match.group('currency') or match.group('unit'))):
            continue
        value = Decimal(number)
        tokens.append(to_money(value) if value == to_money(value) else value)
    return tokens


def render_rule_context(*, reference_id: str, evaluation: RuleEvaluation) -> str:
    """按 RULE_CONTEXT_HEADER 文档格式渲染（确定性，不含 PII、不含申请人信息）。"""
    r = evaluation.reconciliation
    lines = [RULE_CONTEXT_HEADER, f'subject: {r.subject_type.value} {reference_id}',
             f'declared_total: {r.declared_total:.2f}', f'computed_total: {r.computed_total:.2f}',
             f'total_deviation: {r.total_deviation:.2f}', f'approval_amount: {r.approval_amount:.2f}',
             f'approver_tier: {r.approver_tier.value}']
    if r.split_evidence:
        lines.append(f'split_window_total: {r.split_evidence.window_total:.2f}')
    for f in evaluation.findings:
        amount = '-' if f.amount_delta is None else f'{f.amount_delta:.2f}'
        lines.append(f'finding: {f.code.value} | {f.risk_level.value} | {f.rule_id} | {amount}')
    return '\n'.join(lines)


def parse_rule_context(text: str) -> dict[str, list[str]] | None:
    """解析 FIN_RULE_V1 文本为 {键: [值, ...]}（finding 可多行）；首行不是 RULE_CONTEXT_HEADER 时返回 None。"""
    lines = text.splitlines()
    if not lines or lines[0] != RULE_CONTEXT_HEADER:
        return None
    result = {}
    for line in lines[1:]:
        key, separator, value = line.partition(': ')
        if separator:
            result.setdefault(key, []).append(value)
    return result


# ---------------------------------------------------------------------------
# 规则引擎
# ---------------------------------------------------------------------------


class DeterministicFinanceRuleEngine:
    """Decimal 确定性财务规则引擎（无状态、可复用、线程安全）。"""

    def __init__(self, config: FinanceRuleConfig | None = None) -> None:
        """保存配置（缺省 FinanceRuleConfig()），并执行 self_check()。"""
        self.config = config or FinanceRuleConfig()
        self.self_check()

    def self_check(self) -> None:
        """启动期自检，失败抛 FinanceRuleConfigError：

        - category_rules 覆盖 ExpenseCategory 全部取值；
        - allowed_tax_rates 全部为 Decimal 且 0 <= r <= 0.17；
        - RULE_POLICY_SECTIONS ∪ CONTRACT_POLICY_SECTIONS 的键均为 allowed_sources 含 RULE_ENGINE 的编码；
        - 每个 rule_id 产出的编码在 VIOLATION_POLICIES 中存在。
        """
        try:
            FinanceRuleConfig.model_validate(self.config.model_dump())
            assert {r.category for r in self.config.category_rules} == set(ExpenseCategory)
            assert all(isinstance(r, Decimal) and Decimal('0') <= r <= Decimal('0.17') for r in self.config.allowed_tax_rates)
            assert set(VIOLATION_POLICIES) == set(ViolationCode)
            assert all(FindingSource.RULE_ENGINE in VIOLATION_POLICIES[c].allowed_sources
                       for c in RULE_POLICY_SECTIONS | CONTRACT_POLICY_SECTIONS)
        except (AssertionError, ValueError, KeyError):
            raise FinanceRuleConfigError('财务规则配置不一致') from None

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def evaluate_expense(
        self,
        submission: ExpenseAuditSubmission,
        *,
        ledger: LedgerSnapshot,
        caller: UserContext,
    ) -> RuleEvaluation:
        """依次执行 FIN-R01 ~ FIN-R14，组装 AmountReconciliationResult 与 RULE_ENGINE findings。

        - findings 按 (risk_level 降序, rule_id 升序, 行号升序) 排序；finding_id = f"{code.value}:{序号}"（从 1 起，按编码分别计数）。
        - finding.detail 只使用结构化字段（行号、发票号、Decimal 金额），**不得**拼接 reason / description 原文。
        - NEEDS_SUPPLEMENT 级 finding 的 supplement_request 用确定性模板（如"请补充附件：采购清单、验收记录"）。
        - ledger.subject_type 必须为 EXPENSE，否则 ValueError。
        """
        if ledger.subject_type is not AuditSubjectType.EXPENSE:
            raise ValueError('报销必须使用报销台账快照')
        with decimal.localcontext(DECIMAL_CONTEXT):
            lines = self.check_line_amounts(submission)
            invoices = self.check_invoice_totals(submission)
            duplicates = self.detect_duplicate_invoices(submission, ledger=ledger)
            split = self.detect_split_claim(submission, ledger=ledger)
            findings = []
            for c in lines:
                if not c.amount_passed:
                    delta = c.declared_amount_excl_tax-c.expected_amount_excl_tax
                    if c.declared_amount_incl_tax != c.expected_amount_incl_tax:
                        delta = c.declared_amount_incl_tax-c.expected_amount_incl_tax
                    findings.append(_finding(ViolationCode.LINE_AMOUNT_MISMATCH, 'FIN-R01', detail=f'第 {c.line_no} 行金额勾稽不符', related_line_nos=[c.line_no], amount_delta=delta))
                if not c.tax_rate_allowed:
                    findings.append(_finding(ViolationCode.TAX_RATE_NOT_ALLOWED, 'FIN-R02', detail=f'第 {c.line_no} 行税率不在白名单', related_line_nos=[c.line_no]))
                if not c.tax_passed:
                    findings.append(_finding(ViolationCode.TAX_AMOUNT_MISMATCH, 'FIN-R03', detail=f'第 {c.line_no} 行税额不符', related_line_nos=[c.line_no], amount_delta=c.declared_tax_amount-c.expected_tax_amount))
            for c in invoices:
                if not c.passed:
                    findings.append(_finding(ViolationCode.INVOICE_TOTAL_MISMATCH, 'FIN-R04', detail='发票汇总与票面不符', related_invoice_nos=[c.invoice_no], amount_delta=c.amount_deviation if c.amount_deviation else c.tax_deviation))
            computed = sum((line.amount_incl_tax for line in submission.lines), Decimal('0'))
            delta = submission.declared_total-computed
            within = abs(delta) <= self.config.declared_total_tolerance
            if not within:
                findings.append(_finding(ViolationCode.DECLARED_TOTAL_MISMATCH, 'FIN-R05', detail='申报总额与明细合计不符', amount_delta=delta))
            for flag in duplicates:
                findings.append(_finding(flag.code, 'FIN-R06' if flag.conflicting_claim_id is None else 'FIN-R07', detail='发票重复占用', related_invoice_nos=[flag.invoice_no]))
            if split:
                findings.append(_finding(ViolationCode.SUSPECTED_SPLIT_CLAIM, 'FIN-R08', detail='滚动窗口内存在疑似拆单', amount_delta=Decimal('0.00')))
            findings.extend(self.check_policy_compliance(submission))
            findings.extend(f for f in (self.check_self_review(applicant_user_id=submission.applicant.user_id, caller=caller), self.scan_prompt_injection([submission.reason, *(line.description for line in submission.lines)])) if f)
            approval = max(submission.declared_total, split.window_total if split else submission.declared_total)
            tier = self.resolve_approver_tier(approval_amount=approval, applicant_roles=submission.applicant.roles, is_contract=False)
            tax_passed = all(c.tax_passed and c.tax_rate_allowed for c in lines) and all(abs(c.tax_deviation) <= self.config.invoice_tax_tail_tolerance for c in invoices)
            reconciliation = AmountReconciliationResult(subject_type=AuditSubjectType.EXPENSE,
                declared_total=submission.declared_total, computed_total=computed, total_deviation=delta,
                total_within_tolerance=within, line_checks=lines, invoice_checks=invoices, tax_check_passed=tax_passed,
                duplicate_invoice_flags=duplicates, has_duplicate_invoice=bool(duplicates), split_suspected=split is not None,
                split_evidence=split, approval_amount=approval, approver_tier=tier,
                passed=within and tax_passed and all(c.amount_passed for c in lines) and all(c.passed for c in invoices) and not duplicates and split is None)
            return RuleEvaluation(ruleset_version=self.config.ruleset_version, reconciliation=reconciliation, findings=_order_findings(findings), approver_tier=tier)

    def evaluate_contract(
        self,
        submission: ContractAuditSubmission,
        *,
        ledger: LedgerSnapshot,
        caller: UserContext,
    ) -> RuleEvaluation:
        """依次执行 CTR-R01 ~ CTR-R05、FIN-R13、FIN-R14；无付款计划时 computed_total = subject_amount。"""
        if ledger.subject_type is not AuditSubjectType.CONTRACT:
            raise ValueError('合同必须使用合同台账快照')
        with decimal.localcontext(DECIMAL_CONTEXT):
            schedule = self.check_payment_schedule(submission)
            split = self.detect_split_contract(submission, ledger=ledger)
            findings = []
            if schedule and not schedule.passed:
                findings.append(_finding(ViolationCode.PAYMENT_SCHEDULE_MISMATCH, 'CTR-R01', detail='付款计划与标的金额不符', amount_delta=schedule.deviation))
            if max(submission.payment_term_days, schedule.max_due_in_days if schedule else 0) > self.config.max_payment_term_days:
                findings.append(_finding(ViolationCode.PAYMENT_TERM_EXCEEDED, 'CTR-R02', detail='合同账期超过制度上限'))
            if not submission.breach_clause_text.strip():
                findings.append(_finding(ViolationCode.BREACH_CLAUSE_MISSING, 'CTR-R03', detail='缺少违约责任条款', supplement_request='请补充双方违约责任条款'))
            if split:
                findings.append(_finding(ViolationCode.SUSPECTED_SPLIT_CONTRACT, 'CTR-R04', detail='滚动窗口内存在疑似拆分合同', amount_delta=Decimal('0.00')))
            if submission.subject_amount > self.config.contract_legal_review_threshold:
                findings.append(_finding(ViolationCode.LEGAL_REVIEW_REQUIRED, 'CTR-R05', detail='合同标的金额超过法务审查门槛'))
            findings.extend(f for f in (self.check_self_review(applicant_user_id=submission.applicant.user_id, caller=caller), self.scan_prompt_injection([submission.title, submission.payment_terms_text, submission.breach_clause_text, submission.other_key_clauses, *(p.description for p in submission.payment_schedule)])) if f)
            computed = schedule.schedule_total if schedule else submission.subject_amount
            approval = max(submission.subject_amount, split.window_total if split else submission.subject_amount)
            tier = self.resolve_approver_tier(approval_amount=approval, applicant_roles=submission.applicant.roles, is_contract=True)
            reconciliation = AmountReconciliationResult(subject_type=AuditSubjectType.CONTRACT,
                declared_total=submission.subject_amount, computed_total=computed, total_deviation=submission.subject_amount-computed,
                total_within_tolerance=computed == submission.subject_amount, tax_check_passed=True,
                payment_schedule_check=schedule, split_suspected=split is not None, split_evidence=split,
                approval_amount=approval, approver_tier=tier, passed=(schedule is None or schedule.passed) and split is None)
            return RuleEvaluation(ruleset_version=self.config.ruleset_version, reconciliation=reconciliation, findings=_order_findings(findings), approver_tier=tier)

    # ------------------------------------------------------------------
    # 金额勾稽
    # ------------------------------------------------------------------

    def check_line_amounts(self, submission: ExpenseAuditSubmission) -> list[LineAmountCheck]:
        """FIN-R01 / R02 / R03 逐行勾稽（按 line_no 顺序）。"""
        with decimal.localcontext(DECIMAL_CONTEXT):
            checks = []
            for line in submission.lines:
                excl = to_money(line.quantity * line.unit_price)
                tax = to_money(line.amount_excl_tax * line.tax_rate)
                incl = line.amount_excl_tax + line.tax_amount
                checks.append(LineAmountCheck(
                    line_no=line.line_no, expected_amount_excl_tax=excl, declared_amount_excl_tax=line.amount_excl_tax,
                    expected_tax_amount=tax, declared_tax_amount=line.tax_amount,
                    expected_amount_incl_tax=incl, declared_amount_incl_tax=line.amount_incl_tax,
                    tax_rate=line.tax_rate, tax_rate_allowed=line.tax_rate in self.config.allowed_tax_rates,
                    amount_passed=abs(excl-line.amount_excl_tax) <= self.config.line_amount_tolerance and incl == line.amount_incl_tax,
                    tax_passed=abs(tax-line.tax_amount) <= self.config.line_tax_tolerance,
                ))
            return checks

    def check_invoice_totals(self, submission: ExpenseAuditSubmission) -> list[InvoiceTotalCheck]:
        """FIN-R04：按 invoice_no 分组汇总明细（同号多张票面时以第一张为准，重复由 FIN-R06 报告）。"""
        with decimal.localcontext(DECIMAL_CONTEXT):
            checks, seen = [], set()
            for invoice in submission.invoices:
                number = _invoice_key(invoice.invoice_no)
                if number in seen:
                    continue
                seen.add(number)
                lines = [line for line in submission.lines if _invoice_key(line.invoice_no) == number]
                amount = sum((line.amount_incl_tax for line in lines), Decimal('0'))
                tax = sum((line.tax_amount for line in lines), Decimal('0'))
                da, dt = amount - invoice.amount_incl_tax, tax - invoice.tax_amount
                checks.append(InvoiceTotalCheck(invoice_no=invoice.invoice_no, line_nos=[line.line_no for line in lines],
                    lines_amount_incl_tax=amount, invoice_amount_incl_tax=invoice.amount_incl_tax,
                    lines_tax_amount=tax, invoice_tax_amount=invoice.tax_amount, amount_deviation=da, tax_deviation=dt,
                    passed=da == 0 and abs(dt) <= self.config.invoice_tax_tail_tolerance))
            return checks

    def check_payment_schedule(self, submission: ContractAuditSubmission) -> PaymentScheduleCheck | None:
        """CTR-R01 / R02 的计算部分；schedule 为空返回 None。prepayment_ratio 量化到 0.0001（ROUND_HALF_UP）。"""
        if not submission.payment_schedule:
            return None
        with decimal.localcontext(DECIMAL_CONTEXT):
            total = sum((p.amount for p in submission.payment_schedule), Decimal('0'))
            prepaid = sum((p.amount for p in submission.payment_schedule if p.due_in_days == 0), Decimal('0'))
            return PaymentScheduleCheck(schedule_total=total, subject_amount=submission.subject_amount,
                deviation=total-submission.subject_amount, prepayment_amount=prepaid,
                prepayment_ratio=min(Decimal('1'), prepaid/submission.subject_amount).quantize(Decimal('0.0001')),
                max_due_in_days=max(p.due_in_days for p in submission.payment_schedule), passed=total == submission.subject_amount)

    # ------------------------------------------------------------------
    # 重复 / 拆单
    # ------------------------------------------------------------------

    def detect_duplicate_invoices(
        self,
        submission: ExpenseAuditSubmission,
        *,
        ledger: LedgerSnapshot,
    ) -> list[DuplicateInvoiceFlag]:
        """FIN-R06 / R07。只有 ACTIVE_INVOICE_USAGE_STATUSES 的占用构成跨单重复；
        同一发票同时存在 PAID 与 SUBMITTED 占用时只报 PAID（更严重者）。结果按 invoice_no 升序。"""
        numbers = Counter(_invoice_key(i.invoice_no) for i in submission.invoices)
        flags = []
        for number in sorted(numbers):
            if numbers[number] > 1:
                flags.append(DuplicateInvoiceFlag(invoice_no=number, code=ViolationCode.DUPLICATE_INVOICE_IN_SUBMISSION))
            usages = [u for u in ledger.invoice_usages if _invoice_key(u.invoice_no) == number
                      and u.claim_id != submission.claim_id and u.status in ACTIVE_INVOICE_USAGE_STATUSES]
            usages.sort(key=lambda u: (u.status is InvoiceUsageStatus.SUBMITTED, u.claim_id))
            if usages:
                u = usages[0]
                flags.append(DuplicateInvoiceFlag(invoice_no=number, conflicting_claim_id=u.claim_id,
                    conflicting_status=u.status, same_applicant=u.applicant_user_id == submission.applicant.user_id,
                    code=ViolationCode.DUPLICATE_INVOICE_IN_FLIGHT if u.status is InvoiceUsageStatus.SUBMITTED else ViolationCode.DUPLICATE_INVOICE_PAID))
        return flags

    def detect_split_claim(
        self,
        submission: ExpenseAuditSubmission,
        *,
        ledger: LedgerSnapshot,
    ) -> SplitClaimEvidence | None:
        """FIN-R08 拆单规避审批检测。

        候选集合 = 本单 + ledger.recent_claims 中 status ∈ ACTIVE_INVOICE_USAGE_STATUSES、同申请人、同门店、
        类别与本单任一明细类别相同的单据（窗口 [submitted_at − split_window_days, submitted_at]）。
        对每个审批档上限 U（approval_bands 中非 None 的 upper_inclusive）：
            若候选集合中每张单据金额均 <= U 且 合计 > U 且 候选数 >= 2 → 命中，bypassed_threshold = U；
        取命中的最大 U；escalated_tier = 按合计定档的层级。未命中返回 None。
        """
        start = submission.submitted_at - timedelta(days=self.config.split_window_days)
        categories = {line.category for line in submission.lines}
        rows = {r.claim_id: r for r in ledger.recent_claims if r.claim_id != submission.claim_id
                and r.applicant_user_id == submission.applicant.user_id and r.store_code == submission.store_code
                and r.category in categories and r.status in ACTIVE_INVOICE_USAGE_STATUSES
                and start <= r.submitted_at <= submission.submitted_at}
        with decimal.localcontext(DECIMAL_CONTEXT):
            amounts = [submission.declared_total, *(r.amount_incl_tax for r in rows.values())]
            total = sum(amounts, Decimal('0'))
            thresholds = [b.upper_inclusive for b in self.config.approval_bands if b.upper_inclusive is not None
                          and max(amounts) <= b.upper_inclusive < total]
            if not rows or not thresholds:
                return None
            return SplitClaimEvidence(window_start=start, window_end=submission.submitted_at,
                related_reference_ids=sorted(rows), window_total=total, bypassed_threshold=max(thresholds),
                single_amounts_below_threshold=True, escalated_tier=self.resolve_approver_tier(
                    approval_amount=total, applicant_roles=submission.applicant.roles, is_contract=False))

    def detect_split_contract(
        self,
        submission: ContractAuditSubmission,
        *,
        ledger: LedgerSnapshot,
    ) -> SplitClaimEvidence | None:
        """CTR-R04：窗口 contract_split_window_days 内同一相对方、同一 contract_type 的有效合同，
        每份 <= contract_legal_review_threshold 且合计 > 门槛且份数 >= 2 → 命中。"""
        start = submission.submitted_at - timedelta(days=self.config.contract_split_window_days)
        rows = {r.contract_id: r for r in ledger.recent_contracts if r.contract_id != submission.contract_id
                and r.counterparty_credit_code == submission.counterparty.unified_credit_code
                and r.contract_type == submission.contract_type and r.status in ACTIVE_INVOICE_USAGE_STATUSES
                and start <= r.submitted_at <= submission.submitted_at}
        with decimal.localcontext(DECIMAL_CONTEXT):
            amounts = [submission.subject_amount, *(r.subject_amount for r in rows.values())]
            total = sum(amounts, Decimal('0'))
            threshold = self.config.contract_legal_review_threshold
            if not rows or not max(amounts) <= threshold < total:
                return None
            return SplitClaimEvidence(window_start=start, window_end=submission.submitted_at,
                related_reference_ids=sorted(rows), window_total=total, bypassed_threshold=threshold,
                single_amounts_below_threshold=True, escalated_tier=self.resolve_approver_tier(
                    approval_amount=total, applicant_roles=submission.applicant.roles, is_contract=True))

    # ------------------------------------------------------------------
    # 制度合规与定档
    # ------------------------------------------------------------------

    def resolve_approver_tier(
        self,
        *,
        approval_amount: Decimal,
        applicant_roles: Sequence[str],
        is_contract: bool,
    ) -> ApproverTier:
        """见模块文档"审批层级判定"。"""
        if not isinstance(approval_amount, Decimal):
            raise TypeError('定档金额必须为 Decimal')
        if is_contract and approval_amount > self.config.contract_legal_review_threshold:
            return ApproverTier.LEGAL_AND_FINANCE_HEAD
        for band in self.config.approval_bands:
            if band.lower_exclusive < approval_amount and (band.upper_inclusive is None or approval_amount <= band.upper_inclusive):
                if not is_contract and band.approver_tier is ApproverTier.STORE_MANAGER and 'store_manager' in applicant_roles:
                    return ApproverTier.REGIONAL_MANAGER
                return band.approver_tier
        raise FinanceRuleConfigError('金额未命中审批档位')

    def check_policy_compliance(
        self,
        submission: ExpenseAuditSubmission,
    ) -> list[ComplianceFinding]:
        """FIN-R09 ~ FIN-R12（类别额度、附件、时限、住宿标准）。"""
        findings = []
        with decimal.localcontext(DECIMAL_CONTEXT):
            for rule in self.config.category_rules:
                lines = [line for line in submission.lines if line.category == rule.category]
                if not lines:
                    continue
                total = sum((line.amount_incl_tax for line in lines), Decimal('0'))
                if rule.max_single_amount is not None and total > rule.max_single_amount:
                    findings.append(_finding(ViolationCode.CATEGORY_LIMIT_EXCEEDED, 'FIN-R09',
                        detail='费用类别单笔金额超限', amount_delta=total-rule.max_single_amount))
                missing = [a for a in rule.required_attachments if a not in submission.attachments]
                if missing:
                    supplement = '请补充附件：' + '、'.join(_ATTACHMENT_NAMES[a] for a in missing)
                    findings.append(_finding(ViolationCode.MISSING_ATTACHMENT, 'FIN-R10', detail=supplement, supplement_request=supplement))
            if (submission.submitted_at.date() - min(line.occurred_on for line in submission.lines)).days > self.config.submission_deadline_days:
                findings.append(_finding(ViolationCode.SUBMISSION_OVERDUE, 'FIN-R11', detail='费用提交超过制度时限'))
            for line in submission.lines:
                if line.category is ExpenseCategory.TRAVEL_LODGING and not submission.pre_approval_ref and line.unit_price > self.config.lodging_cap_per_night:
                    findings.append(_finding(ViolationCode.LODGING_OVER_CAP, 'FIN-R12', detail='住宿每晚单价超过标准且缺事前审批',
                        related_line_nos=[line.line_no], amount_delta=to_money((line.unit_price-self.config.lodging_cap_per_night)*line.quantity),
                        supplement_request='请补充超标住宿事前审批或调整报销金额'))
        return findings

    def check_self_review(
        self,
        *,
        applicant_user_id: str,
        caller: UserContext,
    ) -> ComplianceFinding | None:
        """FIN-R13：caller.user_id == applicant_user_id → SELF_REVIEW_CONFLICT。"""
        if caller.user_id == applicant_user_id:
            return _finding(ViolationCode.SELF_REVIEW_CONFLICT, 'FIN-R13', detail='调用人与申请人相同，须回避审核')
        return None

    def scan_prompt_injection(self, texts: Sequence[str]) -> ComplianceFinding | None:
        """FIN-R14：NFKC + casefold 后对 prompt_injection_markers 做子串匹配；命中一个即产出单条 finding，
        detail 只写命中的 marker 序号（如"命中注入特征 #2"），不回显原文。"""
        for index, marker in enumerate(self.config.prompt_injection_markers, 1):
            if any(unicodedata.normalize('NFKC', marker).casefold() in unicodedata.normalize('NFKC', text).casefold() for text in texts):
                return _finding(ViolationCode.PROMPT_INJECTION_SUSPECTED, 'FIN-R14', detail=f'命中注入特征 #{index}')
        return None


_ATTACHMENT_NAMES = dict(zip(AttachmentType, ('发票', '采购清单', '验收记录', '维保工单', '报价单', '预算审批', '合同', '事前审批', '业务凭证')))


def _invoice_key(value: str) -> str:
    return ''.join(unicodedata.normalize('NFKC', value).split()).upper()


def _finding(code: ViolationCode, rule_id: str, *, detail: str, **kwargs) -> ComplianceFinding:
    policy = VIOLATION_POLICIES[code]
    if policy.min_route is ApprovalRoute.NEEDS_SUPPLEMENT:
        kwargs.setdefault('supplement_request', '请核对并补正相关金额或凭证')
    return ComplianceFinding(finding_id=f'{code.value}:1', code=code, risk_level=policy.min_risk_level,
        source=FindingSource.RULE_ENGINE, min_route=policy.min_route, is_redline=policy.is_redline,
        title=detail, detail=detail, rule_id=rule_id,
        policy_section=(RULE_POLICY_SECTIONS | CONTRACT_POLICY_SECTIONS).get(code), **kwargs)


def _order_findings(findings: list[ComplianceFinding]) -> list[ComplianceFinding]:
    counts = Counter()
    result = []
    for finding in sorted(findings, key=lambda f: (-f.risk_level.rank, f.rule_id or '', f.related_line_nos)):
        counts[finding.code] += 1
        result.append(finding.model_copy(update={'finding_id': f'{finding.code.value}:{counts[finding.code]}'}))
    return result
