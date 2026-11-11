import { clear, h } from '../core/dom.js';
import { errorOf, post } from '../core/api.js';
import { badge, empty, errorNotice, kv, metric, ms, quickbar, section } from '../core/ui.js';

const DEPTS = { store_ops: ['clerk', 'store_manager'], supply_chain: ['warehouse', 'supply_manager', 'hq_supply_analyst', 'procurement_manager'], finance: ['finance', 'finance_manager', 'legal'] };
const DEPT_LABEL = { store_ops: '门店运营', supply_chain: '供应链', finance: '财务' };
const ROLE_LABEL = { clerk: '店员', store_manager: '店长', warehouse: '仓管员', supply_manager: '供应链经理', hq_supply_analyst: '总部分析师', procurement_manager: '采购经理', finance: '财务专员', finance_manager: '财务经理', legal: '法务' };
const CASES = [
  { label: '同部门设备排障召回', kind: 'positive', query: '制冰机不进水如何排查', dept: 'store_ops', roles: ['clerk'] },
  { label: '跨部门权限过滤演示', kind: 'security', query: '费用审批矩阵与发票报销要求', dept: 'store_ops', roles: ['clerk'] },
  { label: '受限文档角色隔离演示', kind: 'security', query: '采购合同价格与供应商银行账户', dept: 'supply_chain', roles: ['warehouse'] },
];
const RRF_MAX = 2 / 61;
const BRANCH = { vector: '向量', keyword: '关键词' };

function bar(label, ratio, text, tone) {
  return h('div', { class: 'bar-row' }, h('span', null, label), h('div', { class: 'bar-track' }, h('div', { class: `bar-fill ${tone}`, style: `width:${Math.max(0, Math.min(100, ratio * 100)).toFixed(1)}%` })), h('span', null, text));
}

