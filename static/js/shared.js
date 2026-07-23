/*
  shared.js — cross-template JS utilities (Refactor Phase 6).

  Loaded from base.html <head> so these globals exist before any inline
  <script> inside {% block content %} / {% block scripts %} runs (inline
  scripts execute at parse time, before end-of-body scripts like main.js).

  Extraction rule: each definition here is the exact logic the templates
  used inline. Where two templates' helpers diverged (catTypeFor vs
  cuCatType), BOTH names are kept with their original semantics — do not
  unify them.
*/

// ── HTML escape ───────────────────────────────────────────────────────────────
// Extracted from receipts/review.html (function esc) and tools/cleanup.html
// (const esc) — identical bodies.
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

// ── Money format ─────────────────────────────────────────────────────────────
// Extracted from receipts/review.html. Always two decimals, $ prefix.
function money(n){return '$'+Number(n||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}

// ── Category-type resolvers (type → cat_type key) ────────────────────────────
// The category tree key is just the trx_type.
// Both legacy names kept so any template caller keeps working.
function catTypeFor(owner, type){type=type||'expense';return type==='transfer'?'transfer':type;}
function cuCatType(owner, type){return catTypeFor(owner, type);}

// ── Error toast + txPatch (Phase 6 behavior change) ───────────────────────────
// Small fixed-position toast for save failures. Styled inline to match the
// app's alert toasts using the --red palette.
// _mkToast: shared builder for the fixed-position toasts. PERSISTENT — no
// auto-dismiss; a × button lets the user clear it. Reuses one bar per id so
// repeated saves/errors don't stack.
function _mkToast(id, css) {
  let bar = document.getElementById(id);
  if (!bar) {
    bar = document.createElement('div');
    bar.id = id;
    bar.style.cssText = 'position:fixed;top:16px;right:16px;z-index:9999;'
      + 'display:flex;align-items:center;gap:12px;'
      + 'box-shadow:0 4px 12px rgba(0,0,0,0.15);max-width:360px;'
      + 'border-radius:var(--radius);font-size:13px;' + css;
    const span = document.createElement('span'); span.className = 'toast-text';
    bar.appendChild(span);
    const x = document.createElement('button'); x.type = 'button';
    x.textContent = '×'; x.setAttribute('aria-label', 'Dismiss');
    x.style.cssText = 'background:none;border:none;font-size:18px;line-height:1;'
      + 'cursor:pointer;color:inherit;margin-left:auto;opacity:.7';
    x.onclick = () => bar.remove();
    bar.appendChild(x);
    document.body.appendChild(bar);
  }
  return bar;
}

function _txErrorToast(text) {
  const bar = _mkToast('tx-error-toast',
    'padding:10px 14px;background:var(--red-bg);color:var(--red);border:1px solid var(--red)');
  bar.querySelector('.toast-text').textContent = text;
  // Persistent — a real error stays until the user resolves/dismisses it.
}

// POST JSON to `url`, parse the response.
//   Success (HTTP ok and not {ok:false}) → returns parsed JSON
//     (if the body isn't JSON but HTTP was ok, returns {ok:true} — matches
//     the old _trx_table_js.html txPatch fallback).
//   Failure (non-ok HTTP, or body {ok:false}) → shows the error toast and
//     THROWS, so success-path UI updates after `await txPatch(...)` never
//     run on a failed save. Network errors toast + rethrow too.
// ── Type-to-filter comboboxes (2026-07-05) ─────────────────────────────
// Progressive enhancement: every <select> with ≥ COMBO_MIN_OPTIONS options
// becomes a searchable dropdown — typing narrows the list (prefix matches
// first, substring matches after). The native select stays in the DOM
// (hidden) as the source of truth: picking an item sets select.value and
// dispatches 'change', so every existing handler works untouched. Options
// are re-read every time the menu opens, so selects whose options get
// rebuilt (L1→L2 chains etc.) stay correct automatically. Placeholder =
// the select's empty-value option text. Opt out with data-no-combo.
const COMBO_MIN_OPTIONS = 5;

function comboify(sel) {
  if (!sel || sel.tagName !== 'SELECT' || sel._combo) return;
  if (sel.multiple || sel.size > 1 || 'noCombo' in sel.dataset) return;
  if (sel.options.length < COMBO_MIN_OPTIONS) return;
  sel._combo = true;

  const wrap = document.createElement('span');
  wrap.style.cssText = 'position:relative;display:inline-block;vertical-align:middle;width:'
    + (sel.style.width || (sel.offsetWidth ? sel.offsetWidth + 'px' : '100%'));
  const inp = document.createElement('input');
  inp.type = 'text';
  inp.className = sel.className;
  inp.style.cssText = sel.style.cssText;
  inp.style.width = '100%';
  inp.autocomplete = 'off';
  // Native validation must target a focusable control — transfer `required`.
  if (sel.required) { sel.required = false; inp.required = true; }
  const menu = document.createElement('div');
  menu.style.cssText = 'position:absolute;top:100%;left:0;min-width:100%;z-index:10000;'
    + 'display:none;background:var(--surface,#fff);border:1px solid var(--border,#ccc);'
    + 'border-radius:6px;box-shadow:0 6px 18px rgba(0,0,0,.12);max-height:240px;'
    + 'overflow-y:auto;margin-top:2px;text-align:left';

  sel.style.display = 'none';
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(inp);
  wrap.appendChild(menu);

  let items = [], active = -1, open = false;

  function syncDisplay() {
    const ph = [...sel.options].find(x => !x.value);
    inp.placeholder = ph ? (ph.text || '—') : '';
    const o = sel.options[sel.selectedIndex];
    inp.value = (o && o.value) ? o.text : '';
  }
  sel._comboSync = syncDisplay;

  function paint() {
    [...menu.children].forEach((el, i) =>
      el.style.background = i === active ? 'var(--surface-alt,#f0f2f5)' : '');
    const el = menu.children[active];
    if (el && el.scrollIntoView) el.scrollIntoView({block: 'nearest'});
  }
  function render(q) {
    const ql = (q || '').toLowerCase();
    const starts = [], contains = [];
    for (const o of sel.options) {
      const t = o.text.toLowerCase();
      if (!ql || t.startsWith(ql)) starts.push({value: o.value, text: o.text});
      else if (t.includes(ql)) contains.push({value: o.value, text: o.text});
    }
    items = starts.concat(contains);
    active = items.length ? 0 : -1;
    menu.innerHTML = items.map((o, i) =>
      `<div class="combo-item" data-i="${i}" style="padding:6px 10px;cursor:pointer;white-space:nowrap${i === active ? ';background:var(--surface-alt,#f0f2f5)' : ''}">${esc(o.text || '—')}</div>`
    ).join('') || '<div style="padding:6px 10px;color:var(--text-3,#999);font-style:italic">No matches</div>';
  }
  function show(q) { open = true; render(q); menu.style.display = 'block'; }
  function hide() { open = false; menu.style.display = 'none'; syncDisplay(); }
  function pick(i) {
    const o = items[i];
    if (!o) return;
    sel.value = o.value;
    hide();
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    syncDisplay();
  }

  inp.addEventListener('focus', () => { inp.select(); show(''); });
  inp.addEventListener('input', () => show(inp.value.trim()));
  inp.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!open) show(inp.value.trim());
      else { active = Math.min(active + 1, items.length - 1); paint(); }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault(); active = Math.max(active - 1, 0); paint();
    } else if (e.key === 'Enter') {
      if (open && active >= 0) { e.preventDefault(); pick(active); }
    } else if (e.key === 'Escape') {
      hide(); inp.blur();
    } else if (e.key === 'Tab') hide();
  });
  // mousedown (not click) so the pick lands before the input's blur
  menu.addEventListener('mousedown', e => {
    const it = e.target.closest('.combo-item');
    if (it) { e.preventDefault(); pick(+it.dataset.i); }
  });
  document.addEventListener('mousedown', e => {
    if (open && !wrap.contains(e.target)) hide();
  });
  inp.addEventListener('blur', () => setTimeout(() => { if (open) hide(); }, 120));

  syncDisplay();
}

