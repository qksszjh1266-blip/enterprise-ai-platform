import { clear, h } from '../core/dom.js';
import { errorOf, post } from '../core/api.js';
import { badge, empty, errorNotice, identityText, kv, metric, ms, notice, personaSelect, quickbar, section, table } from '../core/ui.js';

const EXPENSE = `单据类型：报销单
单据编号：EXP-20261105-001
店铺编码：ST_0101
申请人：store_clerk_01 | store_ops | store_clerk
提交时间：2026-11-05 10:00
报销事由：仓库日常打包耗材采购
发票：INV20261104001 | 增值税普通发票 | 2026-11-04 | 杭州示例包装材料有限公司 | 91330100MA12345678
明细：日常耗材 | 快递纸箱 | 2 | 件 | 100.00 | 0.13 | INV20261104001 | 2026-11-04
附件：发票、采购清单、验收记录
申报总额：226.00`;
const OVER_LIMIT = EXPENSE.replace('EXP-20261105-001', 'EXP-20261105-002').replace('| 2 | 件 | 100.00', '| 3 | 件 | 200.00').replace('申报总额：226.00', '申报总额：678.01');
const WITH_PII = EXPENSE.replace('EXP-20261105-001', 'EXP-20261105-003')
  .replace('仓库日常打包耗材采购', '打包耗材采购，经办人手机 13800138000，身份证 11010519491231002X，垫付卡 6222020200112233446')
  .replace('申报总额', '收款账户：6222020200112233446\n申报总额');
const CONTRACT = `单据类型：合同
合同编号：CTR-20261105-001
合同类型：物流配送
合同名称：华东区域仓储与干线物流服务合同
店铺编码：ST_0101
申请人：store_manager_01 | store_ops | store_manager
相对方：杭州示例仓配物流有限公司 | 91330100MA12345678 | 13800138000 | 6222020200112233446
标的额：80000.00
税率：0.13
账期天数：60
付款计划：0 | 20000.00 | 预付款
付款计划：60 | 60000.00 | 验收后尾款
付款条款：合同生效后支付预付款，账期 60 天内付款。
违约条款：我方违约责任不设上限，相对方违约金为合同额 5%。
其他条款：无
生效日期：2026-11-10
到期日期：2027-11-09
附件：合同
提交时间：2026-11-05 10:00`;
const CASES = [
  { label: '常规店铺报销合规放行', kind: 'positive', persona: 'finance_clerk', text: EXPENSE },
  { label: '超标金额触发 Decimal 规则拦截', kind: 'security', persona: 'finance_clerk', text: OVER_LIMIT },
  { label: '含手机号/身份证/银行卡触发 PII 自动脱敏与人工审批工单', kind: 'security', persona: 'finance_clerk', text: WITH_PII },
];
const ROUTE = { AUTO_APPROVE: ['good', '预审通过，进入常规审批'], NEEDS_SUPPLEMENT: ['warn', '退回补正'], ESCALATE_HUMAN: ['warn', '规则拦截，转人工复核'], REJECT: ['bad', '驳回'] };
const RISK = { LOW: 'info', MEDIUM: 'warn', HIGH: 'bad', CRITICAL: 'bad' };
const TIER = { store_manager: '运营主管', regional_manager: '区域经理', regional_manager_and_finance: '区域经理及财务', regional_head_and_finance_head: '区域负责人及财务负责人', legal_and_finance_head: '法务及财务负责人' };
const SOURCE = { rule_engine: 'Decimal 规则', model_review: '语义审查', system_guard: '系统兜底' };
const PII = { bank_card: '银行卡', id_card: '身份证', mobile_phone: '手机号' };
const STAGE = { redact: '脱敏', ledger_lookup: '台账查询', retrieve_policy: '制度检索', rule_check: '规则校验', model_review: '语义审查', route: '路由合成', escalate: '生成工单' };

function highlightOriginal(text, matches) {
  const chars = Array.from(text);
  const nodes = [];
  let cursor = 0;
  for (const m of matches) {
    nodes.push(chars.slice(cursor, m.start).join(''));
    nodes.push(h('mark', { class: 'pii', title: PII[m.pii_type] }, chars.slice(m.start, m.end).join('')));
    cursor = m.end;
  }
  nodes.push(chars.slice(cursor).join(''));
  return nodes;
}

