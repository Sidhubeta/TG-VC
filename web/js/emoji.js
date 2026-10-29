// Renders every emoji on the page as Apple (iOS) emoji images, on Android too.
const RE = /(?:[\u{1F1E6}-\u{1F1FF}]{2}|[#*0-9]️?⃣|\p{Extended_Pictographic}(?:️|[\u{1F3FB}-\u{1F3FF}])?(?:‍\p{Extended_Pictographic}(?:️|[\u{1F3FB}-\u{1F3FF}])?)*)/gu;
const failed = new Set();
const SKIP = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'OPTION', 'svg', 'use', 'TITLE']);

function code(e, keepFE0F = true) {
  return [...e].map(c => c.codePointAt(0).toString(16)).filter(c => keepFE0F || c !== 'fe0f').join('-');
}
function img(e) {
  const i = document.createElement('img');
  i.className = 'emj';
  i.alt = e;
  i.draggable = false;
  const full = code(e), bare = code(e, false);
  i.src = `/emoji/${full}.png`;
  i.onerror = () => {
    if (i.dataset.t !== '1' && bare !== full) { i.dataset.t = '1'; i.src = `/emoji/${bare}.png`; }
    else if (i.dataset.t !== '2' && !full.includes('fe0f') && !full.includes('200d')) { i.dataset.t = '2'; i.src = `/emoji/${full}-fe0f.png`; }
    else { failed.add(e); i.replaceWith(document.createTextNode(e)); }
  };
  return i;
}
function processText(node) {
  const t = node.nodeValue;
  RE.lastIndex = 0;
  if (!t || !RE.test(t)) return;
  RE.lastIndex = 0;
  const p = node.parentNode;
  if (!p || SKIP.has(p.nodeName) || p.closest?.('[data-noemoji]')) return;
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of t.matchAll(RE)) {
    if (/^[0-9#*]$/.test(m[0]) || failed.has(m[0])) continue;
    if (m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
    frag.appendChild(img(m[0]));
    last = m.index + m[0].length;
  }
  if (!last) return;
  if (last < t.length) frag.appendChild(document.createTextNode(t.slice(last)));
  p.replaceChild(frag, node);
}
function walk(root) {
  if (root.nodeType === 3) return processText(root);
  if (root.nodeType !== 1 || SKIP.has(root.nodeName)) return;
  const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const list = [];
  while (tw.nextNode()) list.push(tw.currentNode);
  list.forEach(processText);
}
export function startEmoji() {
  walk(document.body);
  new MutationObserver(muts => {
    for (const m of muts) {
      if (m.type === 'characterData') processText(m.target);
      else m.addedNodes.forEach(walk);
    }
  }).observe(document.body, { childList: true, subtree: true, characterData: true });
}
