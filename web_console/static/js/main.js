import { clear, h, icon } from './core/dom.js';
import { get } from './core/api.js';
import { createSop } from './modules/sop.js';
import { createBi } from './modules/bi.js';
import { createFinance } from './modules/finance.js';
import { createKnowledge } from './modules/knowledge.js';
import { createEval } from './modules/eval.js';

const MODULES = [
  { id: 'sop', label: '门店 SOP 督导助手', desc: '多轮问答 · 强制溯源 · 排障转报修', icon: 'chat', create: createSop },
  { id: 'bi', label: '供应链经营分析', desc: '受控 Text-to-SQL · 静态守卫 · 只读沙箱', icon: 'chart', create: createBi },
  { id: 'finance', label: '财务与合同预审', desc: 'Decimal 规则 · PII 脱敏 · 人工审批', icon: 'shield', create: createFinance },
  { id: 'knowledge', label: '知识数据中台检索器', desc: 'ACL 前置过滤 · 双路召回 · 父子切块', icon: 'search', create: createKnowledge },
  { id: 'eval', label: '评测中台与门禁看板', desc: '指标 · 失败归因 · 回归门禁', icon: 'gauge', create: createEval },
];

const THEME_KEY = 'console-theme';
const instances = new Map();
let current = null;
let status = null;

const $ = (id) => document.getElementById(id);

function storedTheme() { try { return localStorage.getItem(THEME_KEY); } catch { return null; } }

function setupTheme() {
  const saved = storedTheme();
  if (saved) document.documentElement.dataset.theme = saved;
  $('theme-toggle').append(icon('sun'));
  $('theme-toggle').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    const next = dark ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch { /* 无痕窗口下仅当次生效 */ }
  });
}

function setDrawer(moduleId, subtitle, nodes) {
  if (moduleId !== current) return;
  $('drawer-sub').textContent = subtitle;
  clear($('drawer-body'), nodes);
}

function renderEnv() {
  clear($('env-status'),
    h('span', { class: 'env-chip' }, h('i', { class: 'env-dot' }), h('b', null, status.mode)),
    h('span', { class: 'env-chip' }, '模型 ', h('b', null, '本地确定性适配器')),
    h('span', { class: 'env-chip' }, '知识库 ', h('b', null, `${status.documents} 文档 / ${status.parents} 父块 / ${status.children} 子块`)),
    h('span', { class: 'env-chip' }, '只读沙箱 ', h('b', null, `${status.tables} 表`)),
    h('span', { class: 'env-chip' }, '基准日 ', h('b', null, status.as_of_date)),
    h('span', { class: 'env-chip' }, '外部调用 ', h('b', null, status.external_calls)));
}

function activate(id) {
  const meta = MODULES.find((m) => m.id === id) || MODULES[0];
  current = meta.id;
  if (!instances.has(meta.id)) {
    const instance = meta.create({ personas: status.personas, status, drawer: (sub, nodes) => setDrawer(meta.id, sub, nodes) });
    instances.set(meta.id, instance);
    $('workspace').append(instance.root);
  }
  for (const [key, instance] of instances) instance.root.classList.toggle('active', key === meta.id);
  document.querySelectorAll('.nav-item').forEach((el) => el.classList.toggle('active', el.dataset.id === meta.id));
  $('crumb-title').textContent = meta.label;
  $('crumb-desc').textContent = meta.desc;
  instances.get(meta.id).renderDrawer();
}

async function boot() {
  setupTheme();
  const app = $('app');
  $('drawer-toggle').append(icon('panel'));
  const narrow = matchMedia('(max-width: 1180px)');
  app.classList.toggle('drawer-closed', narrow.matches);
  narrow.addEventListener('change', (e) => app.classList.toggle('drawer-closed', e.matches));
  $('drawer-toggle').addEventListener('click', () => app.classList.toggle('drawer-closed'));
  $('drawer-close').addEventListener('click', () => app.classList.add('drawer-closed'));
  clear($('nav-list'), MODULES.map((m) => h('button', { class: 'nav-item', type: 'button', 'data-id': m.id, onclick: () => { location.hash = m.id; } }, icon(m.icon), m.label)));
  const result = await get('/api/console/status');
  if (!result.ok) {
    clear($('workspace'), h('div', { class: 'empty' }, '运行时尚未就绪，请刷新页面重试。'));
    return;
  }
  status = result.data;
  renderEnv();
  window.addEventListener('hashchange', () => activate(location.hash.slice(1)));
  activate(location.hash.slice(1));
}

boot();