function highlightRedacted(text) {
  return text.split(/(\[(?:银行卡|身份证|手机号)#\d+\])/).map((part, i) => (i % 2 ? h('mark', { class: 'mask' }, part) : part));
}

export function createFinance(ctx) {
  const state = { persona: 'finance_clerk', last: null };
  const input = h('textarea', { class: 'input mono', rows: 11, 'aria-label': '报销单或合同文本', placeholder: '粘贴报销单或合同文本，或点击上方用例填入' });
  const button = h('button', { class: 'btn primary', type: 'button', onclick: run }, '提交预审');
  const output = h('div', { style: 'display:grid;gap:12px' }, h('div', { class: 'card' }, empty('提交后在此对比脱敏前后文本，并展示规则命中与审批回执。')));
  const select = personaSelect(ctx.personas, 'finance', state.persona, (v) => { state.persona = v; renderDrawer(); });

  function pick(item) {
    state.persona = item.persona;
    select.value = item.persona;
    input.value = item.text;
    renderDrawer();
  }

  function compare(text, redaction) {
    const counts = Object.entries(redaction.counts);
    return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', null, '脱敏前后对比'),
      counts.length ? counts.map(([k, v]) => badge('warn', `${PII[k]} ${v}`)) : badge('good', '未检出隐私字段')),
      h('div', { class: 'compare' }, h('div', null, h('h4', null, '原文（仅当前页面可见，未回传）'), h('pre', null, highlightOriginal(text, redaction.matches))),
        h('div', null, h('h4', null, '脱敏后（模型、日志、工单、响应可见）'), h('pre', null, highlightRedacted(redaction.text)))));
  }

  function render(result, text) {
    const redaction = result.data?.redaction;
    const head = redaction ? compare(text, redaction) : null;
    if (!result.ok) return clear(output, errorNotice(result.status, errorOf(result)), head);
    const { response: r, ticket } = result.data;
    const [kind, label] = ROUTE[r.route];
    const rec = r.reconciliation;
    clear(output, head,
      h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', null, `预审结论 · ${r.reference_id}`), badge(kind, label), h('span', { class: 'spacer' }), h('span', { class: 'muted' }, `审批层级：${TIER[r.approver_tier]}`)),
        h('div', { class: 'metrics' }, metric('申报金额', rec.declared_total), metric('Decimal 核算', rec.computed_total), metric('偏差', rec.total_deviation), metric('定档金额', rec.approval_amount)),
        h('p', { style: 'white-space:pre-wrap;margin-top:10px' }, r.conclusion)),
      h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', null, '规则命中明细'), h('span', { class: 'muted' }, `${r.findings.length} 项`)),
        r.findings.length ? table(['风险', '编码', '规则', '来源', '说明', '金额偏差'], r.findings.map((f) => [badge(RISK[f.risk_level], f.risk_level), f.code, f.rule_id || '-', SOURCE[f.source], f.detail, f.amount_delta ?? '-'])) : notice('good', '全部 Decimal 规则与语义审查通过', '金额勾稽、发票占用、拆单窗口与制度引用校验均无异常。')),
      ticket ? h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', null, '人工审批工单回执'), badge('info', ticket.queue)),
        h('div', { class: 'metrics' }, metric('工单号', ticket.ticket_id), metric('风险等级', ticket.max_risk_level), metric('SLA', `${ticket.sla_hours} 小时`), metric('发起人', ticket.requested_by)),
        h('p', { class: 'muted', style: 'margin-top:8px;overflow-wrap:anywhere' }, ticket.summary)) : null);
  }

  async function run() {
    const text = input.value;
    if (!text.trim()) return;
    button.disabled = true;
    const result = await post('/api/console/finance/audit', { persona_id: state.persona, text });
    button.disabled = false;
    state.last = result;
    render(result, text);
    renderDrawer();
  }

  function renderDrawer() {
    const persona = ctx.personas.find((p) => p.persona_id === state.persona);
    const base = section('网关身份', kv([['人员', persona.label], ['可信头', identityText(persona), true]]));
    const result = state.last;
    if (!result) return ctx.drawer('财务预审', [base, empty('提交后展示勾稽、制度引用与阶段耗时。')]);
    if (!result.ok) return ctx.drawer('财务预审', [base, section('拦截详情', kv([['HTTP', result.status], ['错误码', errorOf(result).error_code, true], ['trace_id', result.data?.trace_id, true]]))]);
    const r = result.data.response;
    const rec = r.reconciliation;
    ctx.drawer(r.route, [
      base,
      section('追踪', kv([['trace_id', result.data.trace_id, true], ['语义审查', r.model_review_status, true], ['引用闭锁校验', r.citation_check_passed ? '通过' : '未通过'], ['总耗时', ms(r.latency_ms)]])),
      section('Decimal 勾稽', kv([['总额偏差', rec.total_deviation], ['税额校验', rec.tax_check_passed ? '通过' : '未通过'], ['重复发票', rec.has_duplicate_invoice ? '命中' : '无'], ['疑似拆单', rec.split_suspected ? '命中' : '无'], ['整单勾稽', rec.passed ? '通过' : '未通过']])),
      section('明细逐行', rec.line_checks.length ? kv(rec.line_checks.map((l) => [`第 ${l.line_no} 行 含税`, `${l.declared_amount_incl_tax} · ${l.amount_passed && l.tax_passed ? '一致' : '偏差'}`])) : h('span', { class: 'muted' }, '合同无明细行')),
      section('制度引用', r.citations.length ? r.citations.map((c) => h('div', { class: 'cite' }, h('strong', null, c.section_path), c.quote ? h('span', null, c.quote) : null, h('small', null, `${c.doc_id} · v${c.version}`))) : h('span', { class: 'muted' }, '无需引用')),
      section('阶段耗时', kv(Object.entries(r.stage_latency_ms).map(([k, v]) => [STAGE[k] || k, ms(v)]))),
    ]);
  }

  const root = h('div', { class: 'module' }, quickbar(CASES, pick),
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', null, '单据文本'), h('button', { class: 'link', type: 'button', onclick: () => { input.value = CONTRACT; state.persona = 'legal_counsel'; select.value = 'legal_counsel'; renderDrawer(); } }, '填入合同样例'), h('span', { class: 'spacer' }), h('label', { class: 'field', style: 'flex-direction:row;align-items:center;gap:8px;white-space:nowrap' }, '预审身份', select), button), input),
    output);
  return { root, renderDrawer };
}