function comboifyAll(root) {
  if (root.querySelectorAll) root.querySelectorAll('select').forEach(comboify);
  if (root.tagName === 'SELECT') comboify(root);
}

document.addEventListener('DOMContentLoaded', () => {
  comboifyAll(document);
  // Dynamically-created selects (inline cell editors, popups, split rows)
  // + selects whose options are swapped out (fillL1/fillL2) enhance/resync
  // automatically.
  new MutationObserver(muts => {
    for (const m of muts) {
      if (m.addedNodes) m.addedNodes.forEach(n => { if (n.nodeType === 1) comboifyAll(n); });
      const t = m.target;
      if (t && t.tagName === 'SELECT') {
        if (t._comboSync) t._comboSync();
        else comboify(t);   // options arrived after the node itself
      }
    }
  }).observe(document.body, {childList: true, subtree: true});
});

// Green "Saved" toast (2026-07-04: every inline save shows obvious
// feedback everywhere EXCEPT the import review queues, which save on the
// explicit Save/approve click — those pages set
// window.TX_SUPPRESS_SAVED_TOAST = true).
function _txSavedToast() {
  if (window.TX_SUPPRESS_SAVED_TOAST) return;
  const bar = _mkToast('tx-saved-toast',
    'padding:8px 14px;background:var(--green-bg, #e8f5ec);color:var(--green);border:1px solid var(--green)');
  bar.querySelector('.toast-text').textContent = '✓ Saved';
  // Persistent + dismissible (no auto-fade). A new save reuses this same bar
  // rather than stacking. If a lingering "Saved" ever feels noisy on rapid
  // cell edits, this toast is the place to add an auto-fade exception.
}

