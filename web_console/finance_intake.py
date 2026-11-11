"""把工作台文本单据解析为财务预审提交对象。

金额全程使用 Decimal；解析错误只回显字段名，绝不回显字段值（字段值可能含隐私原文）。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from agent_matrix.finance_audit.models import (
    AttachmentType, ContractAuditSubmission, ContractType, ExpenseAuditSubmission, ExpenseCategory, InvoiceType,
)

CENT = Decimal("0.01")
TZ = ZoneInfo("Asia/Shanghai")
CATEGORIES = {"日常耗材": ExpenseCategory.DAILY_CONSUMABLES, "设备维修": ExpenseCategory.EQUIPMENT_REPAIR,
              "装修改造": ExpenseCategory.RENOVATION, "差旅住宿": ExpenseCategory.TRAVEL_LODGING,
              "市内交通": ExpenseCategory.LOCAL_TRANSPORT, "其他": ExpenseCategory.OTHER}
INVOICE_TYPES = {"增值税专用发票": InvoiceType.VAT_SPECIAL, "增值税普通发票": InvoiceType.VAT_NORMAL,
                 "全电发票": InvoiceType.E_INVOICE_FULL, "其他": InvoiceType.OTHER}
ATTACHMENTS = {"发票": AttachmentType.INVOICE, "采购清单": AttachmentType.PURCHASE_LIST,
               "验收记录": AttachmentType.ACCEPTANCE_RECORD, "维保工单": AttachmentType.MAINTENANCE_TICKET,
               "报价单": AttachmentType.QUOTATION, "预算审批": AttachmentType.BUDGET_APPROVAL,
               "合同": AttachmentType.CONTRACT, "事前审批": AttachmentType.PRE_APPROVAL,
               "业务凭证": AttachmentType.BUSINESS_VOUCHER}
CONTRACT_TYPES = {"商品采购": ContractType.FRANCHISE_PROCUREMENT, "设备采购": ContractType.EQUIPMENT_PURCHASE,
                  "仓库租赁": ContractType.STORE_LEASE, "装修工程": ContractType.RENOVATION_SERVICE,
                  "物流配送": ContractType.LOGISTICS_SERVICE, "其他": ContractType.OTHER}


class IntakeError(ValueError):
    """单据文本无法解析；message 不含字段值。"""


def _fields(text: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = re.match(r"\s*([^:：]{1,12})[:：]\s*(.*)$", line)
        if match:
            fields.setdefault(match.group(1).strip(), []).append(match.group(2).strip())
    return fields


def _one(fields: dict[str, list[str]], key: str, *, required: bool = True, default: str = "") -> str:
    value = fields.get(key, [""])[0]
    if required and not value:
        raise IntakeError(f"缺少字段：{key}")
    return value or default


def _cells(value: str, key: str, count: int) -> list[str]:
    cells = [c.strip() for c in value.split("|")]
    if len(cells) != count or not all(cells):
        raise IntakeError(f"字段「{key}」应为 {count} 段，并以竖线分隔")
    return cells


def _money(value: str, key: str) -> Decimal:
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        raise IntakeError(f"字段「{key}」不是合法金额") from None


def _date(value: str, key: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise IntakeError(f"字段「{key}」应为 YYYY-MM-DD 日期") from None


def _submitted_at(fields: dict[str, list[str]]) -> datetime:
    try:
        return datetime.strptime(_one(fields, "提交时间"), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        raise IntakeError("字段「提交时间」应为 YYYY-MM-DD HH:MM") from None


def _lookup(table: dict, value: str, key: str):
    if value not in table:
        raise IntakeError(f"字段「{key}」取值应为：{'、'.join(table)}")
    return table[value]


def _applicant(fields: dict[str, list[str]]) -> dict[str, object]:
    user_id, dept_id, roles = _cells(_one(fields, "申请人"), "申请人", 3)
    return {"user_id": user_id, "dept_id": dept_id, "roles": [r.strip() for r in roles.split(",") if r.strip()]}


def _attachments(fields: dict[str, list[str]]) -> list[AttachmentType]:
    names = [n for n in re.split(r"[、,，\s]+", _one(fields, "附件", required=False)) if n]
    return [_lookup(ATTACHMENTS, n, "附件") for n in names]


def _validated(model, payload):
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        locations = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise IntakeError("单据字段未通过校验：" + "、".join(locations)) from None


def _parse_expense(fields: dict[str, list[str]]) -> ExpenseAuditSubmission:
    lines, totals = [], {}
    for number, raw in enumerate(fields.get("明细", []), 1):
        category, description, quantity, unit, price, rate, invoice_no, occurred = _cells(raw, "明细", 8)
        qty, unit_price, tax_rate = _money(quantity, "明细数量"), _money(price, "明细单价"), _money(rate, "明细税率")
        excl = (qty * unit_price).quantize(CENT, ROUND_HALF_UP)
        tax = (excl * tax_rate).quantize(CENT, ROUND_HALF_UP)
        lines.append({"line_no": number, "category": _lookup(CATEGORIES, category, "明细类别"), "description": description,
                      "invoice_no": invoice_no, "occurred_on": _date(occurred, "明细发生日"), "quantity": qty, "unit": unit,
                      "unit_price": unit_price, "amount_excl_tax": excl, "tax_rate": tax_rate, "tax_amount": tax,
                      "amount_incl_tax": excl + tax})
        sums = totals.setdefault(invoice_no, [Decimal("0"), Decimal("0")])
        sums[0] += excl
        sums[1] += tax
    if not lines:
        raise IntakeError("缺少字段：明细")
    invoices = []
    for raw in fields.get("发票", []):
        number, kind, issued, seller, tax_id = _cells(raw, "发票", 5)
        excl, tax = totals.get(number, [Decimal("0"), Decimal("0")])
        invoices.append({"invoice_no": number, "invoice_type": _lookup(INVOICE_TYPES, kind, "发票类型"),
                         "issue_date": _date(issued, "开票日期"), "seller_name": seller, "seller_tax_id": tax_id,
                         "amount_excl_tax": excl, "tax_amount": tax, "amount_incl_tax": excl + tax})
    return _validated(ExpenseAuditSubmission, {
        "subject_type": "expense", "claim_id": _one(fields, "单据编号"), "store_code": _one(fields, "店铺编码"),
        "applicant": _applicant(fields), "reason": _one(fields, "报销事由"), "lines": lines, "invoices": invoices,
        "declared_total": _money(_one(fields, "申报总额"), "申报总额"), "attachments": _attachments(fields),
        "pre_approval_ref": _one(fields, "事前审批单", required=False) or None,
        "payee_bank_account": _one(fields, "收款账户", required=False) or None, "submitted_at": _submitted_at(fields)})


def _parse_contract(fields: dict[str, list[str]]) -> ContractAuditSubmission:
    name, credit_code, phone, account = _cells(_one(fields, "相对方"), "相对方", 4)
    schedule = []
    for number, raw in enumerate(fields.get("付款计划", []), 1):
        due, amount, description = _cells(raw, "付款计划", 3)
        if not due.isdigit():
            raise IntakeError("字段「付款计划」的天数应为整数")
        schedule.append({"seq": number, "due_in_days": int(due), "amount": _money(amount, "付款计划金额"), "description": description})
    term = _one(fields, "账期天数")
    if not term.isdigit():
        raise IntakeError("字段「账期天数」应为整数")
    rate = _one(fields, "税率", required=False)
    return _validated(ContractAuditSubmission, {
        "subject_type": "contract", "contract_id": _one(fields, "合同编号"),
        "contract_type": _lookup(CONTRACT_TYPES, _one(fields, "合同类型"), "合同类型"), "title": _one(fields, "合同名称"),
        "store_code": _one(fields, "店铺编码", required=False) or None, "applicant": _applicant(fields),
        "counterparty": {"name": name, "unified_credit_code": credit_code, "contact_phone": phone, "bank_account": account},
        "subject_amount": _money(_one(fields, "标的额"), "标的额"), "tax_rate": _money(rate, "税率") if rate else None,
        "payment_term_days": int(term), "payment_schedule": schedule, "payment_terms_text": _one(fields, "付款条款"),
        "breach_clause_text": _one(fields, "违约条款", required=False), "other_key_clauses": _one(fields, "其他条款", required=False),
        "effective_date": _date(_one(fields, "生效日期"), "生效日期"), "expiry_date": _date(_one(fields, "到期日期"), "到期日期"),
        "attachments": _attachments(fields), "submitted_at": _submitted_at(fields)})


def parse_submission(text: str) -> ExpenseAuditSubmission | ContractAuditSubmission:
    """按「单据类型」分派解析；类型缺失或字段非法抛 IntakeError。"""
    fields = _fields(text)
    kind = _one(fields, "单据类型")
    if kind == "报销单":
        return _parse_expense(fields)
    if kind == "合同":
        return _parse_contract(fields)
    raise IntakeError("字段「单据类型」取值应为：报销单、合同")