export function createKnowledge(ctx) {
  const state = { last: null, dept: 'store_ops', roles: new Set(['clerk']), params: null };
  const query = h('input', { class: 'input', placeholder: '输入检索词', 'aria-label': '检索词', onkeydown: (e) => { if (e.key === 'Enter') run(); } });
  const dept = h('select', { class: 'input', 'aria-label': '部门', onchange: (e) => { state.dept = e.target.value; state.roles = new Set([DEPTS[state.dept][0]]); renderRoles(); } }, Object.keys(DEPTS).map((d) => h('option', { value: d }, DEPT_LABEL[d])));
  const roles = h('div', { class: 'checks', role: 'group', 'aria-label': '角色权限' });
  const rerank = h('input', { type: 'checkbox', checked: true });
  const button = h('button', { class: 'btn primary', type: 'button', onclick: run }, '检索');
  const output = h('div', { style: 'display:grid;gap:12px' }, h('div', { class: 'card' }, empty('切换部门与角色，观察同一检索词的权限过滤效果。')));

  function renderRoles() {
    clear(roles, DEPTS[state.dept].map((role) => h('label', null, h('input', { type: 'checkbox', checked: state.roles.has(role), onchange: (e) => { if (e.target.checked) state.roles.add(role); else state.roles.delete(role); } }), ROLE_LABEL[role])));
  }

  function pick(item) {
    query.value = item.query;
    state.dept = item.dept;
    dept.value = item.dept;
    state.roles = new Set(item.roles);
    renderRoles();
    query.focus();
  }

  function resultCard(item, index) {
    const maxRerank = 1;
    const hit = item.children.filter((c) => c.vector_rank || c.keyword_rank);
    return h('div', { class: 'result' },
      h('div', { class: 'result-head' }, h('span', { class: 'result-rank' }, index + 1), h('strong', null, item.section_path || item.doc_id), item.source_branches.map((b) => badge('plain', BRANCH[b])), h('span', { class: 'muted' }, `${item.doc_id} · v${item.version}`)),
      h('div', { class: 'bars' }, bar('RRF 融合', item.rrf_score / RRF_MAX, item.rrf_score.toFixed(4), ''), item.rerank_score === null ? null : bar('精排得分', item.rerank_score / maxRerank, item.rerank_score.toFixed(3), 's3')),
      h('div', { class: 'hierarchy' },
        h('div', null, h('h4', null, '父块上下文（回表返回给智能体）'), h('div', { class: 'parent-text' }, item.full_content)),
        h('div', null, h('h4', null, `子块索引（${hit.length}/${item.children.length} 命中）`), h('div', { class: 'child-list' }, item.children.map((c) => h('div', { class: `child ${c.keyword_rank ? 'hit' : ''}` },
          h('div', { class: 'child-meta' }, h('span', null, `#${c.chunk_index}`), c.vector_rank ? h('span', null, `向量第 ${c.vector_rank} 名 ${c.vector_score.toFixed(3)}`) : null, c.keyword_rank ? h('span', null, `关键词第 ${c.keyword_rank} 名 ${c.keyword_score.toFixed(0)}`) : null), c.text))))));
  }

  function render(result) {
    if (!result.ok) return clear(output, errorNotice(result.status, errorOf(result)));
    const { response: r, blocked_matches: blocked, visible_documents: visible, total_documents: total } = result.data;
    clear(output,
      h('div', { class: 'metrics' }, metric('ACL 可见文档', `${visible} / ${total}`), metric('向量候选', r.candidate_counts.vector), metric('关键词候选', r.candidate_counts.keyword), metric('融合父块', r.candidate_counts.fused), metric('端到端耗时', ms(r.latency_ms))),
      h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', null, '跨部门权限过滤效果'), blocked.length ? badge('bad', `已前置过滤 ${blocked.length} 篇相关文档`) : badge('good', '无相关受限文档')),
        blocked.length ? h('div', { class: 'acl' }, blocked.map((b) => h('div', { class: 'acl-row' }, h('span', null, `${b.title} · ${DEPT_LABEL[b.dept_id]} · 需 ${b.allowed_roles.map((x) => ROLE_LABEL[x] || x).join('/')}`), badge('bad', b.reason)))) : h('p', { class: 'muted' }, '当前检索词在不可见文档中没有明显匹配。过滤发生在召回之前，不可见文档的正文不会进入候选。')),
      h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', null, '混合检索结果'), r.degraded ? badge('warn', r.degraded_reason) : badge('good', '双路正常')),
        r.results.length ? h('div', { style: 'display:grid;gap:10px' }, r.results.map(resultCard)) : empty('当前身份无可见文档或无匹配结果。')));
  }

  async function run() {
    const text = query.value.trim();
    const selected = [...state.roles];
    if (!text) return;
    if (!selected.length) return clear(output, h('div', { class: 'card' }, empty('请至少选择一个角色。')));
    button.disabled = true;
    state.params = { dept: state.dept, roles: selected };
    const result = await post('/api/console/knowledge/search', { query: text, dept_id: state.dept, roles: selected, enable_rerank: rerank.checked });
    button.disabled = false;
    state.last = result;
    render(result);
    renderDrawer();
  }

  function renderDrawer() {
    const result = state.last;
    if (!result?.ok) return ctx.drawer('知识中台', [empty('检索后展示 ACL 台账与阶段耗时。')]);
    const { response: r, acl } = result.data;
    ctx.drawer('知识中台', [
      section('检索身份', kv([['部门', state.params.dept, true], ['角色', state.params.roles.join(','), true]])),
      section('阶段耗时', kv(Object.entries(r.stage_latency_ms).map(([k, v]) => [k, ms(v)]))),
      section('DegradedReason', r.degraded_reasons.length ? h('div', { class: 'chips' }, r.degraded_reasons.map((x) => badge('warn', x))) : badge('good', '无降级')),
      section('ACL 台账', h('div', { class: 'acl' }, acl.map((a) => h('div', { class: 'acl-row' }, h('span', { title: a.title }, a.doc_id), a.visible ? badge('good', '可见') : badge('bad', a.reason))))),
    ]);
  }

  renderRoles();
  const root = h('div', { class: 'module' }, quickbar(CASES, pick),
    h('div', { class: 'card' }, h('div', { class: 'row' }, h('label', { class: 'field grow' }, '检索词', query), h('label', { class: 'field', style: 'min-width:130px' }, '部门', dept), h('div', { class: 'field' }, '角色权限', roles), h('label', { class: 'field' }, '精排', h('span', { class: 'checks' }, rerank)), button)),
    output);
  return { root, renderDrawer };
}
