import { clear, h } from '../core/dom.js';
import { errorOf, post } from '../core/api.js';
import { badge, empty, errorNotice, identityText, kv, ms, notice, personaSelect, quickbar, section } from '../core/ui.js';

const CASES = [
  { label: '标准饮品制作查询', kind: 'positive', persona: 'store_clerk', messages: ['鲜柠檬茶 500ml 的标准配方和操作要求是什么？'] },
  { label: '制冰机不制冰多轮排障', kind: 'positive', persona: 'store_clerk', messages: ['制冰机不制冰了，应该怎么排查？', '现象是不进水'] },
  { label: '排障超限自动转人工报修工单', kind: 'security', persona: 'store_clerk', messages: ['店里的机器出问题了', '是制冰机', '不进水，设备编号 ICE-008'] },
];
const STOP = {
  answered: ['good', '已回答'], clarification_requested: ['info', '追问补槽'], escalated: ['warn', '已转人工报修'],
  refused: ['bad', '拒答'], max_steps_reached: ['warn', '步数超限兜底'], retrieval_unavailable: ['warn', '知识库不可用'],
};
const STEP = { intent_slot: '意图与槽位', decide: '动作决策', clarify: '追问', retrieve: '混合检索', generate: '生成回答', verify_citations: '引用核验', escalate: '提交工单', refuse: '拒答', summarize: '摘要压缩', persist: '持久化' };
const SLOTS = { equipment_model: '设备类型', equipment_id: '设备编号', fault_symptom: '故障现象', attempted_checks: '已做检查', drink_name: '饮品名称' };

