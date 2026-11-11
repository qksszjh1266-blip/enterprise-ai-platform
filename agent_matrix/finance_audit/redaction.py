"""PII 确定性脱敏。

在**送入大模型、写入日志、写入工单、返回响应**之前，对银行卡号、身份证号、手机号执行确定性掩码，
保证原始隐私零外泄。纯 CPU、无 I/O、无随机性、不读系统时钟。

识别规则（按优先级，先匹配者占位，后续规则不得与已占位区间重叠）：

    类型            模式（先 NFKC 归一化全角数字；允许数字间单个空格或连字符分隔）          二次校验
    ID_CARD         18 位：6 位地区码 + 8 位出生日期（真实存在的日期）+ 3 位顺序码 + 校验位 [0-9Xx]   GB 11643-1999 MOD 11-2
    BANK_CARD       16~19 位数字                                                       Luhn
    MOBILE_PHONE    (+86|86)? 1[3-9] 加 9 位数字                                       号段前缀 1[3-9]

    - 边界：命中串前后不得紧邻数字或 ASCII 字母（避免切断发票号、统一社会信用代码等更长的标识符）。
    - 未通过二次校验的数字串**不**脱敏（例如 20 位全电发票号、18 位纯数字但校验位错误的串）；
      结构化字段（InvoiceInfo.invoice_no / seller_tax_id、CounterpartyInfo.unified_credit_code）不经过本脱敏器。

占位符（不保留任何原文数字，含末四位）：

    [银行卡#n]  [身份证#n]  [手机号#n]

    - n 按 RedactionContext 内**首次出现顺序**从 1 编号；同一上下文中相同原值（去分隔符后）复用同一编号，
      使模型仍能判断"两处是同一张卡"，但无法得知卡号。
    - 同一输入 + 同一上下文初始状态 ⇒ 输出逐字节相同（确定性，可回归）。

结构化 PII 字段的处置（redact_expense / redact_contract）：
    ExpenseAuditSubmission.payee_bank_account、CounterpartyInfo.contact_phone / bank_account
    **不进入**视图（仅以 has_payee_bank_account 与 pii_counts 体现），且计入 pii_counts。

出口守卫（ensure_clean）：对任何即将离开受信边界的文本再扫描一次（同一识别规则），命中即抛 PiiLeakError
（fail-closed，宁可阻断也不外泄）；占位符本身不会被再次命中。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date
from agent_matrix.finance_audit.errors import PiiLeakError
from agent_matrix.finance_audit.models import RedactedExpenseLine

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from agent_matrix.finance_audit.models import (
    ContractAuditSubmission,
    ExpenseAuditSubmission,
    PiiMatch,
    PiiType,
    RedactedContractView,
    RedactedExpenseView,
    RedactionResult,
)

PLACEHOLDER_LABELS: dict[PiiType, str] = {
    PiiType.BANK_CARD: "银行卡",
    PiiType.ID_CARD: "身份证",
    PiiType.MOBILE_PHONE: "手机号",
}
"""占位符标签：f"[{PLACEHOLDER_LABELS[t]}#{n}]"。修改即破坏回归基线，须同步脱敏测试与评测报告。"""

DETECTION_ORDER: tuple[PiiType, ...] = (PiiType.ID_CARD, PiiType.BANK_CARD, PiiType.MOBILE_PHONE)
"""识别优先级（18 位身份证与 18 位银行卡长度重叠，身份证校验更严格，故优先）。"""

RedactionSink = str
"""出口位置标识：'model_input' / 'log' / 'response' / 'ticket' / 'retrieval_query'，进入 PiiLeakError 消息。"""


@dataclass
class RedactionContext:
    """一次预审内共享的占位符编号表（同一原值复用同一编号）。

    registry 的键为 (PiiType, 原值去分隔符后的 sha256 十六进制摘要)——上下文对象本身也不保存原文。
    """

    registry: dict[tuple[PiiType, str], int] = field(default_factory=dict)
    next_index: dict[PiiType, int] = field(default_factory=dict)

    def placeholder_for(self, pii_type: PiiType, normalized_value: str) -> str:
        """返回该原值的占位符；首次出现时分配下一个编号。"""
        digest = hashlib.sha256(normalized_value.encode()).hexdigest()
        key = (pii_type, digest)
        if key not in self.registry:
            self.next_index[pii_type] = self.next_index.get(pii_type, 0) + 1
            self.registry[key] = self.next_index[pii_type]
        return f"[{PLACEHOLDER_LABELS[pii_type]}#{self.registry[key]}]"

    def counts(self) -> dict[PiiType, int]:
        """各类型已分配的不同原值数量。"""
        return dict(self.next_index)


class PiiRedactor:
    """银行卡 / 身份证 / 手机号确定性脱敏器（线程安全：实例无可变状态，状态全部在 RedactionContext 中）。"""

    def __init__(self, *, enabled_types: frozenset[PiiType] = frozenset(PiiType)) -> None:
        """预编译三类正则；enabled_types 仅供单元测试隔离单一类型，生产必须启用全部类型。"""
        self.enabled_types = enabled_types
        boundary_left, boundary_right = r"(?<![0-9A-Za-z.])", r"(?![0-9A-Za-z.])"
        self._patterns = {
            PiiType.ID_CARD: re.compile(boundary_left + r"[0-9](?:[ -]?[0-9]){16}[ -]?[0-9Xx]" + boundary_right),
            PiiType.BANK_CARD: re.compile(boundary_left + r"[0-9](?:[ -]?[0-9]){15,18}" + boundary_right),
            PiiType.MOBILE_PHONE: re.compile(boundary_left + r"(?:\+?86[ -]?)?1[ -]?[3-9](?:[ -]?[0-9]){9}" + boundary_right),
        }

    # ------------------------------------------------------------------
    # 文本级
    # ------------------------------------------------------------------

    def redact_text(self, text: str, *, context: RedactionContext | None = None) -> RedactionResult:
        """按模块文档规则脱敏单段文本。

        - context 为 None 时使用一次性新上下文。
        - matches 按 start 升序、互不重叠；start / end 为**原文**下标。
        - 空串返回 RedactionResult(text="")。
        """
        context = context if context is not None else RedactionContext()
        normalized = self._normalize(text)
        found = []
        for kind in DETECTION_ORDER:
            if kind not in self.enabled_types:
                continue
            for match in self._patterns[kind].finditer(normalized):
                start, end = match.span()
                # Do not take a suffix/prefix of a longer, separated numeric identifier.
                if start >= 2 and normalized[start-1] in ' -' and normalized[start-2].isdigit():
                    continue
                if end + 1 < len(normalized) and normalized[end] in ' -' and normalized[end+1].isdigit():
                    continue
                if any(start < b and end > a for a, b, _, _ in found):
                    continue
                value = re.sub(r"[ +\-]", "", match.group()).upper()
                if kind is PiiType.ID_CARD and not self._id_card_valid(value):
                    continue
                if kind is PiiType.BANK_CARD and not self._luhn_valid(value):
                    continue
                if kind is PiiType.MOBILE_PHONE and value.startswith('86'):
                    value = value[2:]
                found.append((start, end, kind, value))
        parts, matches, cursor = [], [], 0
        for start, end, kind, value in sorted(found):
            placeholder = context.placeholder_for(kind, value)
            parts.extend((text[cursor:start], placeholder))
            matches.append(PiiMatch(pii_type=kind, start=start, end=end, placeholder=placeholder))
            cursor = end
        parts.append(text[cursor:])
        return RedactionResult(text=''.join(parts), matches=matches, counts=context.counts())

    def redact_fields(
        self, fields: Mapping[str, str], *, context: RedactionContext | None = None
    ) -> dict[str, RedactionResult]:
        """按 fields 的迭代顺序逐个脱敏，共享同一上下文（跨字段编号一致）。"""
        context = context if context is not None else RedactionContext()
        return {key: self.redact_text(value, context=context) for key, value in fields.items()}

    def contains_pii(self, text: str) -> bool:
        """是否命中任一启用类型（与 redact_text 同一识别规则）。"""
        return bool(self.redact_text(text).matches)

    def ensure_clean(self, text: str, *, sink: RedactionSink) -> None:
        """出口守卫：命中即抛 PiiLeakError(f"{sink} 出口检测到 {pii_type.value}")；消息不得包含命中原文。"""
        matches = self.redact_text(text).matches
        if matches:
            raise PiiLeakError(f"{sink} 出口检测到 {matches[0].pii_type.value}")

    # ------------------------------------------------------------------
    # 单据级
    # ------------------------------------------------------------------

    def redact_expense(self, submission: ExpenseAuditSubmission) -> RedactedExpenseView:
        """构造报销脱敏视图。

        - 共享一个 RedactionContext，顺序：reason → lines[*].description（按 line_no）。
        - payee_bank_account 非空 → has_payee_bank_account=True，并按其值登记到上下文（计入 pii_counts，
          若事由中出现同一卡号则复用同一编号），但不出现在视图任何文本字段中。
        - applicant.user_id / dept_id 不进入视图；applicant_roles 保留（自审 / 升级规则需要语义）。
        """
        context = RedactionContext()
        reason = self.redact_text(submission.reason, context=context).text
        lines = [RedactedExpenseLine(
            line_no=line.line_no, category=line.category,
            description=self.redact_text(line.description, context=context).text,
            quantity=line.quantity, unit=self.redact_text(line.unit, context=context).text,
            amount_incl_tax=line.amount_incl_tax, tax_rate=line.tax_rate,
        ) for line in submission.lines]
        if submission.payee_bank_account:
            value = re.sub(r"[ +\-]", "", self._normalize(submission.payee_bank_account))
            context.placeholder_for(PiiType.BANK_CARD, value)
        return RedactedExpenseView(
            claim_id=submission.claim_id, store_code=submission.store_code,
            applicant_roles=submission.applicant.roles,
            categories=list(dict.fromkeys(line.category for line in submission.lines)),
            reason=reason, lines=lines, declared_total=submission.declared_total,
            invoice_count=len(submission.invoices), attachments=submission.attachments,
            has_pre_approval=bool(submission.pre_approval_ref),
            has_payee_bank_account=bool(submission.payee_bank_account), pii_counts=context.counts(),
        )

    def redact_contract(self, submission: ContractAuditSubmission) -> RedactedContractView:
        """构造合同脱敏视图。

        - 共享上下文，顺序：counterparty.name → title → payment_terms_text → breach_clause_text → other_key_clauses。
        - counterparty.contact_phone / bank_account 只登记计数，不进入视图。
        """
        context = RedactionContext()
        fields = self.redact_fields({
            'counterparty_name': submission.counterparty.name, 'title': submission.title,
            'payment_terms_text': submission.payment_terms_text,
            'breach_clause_text': submission.breach_clause_text,
            'other_key_clauses': submission.other_key_clauses,
        }, context=context)
        schedule = [p.model_copy(update={'description': self.redact_text(p.description, context=context).text})
                    for p in submission.payment_schedule]
        for kind, raw in ((PiiType.MOBILE_PHONE, submission.counterparty.contact_phone),
                          (PiiType.BANK_CARD, submission.counterparty.bank_account)):
            if raw:
                value = re.sub(r"[ +\-]", "", self._normalize(raw))
                if kind is PiiType.MOBILE_PHONE and value.startswith('86'):
                    value = value[2:]
                context.placeholder_for(kind, value)
        return RedactedContractView(
            contract_id=submission.contract_id, contract_type=submission.contract_type,
            **{k: v.text for k, v in fields.items()},
            counterparty_is_franchisee=submission.counterparty.is_franchisee,
            subject_amount=submission.subject_amount, tax_rate=submission.tax_rate,
            payment_term_days=submission.payment_term_days, payment_schedule=schedule,
            effective_date=submission.effective_date, expiry_date=submission.expiry_date,
            pii_counts=context.counts(),
        )

    # ------------------------------------------------------------------
    # 校验算法（纯函数）
    # ------------------------------------------------------------------

    @staticmethod
    def _luhn_valid(digits: str) -> bool:
        """Luhn 校验（digits 只含 0-9）。"""
        if not re.fullmatch(r"[0-9]{16,19}", digits):
            return False
        total = 0
        for i, digit in enumerate(reversed(digits)):
            n = int(digit) * (2 if i % 2 else 1)
            total += n - 9 if n > 9 else n
        return total % 10 == 0

    @staticmethod
    def _id_card_valid(id18: str) -> bool:
        """GB 11643-1999：前 17 位加权（7 9 10 5 8 4 2 1 6 3 7 9 10 5 8 4 2）求和 mod 11 → 校验位
        '10X98765432'[mod]；同时校验出生日期为真实日期（不校验地区码字典）。"""
        if not re.fullmatch(r"[0-9]{17}[0-9Xx]", id18):
            return False
        try:
            date(int(id18[6:10]), int(id18[10:12]), int(id18[12:14]))
        except ValueError:
            return False
        weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
        return '10X98765432'[sum(int(d) * w for d, w in zip(id18, weights)) % 11] == id18[-1].upper()

    @staticmethod
    def _normalize(text: str) -> str:
        """NFKC 归一化（全角数字 → 半角）；必须保持字符下标一一对应（NFKC 对 CJK 与全角数字均为 1:1）。"""
        # Preserve offsets even for compatibility characters that expand under NFKC.
        return ''.join(n if len(n := unicodedata.normalize('NFKC', ch)) == 1 else ch for ch in text)


class PiiLoggingFilter(logging.Filter):
    """日志出口脱敏过滤器：挂到 agent_matrix.finance_audit 命名空间的 logger / handler 上。

    filter() 对 record.getMessage() 的结果执行 redact_text，并把 record.msg 替换为脱敏文本、record.args 置空；
    record.exc_info 存在时清除 exc_text 并以 "[exc_info suppressed by PiiLoggingFilter]" 代替（异常文本可能含单据原文）。
    永远返回 True（脱敏后放行，不丢日志）。
    """

    def __init__(self, redactor: PiiRedactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        clean = self.redactor.redact_text(record.getMessage()).text
        self.redactor.ensure_clean(clean, sink='log')
        record.msg, record.args = clean, ()
        if record.exc_info or record.exc_text:
            record.exc_info = None
            record.exc_text = '[exc_info suppressed by PiiLoggingFilter]'
        record.stack_info = None
        return True
