async function parse(response) {
  const text = await response.text();
  try { return text ? JSON.parse(text) : null; } catch { return null; }
}

async function request(path, options) {
  try {
    const response = await fetch(path, options);
    const data = await parse(response);
    return { ok: response.ok, status: response.status, data };
  } catch {
    return { ok: false, status: 0, data: { error: { error_code: 'NETWORK_ERROR', message: '无法连接本地服务', hint: '请确认工作台进程仍在运行。' } } };
  }
}

export const post = (path, body) => request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
export const get = (path) => request(path);

/** 把校验错误等响应统一成 ToolErrorResponse 结构。 */
export function errorOf(result) {
  const data = result.data || {};
  if (data.error) return data.error;
  if (data.error_code) return data;
  return { error_code: `HTTP_${result.status}`, message: '请求未能完成', hint: '请检查输入后重试。' };
}
