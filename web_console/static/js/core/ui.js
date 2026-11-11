import { h, icon } from './dom.js';

const BADGE_ICON = { good: 'check', bad: 'cross', warn: 'alert', info: 'info' };

export function badge(kind, text) {
  return h('span', { class: `badge ${kind}` }, BADGE_ICON[kind] ? icon(BADGE_ICON[kind]) : null, text);
}

export function quickbar(cases, onPick) {
  return h('div', { class: 'quickbar' }, h('span', null, '一键填入'),
    cases.map((item) => h('button', { class: `quick ${item.kind}`, type: 'button', onclick: () => onPick(item) },
      icon(item.kind === 'security' ? 'lock' : 'play'), item.label)));
}

export function notice(kind, title, detail) {
  return h('div', { class: `notice ${kind}` }, icon(BADGE_ICON[kind] || 'info'), h('div', null, title, detail ? h('small', null, detail) : null));
}

export function errorNotice(status, error) {
  const kind = status === 401 || status === 403 ? '安全拦截' : '请求未完成';
  return notice('bad', `${kind} · HTTP ${status} · ${error.error_code}`, `${error.message}${error.hint ? `。${error.hint}` : ''}`);
}

export function empty(text) { return h('div', { class: 'empty' }, text); }

export function section(title, ...children) { return h('section', { class: 'd-section' }, h('h4', null, title), children); }

export function kv(pairs) {
  return h('dl', { class: 'kv' }, pairs.filter(Boolean).map(([key, value, mono]) => [h('dt', null, key), h('dd', { class: mono ? 'code-text' : null }, value ?? '-')]));
}

export function metric(label, value) { return h('div', { class: 'metric' }, h('small', null, label), h('b', null, value)); }

export function table(columns, rows, numeric = () => false) {
  return h('div', { class: 'table-wrap' }, h('table', null,
    h('thead', null, h('tr', null, columns.map((c, i) => h('th', { class: numeric(i) ? 'num' : null }, c)))),
    h('tbody', null, rows.map((row) => h('tr', null, row.map((cell, i) => h('td', { class: numeric(i) ? 'num' : null }, cell)))))));
}

export function personaSelect(personas, moduleId, value, onChange) {
  const select = h('select', { class: 'input', 'aria-label': '网关身份', onchange: (e) => onChange(e.target.value) },
    personas.filter((p) => p.modules.includes(moduleId)).map((p) => h('option', { value: p.persona_id, selected: p.persona_id === value }, p.label)));
  return select;
}

export function identityText(persona) {
  return persona.user_id ? `${persona.user_id} / ${persona.dept_id} / ${persona.roles.join(',')}` : '无身份头';
}

export const ms = (value) => (value === null || value === undefined ? '-' : `${Number(value).toFixed(value < 10 ? 2 : 1)} ms`);
export const pct = (value, digits = 1) => (value === null || value === undefined ? '-' : `${(value * 100).toFixed(digits)}%`);

const tooltip = () => document.getElementById('tooltip');
export function showTip(event, text) {
  const tip = tooltip();
  tip.textContent = text;
  tip.hidden = false;
  const x = Math.min(event.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
  const y = Math.max(event.clientY - tip.offsetHeight - 10, 8);
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
export function hideTip() { tooltip().hidden = true; }

const SQL_KEYWORD = /^(?:WITH|AS|SELECT|FROM|JOIN|LEFT|ON|WHERE|AND|OR|GROUP BY|ORDER BY|LIMIT|BETWEEN|DESC|ASC|SUM|MAX|NULLIF|IN|CASE|WHEN|THEN|ELSE|END)$/i;

/** 轻量 SQL 排版与着色；只产生文本节点与 span。 */
export function sqlBlock(sql) {
  const pretty = sql.replace(/\s+(FROM|WHERE|GROUP BY|ORDER BY|LIMIT|JOIN|LEFT JOIN)\s+/gi, '\n$1 ').replace(/\)\s*,\s*(\w+ AS \()/gi, '),\n$1').replace(/\)\s+SELECT/gi, ')\nSELECT');
  const block = h('pre', { class: 'code' });
  const tokens = /('(?:[^']|'')*')|(:[a-z_]+)|([A-Za-z_]+(?: BY)?)|([\s\S])/g;
  let match;
  while ((match = tokens.exec(pretty))) {
    if (match[1]) block.append(h('span', { class: 's' }, match[1]));
    else if (match[2]) block.append(h('span', { class: 'p' }, match[2]));
    else if (match[3] && SQL_KEYWORD.test(match[3])) block.append(h('span', { class: 'k' }, match[3]));
    else block.append(match[0]);
  }
  return block;
}