export function createSop(ctx) {
  const state = { persona: 'store_clerk', sessionId: null, ownerPersona: null, last: null, script: [], step: 0, busy: false };
  const personaOf = () => ctx.personas.find((p) => p.persona_id === state.persona);

  const log = h('div', { class: 'chat-log', 'aria-live': 'polite' }, empty('选择上方用例，或直接提问门店饮品与设备问题。'));
  const input = h('textarea', { class: 'input', rows: 2, placeholder: '输入门店问题，Enter 发送，Shift+Enter 换行', 'aria-label': '门店问题',
    onkeydown: (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } } });
  const sendButton = h('button', { class: 'btn primary', type: 'button', onclick: () => send() }, '发送');
  const scriptBar = h('div', { class: 'chat-script', hidden: true });
  const sessionLabel = h('span', { class: 'muted' }, '新会话');
  const select = personaSelect(ctx.personas, 'sop', state.persona, (value) => {
    state.persona = value;
    if (state.sessionId && state.ownerPersona !== value) pushNotice(notice('warn', '身份已切换，当前会话仍绑定原身份', '继续发送将被会话归属校验拦截（SESSION_FORBIDDEN），可点击重置会话。'));
    renderDrawer();
  });

  function pushNotice(node) { log.append(h('div', null, node)); log.scrollTop = log.scrollHeight; }
  function pushBubble(role, text, meta) {
    if (log.querySelector('.empty')) clear(log);
    log.append(h('div', { class: `bubble-row ${role}` }, h('div', { class: 'bubble' }, text), meta ? h('div', { class: 'bubble-meta' }, meta.filter(Boolean).map((m) => (m instanceof Node ? m : h('span', null, m)))) : null));
    log.scrollTop = log.scrollHeight;
  }

  function renderScript() {
    scriptBar.hidden = state.script.length < 2;
    if (scriptBar.hidden) return;
    const finished = state.step >= state.script.length;
    clear(scriptBar, h('span', null, '演练脚本'), state.script.map((_, i) => h('span', { class: `step-pill ${i < state.step ? 'done' : i === state.step ? 'now' : ''}` }, `第 ${i + 1} 轮`)),
      finished ? h('span', { class: 'muted' }, '脚本已完成') : h('button', { class: 'link', type: 'button', onclick: autoplay }, '自动演练剩余轮次'));
  }

  async function reset() {
    if (state.sessionId) await post('/api/console/sop/reset', { persona_id: state.ownerPersona, session_id: state.sessionId });
    Object.assign(state, { sessionId: null, ownerPersona: null, last: null, script: [], step: 0 });
    clear(log, empty('会话已重置。'));
    input.value = '';
    sessionLabel.textContent = '新会话';
    renderScript();
    renderDrawer();
  }

  async function pick(item) {
    await reset();
    state.persona = item.persona;
    select.value = item.persona;
    state.script = item.messages;
    input.value = item.messages[0];
    input.focus();
    renderScript();
    renderDrawer();
  }

  async function send() {
    const message = input.value.trim();
    if (!message || state.busy) return;
    state.busy = true;
    sendButton.disabled = true;
    pushBubble('user', message);
    input.value = '';
    const result = await post('/api/console/sop/turn', { persona_id: state.persona, session_id: state.sessionId, message });
    state.busy = false;
    sendButton.disabled = false;
    if (!result.ok) {
      state.last = { error: errorOf(result), status: result.status, trace_id: result.data?.trace_id };
      pushNotice(errorNotice(result.status, state.last.error));
      renderDrawer();
      return false;
    }
    const { response } = result.data;
    state.last = result.data;
    state.sessionId = response.session_id;
    state.ownerPersona = state.ownerPersona || state.persona;
    sessionLabel.textContent = `会话 ${response.session_id.slice(0, 8)} · 第 ${response.turn_index} 轮`;
    const [kind, text] = STOP[response.stop_reason];
    pushBubble('bot', response.answer, [badge(kind, text), response.citations.length ? `${response.citations.length} 条溯源引用` : null,
      response.ticket_receipt ? `工单 ${response.ticket_receipt.ticket_id}` : null, ms(response.latency_ms)]);
    if (state.script.length && state.step < state.script.length) {
      state.step += 1;
      input.value = state.script[state.step] || '';
      renderScript();
    }
    renderDrawer();
    return true;
  }

  async function autoplay() {
    while (state.step < state.script.length && input.value) {
      if (!(await send())) break;
    }
  }

  function renderDrawer() {
    const persona = personaOf();
    const base = section('网关身份', kv([['人员', persona.label], ['可信头', identityText(persona), true]]));
    if (!state.last) return ctx.drawer('门店 SOP', [base, empty('发送消息后展示引用、槽位与状态轨迹。')]);
    if (state.last.error) {
      return ctx.drawer('门店 SOP', [base, section('拦截详情', kv([['HTTP', state.last.status], ['错误码', state.last.error.error_code, true], ['trace_id', state.last.trace_id, true]]))]);
    }
    const { response: r, session, trace_id: traceId } = state.last;
    const slots = session?.slots || {};
    ctx.drawer(`第 ${r.turn_index} 轮 · ${r.steps_used} 步`, [
      base,
      section('追踪', kv([['trace_id', traceId, true], ['意图', session?.intent], ['终止原因', r.stop_reason, true], ['累计追问', session?.clarification_rounds], ['耗时', ms(r.latency_ms)], ['Token（固定记账样本）', r.token_cost]])),
      section('知识库溯源引用', r.citations.length ? r.citations.map((c) => h('div', { class: 'cite' }, h('strong', null, c.section_path), c.quote ? h('span', null, c.quote) : null,
        h('small', null, `${c.doc_id} · v${c.version}${c.page_number ? ` · 第 ${c.page_number} 页` : ''}`))) : h('span', { class: 'muted' }, '本轮无引用')),
      section('ExtractedSlots 槽位', kv(Object.entries(SLOTS).map(([key, label]) => [label, Array.isArray(slots[key]) ? (slots[key].join('；') || '-') : slots[key]]))),
      section('LoopStep 状态轨迹', h('ol', { class: 'timeline' }, r.tool_traces.map((t) => h('li', { class: t.status },
        `${t.step_index}. ${STEP[t.step] || t.step}`, h('small', null, `${t.tool_name} · ${t.status} · ${ms(t.latency_ms)}${t.output_summary ? ` · ${t.output_summary}` : ''}${t.error_code ? ` · ${t.error_code}` : ''}`))))),
      section('DegradedReason', r.degraded_reasons.length ? h('div', { class: 'chips' }, r.degraded_reasons.map((x) => badge('warn', x))) : badge('good', '无降级')),
      r.escalation_ticket ? section('报修工单', kv([['工单号', r.ticket_receipt?.ticket_id, true], ['优先级', r.escalation_ticket.severity, true], ['设备', `${r.escalation_ticket.equipment_model} ${r.escalation_ticket.equipment_id || ''}`], ['现象', r.escalation_ticket.fault_symptom], ['接单角色', r.ticket_receipt?.assignee_role], ['报修人', r.escalation_ticket.reporter_user_id, true]])) : null,
    ]);
  }

  const root = h('div', { class: 'module' }, quickbar(CASES, pick),
    h('div', { class: 'card chat-card' },
      h('div', { class: 'chat-toolbar' }, h('span', { class: 'muted' }, '门店员工身份'), select, sessionLabel, h('span', { style: 'flex:1' }), h('button', { class: 'btn', type: 'button', onclick: reset }, '重置会话')),
      log, scriptBar, h('div', { class: 'composer' }, input, sendButton)));
  return { root, renderDrawer };
}
