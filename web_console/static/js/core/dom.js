// 全部动态内容经 textContent / setAttribute 写入，不拼接 HTML。
const SVG_NS = 'http://www.w3.org/2000/svg';

function apply(node, props) {
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.setAttribute('class', value);
    else if (key === 'style') node.setAttribute('style', value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'value' || key === 'checked' || key === 'disabled' || key === 'selected') node[key] = value;
    else node.setAttribute(key, value === true ? '' : value);
  }
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function h(tag, props, ...children) {
  const node = document.createElement(tag);
  apply(node, props);
  append(node, children);
  return node;
}

export function s(tag, props, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  apply(node, props);
  append(node, children);
  return node;
}

export function clear(node, ...children) {
  node.replaceChildren();
  append(node, children);
  return node;
}

const PATHS = {
  chat: 'M4 5h16v11H9l-5 4z',
  chart: 'M4 20V10M10 20V4M16 20v-7M21 20H3',
  shield: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-5-5',
  gauge: 'M4 18a9 9 0 1 1 16 0M12 15l4-6',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  cross: 'M6 6l12 12M18 6L6 18',
  alert: 'M12 4l9 16H3zM12 10v5M12 17.5v.5',
  info: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 11v5M12 8v.5',
  sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19',
  panel: 'M4 5h16v14H4zM15 5v14',
  play: 'M7 5l12 7-12 7z',
  lock: 'M6 11h12v9H6zM9 11V8a3 3 0 0 1 6 0v3',
};

export function icon(name) {
  return s('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' },
    s('path', { d: PATHS[name] }));
}
