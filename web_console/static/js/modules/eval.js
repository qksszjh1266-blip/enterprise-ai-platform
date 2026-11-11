import { clear, h, s } from '../core/dom.js';
import { get } from '../core/api.js';
import { badge, empty, hideTip, kv, ms, notice, pct, quickbar, section, showTip, table } from '../core/ui.js';

const SERIES = [['accuracy', '回答准确率', 'var(--series-1)'], ['hit_rate', '检索命中率', 'var(--series-2)'], ['faithfulness', '忠实度', 'var(--series-3)']];
const CASES = [
  { label: '查看各智能体准确率', kind: 'positive', target: 'accuracy' },
  { label: '查看失败归因分布', kind: 'positive', target: 'failures' },
  { label: '定位被阻断的回归门禁', kind: 'security', target: 'gates' },
];

function roundedTop(x, y, w, height, r = 4) {
  const radius = Math.min(r, height, w / 2);
  return `M${x},${y + height}V${y + radius}Q${x},${y} ${x + radius},${y}H${x + w - radius}Q${x + w},${y} ${x + w},${y + radius}V${y + height}Z`;
}
function roundedRight(x, y, w, height, r = 4) {
  const radius = Math.min(r, w, height / 2);
  return `M${x},${y}H${x + w - radius}Q${x + w},${y} ${x + w},${y + radius}V${y + height - radius}Q${x + w},${y + height} ${x + w - radius},${y + height}H${x}Z`;
}
function hover(node, text) {
  node.addEventListener('mousemove', (e) => showTip(e, text));
  node.addEventListener('mouseleave', hideTip);
  return node;
}
const legend = (items) => h('div', { class: 'legend' }, items.map(([label, color]) => h('span', null, h('i', { style: `background:${color}` }), label)));

function accuracyChart(agents) {
  const W = 720, H = 250, L = 42, R = 8, T = 18, B = 34;
  const plotH = H - T - B, groupW = (W - L - R) / agents.length, barW = 18, gap = 2;
  const y = (v) => T + plotH * (1 - v);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart-area', role: 'img', 'aria-label': '各智能体准确率、命中率、忠实度柱状图' });
  for (const tick of [0, 0.25, 0.5, 0.75, 1]) {
    svg.append(s('line', { x1: L, x2: W - R, y1: y(tick), y2: y(tick), stroke: 'var(--line)', 'stroke-width': 1 }),
      s('text', { x: L - 8, y: y(tick) + 4, 'text-anchor': 'end', 'font-size': 11, fill: 'var(--ink-3)' }, `${tick * 100}%`));
  }
  agents.forEach((agent, gi) => {
    const start = L + gi * groupW + (groupW - (barW * 3 + gap * 2)) / 2;
    SERIES.forEach(([key, label, color], si) => {
      const value = agent[key];
      if (value === null || value === undefined) return;
      const x = start + si * (barW + gap);
      svg.append(s('path', { d: roundedTop(x, y(value), barW, Math.max(y(0) - y(value), 1)), fill: color }));
      svg.append(hover(s('rect', { x: x - 1, y: T, width: barW + 2, height: plotH, fill: 'transparent' }), `${agent.label}\n${label}：${pct(value)}`));
      if (value < 0.995) svg.append(s('text', { x: x + barW / 2, y: y(value) - 5, 'text-anchor': 'middle', 'font-size': 11, 'font-weight': 600, fill: 'var(--ink)' }, pct(value, 0)));
    });
    svg.append(s('text', { x: L + gi * groupW + groupW / 2, y: H - 12, 'text-anchor': 'middle', 'font-size': 12, fill: 'var(--ink-2)' }, agent.label));
  });
  return svg;
}

function failureChart(labels, baseline, injected) {
  const keys = [...new Set([...Object.keys(injected), ...Object.keys(baseline)])].filter((k) => k !== 'none').sort((a, b) => (injected[b] || 0) - (injected[a] || 0));
  const max = Math.max(...keys.flatMap((k) => [injected[k] || 0, baseline[k] || 0]), 1);
  const W = 720, L = 96, R = 40, row = 34, barH = 11, gap = 2, H = keys.length * row + 6;
  const x = (v) => (W - L - R) * (v / max);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart-area', role: 'img', 'aria-label': '失败归因分布条形图' });
  svg.append(s('line', { x1: L, x2: L, y1: 0, y2: H, stroke: 'var(--line-strong)', 'stroke-width': 1 }));
  keys.forEach((key, i) => {
    const top = i * row + 5;
    svg.append(s('text', { x: L - 10, y: top + barH + 5, 'text-anchor': 'end', 'font-size': 12, fill: 'var(--ink-2)' }, labels[key] || key));
    [[injected[key] || 0, 'var(--series-2)', '故障注入运行'], [baseline[key] || 0, 'var(--series-1)', '基线运行']].forEach(([value, color, name], j) => {
      const y = top + j * (barH + gap);
      if (value > 0) {
        svg.append(s('path', { d: roundedRight(L, y, Math.max(x(value), 2), barH), fill: color }));
        svg.append(s('text', { x: L + x(value) + 6, y: y + barH - 1, 'font-size': 11, fill: 'var(--ink)' }, value));
      }
    });
    svg.append(hover(s('rect', { x: L, y: top - 3, width: W - L - R, height: row - 2, fill: 'transparent' }), `${labels[key] || key}\n故障注入运行：${injected[key] || 0}\n基线运行：${baseline[key] || 0}`));
  });
  return svg;
}

