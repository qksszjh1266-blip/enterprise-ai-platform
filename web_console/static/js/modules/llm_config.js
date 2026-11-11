import { clear, h } from '../core/dom.js';
import { del, errorOf, get, post, put } from '../core/api.js';
import { notice } from '../core/ui.js';

const KEY_SOURCE = { env: '环境变量或 .env', web: '页面配置' };

function field(label, control, help) {
  return h('label', { class: 'llm-field' }, h('span', null, label), control, help ? h('small', null, help) : null);
}

/** 模型配置抽屉。密钥只经输入框提交给本地服务，关闭抽屉时立即清空，服务端也不会回传。 */
export function createLlmDialog(onChange) {
  let mode = 'offline';
  let busy = false;
  const input = (props) => h('input', { class: 'input', autocomplete: 'off', spellcheck: 'false', ...props });
  const baseUrl = input({ type: 'text', placeholder: '兼容 /chat/completions 的 /v1 地址' });
  const model = input({ type: 'text', placeholder: '模型名称' });
  const apiKey = input({ type: 'password', autocomplete: 'new-password' });
  const timeout = input({ type: 'number', min: '1', max: '300', step: '1' });
  const presets = h('div', { class: 'llm-presets' });
  const result = h('div', { class: 'llm-result', 'aria-live': 'polite' });
  const modeButtons = {
    offline: h('button', { type: 'button', onclick: () => setMode('offline') }, '离线确定性演示'),
    online: h('button', { type: 'button', onclick: () => setMode('online') }, '在线 API 推理'),
  };
  const online = h('div', { class: 'llm-online' },
    h('div', { class: 'llm-field' }, h('span', null, '常用网关'), presets),
    field('Base URL', baseUrl), field('Model', model), field('API Key', apiKey, '仅保存在当前服务进程内存中，不写入磁盘，重启后失效。'),
    field('超时（秒）', timeout),
    notice('warn', '在线模式会向所配置的网关发送数据', '包括用户问题、制度片段、语义字典、查询结果和脱敏后的单据。检索、SQL 沙箱与财务规则仍在本地执行。'));
  const testButton = h('button', { class: 'btn', type: 'button', onclick: () => run(testConnection) }, '测试连通性');
  const saveButton = h('button', { class: 'btn primary', type: 'button', onclick: () => run(save) }, '保存生效');
  const clearButton = h('button', { class: 'btn', type: 'button', onclick: () => run(reset) }, '清空配置');
  const dialog = h('dialog', { class: 'llm-dialog', 'aria-labelledby': 'llm-title' },
    h('div', { class: 'drawer-head' },
      h('div', null, h('strong', { id: 'llm-title' }, '模型配置'), h('small', null, '切换离线适配器与在线模型网关')),
      h('button', { class: 'link', type: 'button', onclick: () => dialog.close() }, '关闭')),
    h('div', { class: 'llm-body' },
      h('div', { class: 'llm-mode', role: 'group', 'aria-label': '推理模式' }, modeButtons.offline, modeButtons.online),
      online, result),
    h('div', { class: 'llm-actions' }, clearButton, h('span', { class: 'llm-spacer' }), testButton, saveButton));

  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener('close', () => { apiKey.value = ''; clear(result); });
  document.body.append(dialog);

  function setMode(next) {
    mode = next;
    for (const [key, button] of Object.entries(modeButtons)) {
      button.classList.toggle('active', key === mode);
      button.setAttribute('aria-pressed', String(key === mode));
    }
    online.hidden = mode !== 'online';
    testButton.hidden = mode !== 'online';
  }

  function fill(config) {
    baseUrl.value = config.base_url;
    model.value = config.model;
    timeout.value = String(config.timeout_seconds);
    apiKey.value = '';
    apiKey.placeholder = config.key_configured ? `已配置（${KEY_SOURCE[config.key_source] || '未知来源'}），留空沿用` : '请输入 API Key';
    clear(presets, config.presets.map((preset) => h('button', {
      class: 'quick', type: 'button',
      onclick: () => { baseUrl.value = preset.base_url; model.value = preset.model; (preset.base_url ? apiKey : baseUrl).focus(); },
    }, preset.label)));
    setMode(config.mode);
  }

  const payload = () => ({ mode, base_url: baseUrl.value, model: model.value, api_key: apiKey.value, timeout_seconds: timeout.value === '' ? null : Number(timeout.value) });

  function showError(response) {
    const error = errorOf(response);
    clear(result, notice('bad', error.message, error.hint));
  }

  async function run(action) {
    if (busy) return;
    busy = true;
    for (const button of [testButton, saveButton, clearButton]) button.disabled = true;
    clear(result, notice('info', '正在处理…'));
    try {
      await action();
    } finally {
      busy = false;
      for (const button of [testButton, saveButton, clearButton]) button.disabled = false;
    }
  }

  async function testConnection() {
    const response = await post('/api/console/llm/test', payload());
    if (response.ok) clear(result, notice('good', '连通成功', `模型 ${response.data.model}，耗时 ${Math.round(response.data.latency_ms)} ms`));
    else showError(response);
    await onChange();
  }

  async function save() {
    const response = await put('/api/console/llm/config', mode === 'offline' ? { mode } : payload());
    if (!response.ok) { showError(response); return; }
    await onChange();
    dialog.close();
  }

  async function reset() {
    const response = await del('/api/console/llm/config');
    if (!response.ok) { showError(response); return; }
    fill(response.data);
    clear(result, notice('good', '已清空页面配置并回到离线演示模式'));
    await onChange();
  }

  return {
    async open() {
      const response = await get('/api/console/llm/config');
      if (!response.ok) return;
      fill(response.data);
      clear(result);
      dialog.showModal();
    },
  };
}