// ── Drag & drop for file-upload cards ────────────────────────────────────────
// initDropzone(zoneEl, inputEl, opts) — ONE shared implementation for every
// upload in the app (Import CSV, Gusto payroll upload, investments import).
// Extracted from templates/import.html's inline dropzone so all uploads get
// identical affordances: highlight while dragging over the card, filename
// shown on drop or click-to-browse, extension filter, and NO auto-submit —
// dropping only assigns the file to the input (DataTransfer → files); the
// form's normal submit flow is untouched.
//   zoneEl        the drop target (usually the whole card)
//   inputEl       the <input type=file> the dropped file is assigned to
//   opts.accept   extension filter like '.csv,.txt'; defaults to the input's
//                 accept attribute. Empty/none = any file accepted.
//   opts.areaEl   existing element to highlight on drag; if omitted the
//                 helper wraps the input in a dashed drop area + hint line
//   opts.labelEl  existing element for the picked filename; if omitted the
//                 helper appends one to the area
function initDropzone(zoneEl, inputEl, opts) {
  opts = opts || {};
  if (!zoneEl || !inputEl || inputEl._dropzone) return;
  inputEl._dropzone = true;

  const exts = String(opts.accept || inputEl.getAttribute('accept') || '')
    .split(',').map(s => s.trim().toLowerCase()).filter(s => s.startsWith('.'));

  let area = opts.areaEl;
  if (!area) {
    area = document.createElement('div');
    area.className = 'dropzone-area';
    area.style.cssText = 'border:1.5px dashed var(--border-mid);border-radius:var(--radius);'
      + 'padding:14px;text-align:center;background:var(--bg);'
      + 'transition:background .15s,border-color .15s';
    inputEl.parentNode.insertBefore(area, inputEl);
    area.appendChild(inputEl);
    const hint = document.createElement('div');
    hint.className = 'text-xs text-3 mt-1';
    hint.textContent = '…or drag & drop a file anywhere on this card.';
    area.appendChild(hint);
  }
  let label = opts.labelEl;
  if (!label) {
    label = document.createElement('div');
    label.className = 'text-xs mt-1 dropzone-filename';
    label.style.cssText = 'display:none;color:var(--navy);font-weight:600';
    area.appendChild(label);
  }

  function showName(name) {
    label.textContent = name ? '✓ ' + name : '';
    label.style.display = name ? 'block' : 'none';
  }
  function setHover(on) {
    area.style.background  = on ? 'var(--navy-light)' : 'var(--bg)';
    area.style.borderColor = on ? 'var(--navy)'       : 'var(--border-mid)';
  }

  // dragenter/leave fire on children too — count depth so the highlight
  // doesn't flicker while dragging across the card's inner elements.
  let depth = 0;
  zoneEl.addEventListener('dragenter', e => {
    e.preventDefault(); depth++; setHover(true);
  });
  zoneEl.addEventListener('dragover', e => e.preventDefault());
  zoneEl.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (depth === 0) setHover(false);
  });
  zoneEl.addEventListener('drop', e => {
    e.preventDefault(); depth = 0; setHover(false);
    const files = e.dataTransfer ? e.dataTransfer.files : null;
    if (!files || !files.length) return;
    const f = files[0];
    if (exts.length && !exts.some(x => f.name.toLowerCase().endsWith(x))) {
      alert("That file type isn't accepted here — this upload takes "
            + exts.join(' / ') + ' files only.');
      return;
    }
    // Assign the dropped file to the existing input via DataTransfer, so
    // the normal form submit flow proceeds unchanged (no auto-submit).
    const dt = new DataTransfer();
    dt.items.add(f);
    inputEl.files = dt.files;
    showName(f.name);
  });

  // Click-to-browse still works as before — mirror the chosen name.
  inputEl.addEventListener('change', () => {
    showName(inputEl.files.length ? inputEl.files[0].name : '');
  });
}

async function txPatch(url, body) {
  let r;
  try {
    r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  } catch (e) {
    _txErrorToast('Save failed — network error.');
    throw e;
  }
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (!r.ok || (data && data.ok === false)) {
    const detail = (data && data.error) ? data.error : ('HTTP ' + r.status);
    _txErrorToast('Save failed — ' + detail);
    const err = new Error('txPatch failed: ' + detail);
    err.response = data;
    throw err;
  }
  _txSavedToast();
  return data || {ok: true};
}