function toggleTable(chart, tableNode) {
  const holder = h('div', { class: 'chart-scroll' }, chart);
  let showing = false;
  const button = h('button', { class: 'link', type: 'button', onclick: () => { showing = !showing; clear(holder, showing ? tableNode : chart); button.textContent = showing ? '查看图表' : '查看数据表'; } }, '查看数据表');
  return { holder, button };
}

export function createEval(ctx) {
  const output = h('div', { style: 'display:grid;gap:12px' }, h('div', { class: 'card' }, empty('正在读取评测报告。')));
  const anchors = {};
  let data = null;

  function pick(item) {
    anchors[item.target]?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (item.target === 'gates') anchors.gates?.querySelectorAll('details').forEach((d) => { d.open = d.dataset.passed === 'false'; });
  }

  function render() {
    const { kpis, agents, gates, injected_failure_distribution: injected, failure_labels: labels } = data;
    const baseline = {};
    agents.forEach((a) => Object.entries(a.failure_distribution).forEach(([k, v]) => { baseline[k] = (baseline[k] || 0) + v; }));
    const acc = toggleTable(accuracyChart(agents), table(['智能体', '样本', '准确率', '命中率', '召回率', 'MRR', '忠实度', '错误率', 'P95'], agents.map((a) => [a.label, a.samples, pct(a.accuracy), pct(a.hit_rate), pct(a.recall), a.mrr?.toFixed(3) ?? '-', pct(a.faithfulness), pct(a.error_rate), ms(a.p95_ms)]), (i) => i > 0));
    const failKeys = [...new Set([...Object.keys(injected), ...Object.keys(baseline)])].filter((k) => k !== 'none');
    const fail = toggleTable(failureChart(labels, baseline, injected), table(['归因', '故障注入运行', '基线运行'], failKeys.map((k) => [labels[k] || k, injected[k] || 0, baseline[k] || 0]), (i) => i > 0));
    anchors.accuracy = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', null, '各智能体质量指标'), legend(SERIES.map(([, l, c]) => [l, c])), h('span', { class: 'spacer' }), acc.button), acc.holder,
      agents.filter((a) => a.note).map((a) => h('p', { class: 'muted', style: 'margin-top:6px' }, `${a.label}：${a.note}`)));
    anchors.failures = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', null, '失败归因分布（样本数）'), legend([['故障注入运行', 'var(--series-2)'], ['基线运行', 'var(--series-1)']]), h('span', { class: 'spacer' }), fail.button), fail.holder);
    anchors.gates = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', null, '回归门禁状态'), h('span', { class: 'muted' }, '故障注入项被阻断是预期结果，证明门禁有效')), h('div', { class: 'gate-list' }, gates.map((g) => h('details', { class: 'gate', 'data-passed': String(g.passed) },
      h('summary', null, g.passed ? badge('good', '通过') : badge('bad', '阻断'), h('span', null, g.label), h('span', { class: 'muted' }, g.kind), h('span', null, g.reasons[0] || `${g.baseline} 对比 ${g.candidate}`)),
      h('div', { class: 'gate-body' }, h('p', null, `基线 ${g.baseline} / 候选 ${g.candidate}${g.newly_failed === null ? '' : ` / 新增失败 ${g.newly_failed}`}`), g.reasons.length ? h('ul', null, g.reasons.map((r) => h('li', null, r))) : h('p', null, '全部门禁检查通过。'))))));
    clear(output,
      h('div', { class: 'kpis' },
        h('div', { class: 'card kpi' }, h('small', null, '评测样本总数'), h('b', null, kpis.samples), h('em', null, `覆盖 ${agents.length} 条基线`)),
        h('div', { class: 'card kpi' }, h('small', null, '越权红线拦截'), h('b', null, `${kpis.acl_blocked}/${kpis.acl_total}`), h('em', null, `拦截率 ${pct(kpis.acl_blocked / kpis.acl_total, 0)}`)),
        h('div', { class: 'card kpi' }, h('small', null, '门禁放行 / 阻断'), h('b', null, `${kpis.gates_passed} / ${kpis.gates_blocked}`), h('em', null, `共 ${kpis.gates_total} 组对照实验`)),
        h('div', { class: 'card kpi' }, h('small', null, '最高 P95 延迟'), h('b', null, ms(kpis.worst_p95_ms)), h('em', null, '离线替身环境实测'))),
      anchors.accuracy, anchors.failures, anchors.gates, notice('info', data.disclaimer));
  }

  async function load() {
    const result = await get('/api/console/eval/dashboard');
    if (!result.ok) return clear(output, notice('bad', '评测报告读取失败', '请确认报告文件存在且格式未被修改。'));
    data = result.data;
    render();
    renderDrawer();
  }

  function renderDrawer() {
    if (!data) return ctx.drawer('评测中台', [empty('读取中')]);
    ctx.drawer('评测中台', [
      ...data.agents.map((a) => section(a.label, kv([['运行 ID', a.run_id, true], ['被测版本', a.version, true], ['样本 / 失败', `${a.samples} / ${a.failed}`], ['红线拦截', `${a.acl_blocked}/${a.acl_total}`], ['MRR', a.mrr?.toFixed(3)], ['P95', ms(a.p95_ms)]]))),
    ]);
  }

  load();
  const root = h('div', { class: 'module' }, quickbar(CASES, pick), output);
  return { root, renderDrawer };
}
