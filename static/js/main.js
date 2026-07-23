// Personal Financial Tracker — global JS

// Flash alerts are PERSISTENT (no auto-fade) — a real notice stays until the
// user dismisses it. Add a × dismiss button to each.
document.querySelectorAll('.alert').forEach(el => {
  if (el.querySelector('.alert-dismiss')) return;
  const x = document.createElement('button');
  x.className = 'alert-dismiss';
  x.type = 'button';
  x.setAttribute('aria-label', 'Dismiss');
  x.textContent = '×';
  x.style.cssText = 'float:right;background:none;border:none;font-size:18px;'
    + 'line-height:1;cursor:pointer;color:inherit;opacity:.6;margin-left:12px';
  x.onclick = () => el.remove();
  el.insertBefore(x, el.firstChild);
});

// ── Multi-select dropdown filters (L1, L2) ────────────────────────────────
// Markup pattern (rendered by templates/_multi_select.html):
//   <div class="ms-wrap" data-msfor="l1s" data-mslabel="L1 Categories">
//     <button class="ms-btn">L1 Categories: <span class="ms-summary">All</span> <span class="ms-caret">▼</span></button>
//     <div class="ms-panel" hidden>
//       <div class="ms-actions">
//         <button class="ms-action-btn" data-msaction="all">All</button>
//         <button class="ms-action-btn" data-msaction="none">Clear</button>
//       </div>
//       <div class="ms-options">
//         <label class="ms-option"><input type="checkbox" name="l1s" value="Home"> Home</label>
//         ...
//       </div>
//       <div class="ms-footer"><button class="ms-apply">Apply</button></div>
//     </div>
//   </div>
//
// For L2, each option has data-parent="<L1>". When the L2 panel opens, options
// whose parent is not in the currently-checked L1 set are hidden. If no L1 is
// checked, all L2 options are visible.

(function () {
  function ownerForm(el) {
    return el.closest('form');
  }
  function getCheckedL1Set() {
    // Read the current L1 checkbox state from anywhere on the page.
    const set = new Set();
    document.querySelectorAll('.ms-wrap[data-msfor="l1s"] input[type=checkbox]:checked')
      .forEach(cb => set.add(cb.value));
    return set;
  }
  function updateButtonSummary(wrap) {
    const checked = wrap.querySelectorAll('input[type=checkbox]:checked');
    const summary = wrap.querySelector('.ms-summary');
    const btn = wrap.querySelector('.ms-btn');
    if (checked.length === 0) {
      summary.textContent = 'All';
      btn.classList.remove('has-selection');
    } else if (checked.length === 1) {
      // data-label carries the display name when value is an id (accounts).
      summary.textContent = checked[0].dataset.label || checked[0].value;
      btn.classList.add('has-selection');
    } else {
      summary.textContent = `${checked.length} selected`;
      btn.classList.add('has-selection');
    }
  }
  // Two independent things can hide an option — the L1-parent dependency
  // and the type-to-filter search (2026-07-05). Track each as a flag;
  // hidden = either one.
  function setOptHidden(opt, key, val) {
    if (val) opt.dataset[key] = '1'; else delete opt.dataset[key];
    opt.hidden = ('hideParent' in opt.dataset) || ('hideSearch' in opt.dataset);
  }
  function updateEmptyState(wrap) {
    const list = wrap.querySelector('.ms-options');
    const visible = [...wrap.querySelectorAll('.ms-option')].filter(o => !o.hidden).length;
    let empty = list.querySelector('.ms-empty');
    if (visible === 0) {
      if (!empty) {
        empty = document.createElement('div');
        empty.className = 'ms-empty';
        list.appendChild(empty);
      }
      empty.textContent = 'No matches.';
    } else if (empty) {
      empty.remove();
    }
  }
  function applySearch(wrap) {
    const q = (wrap.querySelector('.ms-search')?.value || '').trim().toLowerCase();
    wrap.querySelectorAll('.ms-option').forEach(opt => {
      // Match: any WORD in the label starts with the query ("gro" →
      // Groceries; "ent" → Entertainment).
      const words = opt.textContent.trim().toLowerCase().split(/[^a-z0-9]+/);
      const hit = !q || words.some(w => w.startsWith(q));
      setOptHidden(opt, 'hideSearch', !hit);
    });
    updateEmptyState(wrap);
  }
  function refreshL2Visibility(wrap) {
    // Only meaningful for the L2 wrapper.
    if (wrap.dataset.msfor !== 'l2s') return;
    const l1set = getCheckedL1Set();
    const noL1 = l1set.size === 0;
    wrap.querySelectorAll('.ms-option').forEach(opt => {
      // data-parent is a comma-separated list of parent L1 names (same L2
      // can have multiple parents — e.g. "Miscellaneous" lives under several
      // L1s). Show this L2 if ANY of its parents is in the selected L1 set.
      const parents = (opt.dataset.parent || '').split(',').filter(x => x);
      const show = noL1 || parents.length === 0 || parents.some(p => l1set.has(p));
      setOptHidden(opt, 'hideParent', !show);
    });
    updateEmptyState(wrap);
  }

  document.querySelectorAll('.ms-wrap').forEach(wrap => {
    const btn   = wrap.querySelector('.ms-btn');
    const panel = wrap.querySelector('.ms-panel');
    const applyBtn = wrap.querySelector('.ms-apply');
    const actionBtns = wrap.querySelectorAll('.ms-action-btn');

    updateButtonSummary(wrap);

    btn.addEventListener('click', e => {
      e.preventDefault();
      const wasOpen = !panel.hidden;
      // Close all other panels first
      document.querySelectorAll('.ms-panel').forEach(p => p.hidden = true);
      panel.hidden = wasOpen;
      if (!panel.hidden) {
        if (wrap.dataset.msfor === 'l2s') refreshL2Visibility(wrap);
        // Fresh search on every open; focus it for immediate typing
        // (2026-07-05).
        const s = wrap.querySelector('.ms-search');
        if (s) { s.value = ''; applySearch(wrap); s.focus(); }
      }
    });

    // Type-to-filter (2026-07-05)
    const search = wrap.querySelector('.ms-search');
    if (search) {
      search.addEventListener('input', () => applySearch(wrap));
      search.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); applyBtn?.click(); }
        else if (e.key === 'Escape') { panel.hidden = true; }
      });
    }

    // Checkbox change → summary + (for L1) trigger L2 recompute if open
    wrap.querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.addEventListener('change', () => {
        updateButtonSummary(wrap);
        if (wrap.dataset.msfor === 'l1s') {
          // If the L2 panel is also open, refresh it.
          document.querySelectorAll('.ms-wrap[data-msfor="l2s"]').forEach(l2wrap => {
            if (!l2wrap.querySelector('.ms-panel').hidden) {
              refreshL2Visibility(l2wrap);
            }
          });
        }
      });
    });

    // All / Clear
    actionBtns.forEach(b => {
      b.addEventListener('click', e => {
        e.preventDefault();
        const action = b.dataset.msaction;
        wrap.querySelectorAll('.ms-option').forEach(opt => {
          if (opt.hidden) return;  // only operate on visible options
          const cb = opt.querySelector('input[type=checkbox]');
          cb.checked = (action === 'all');
        });
        updateButtonSummary(wrap);
      });
    });

    // Apply → close panel + submit form
    if (applyBtn) {
      applyBtn.addEventListener('click', e => {
        e.preventDefault();
        panel.hidden = true;
        const form = ownerForm(wrap);
        if (form) form.submit();
      });
    }
  });

  // Close panels on outside click
  document.addEventListener('click', e => {
    if (!e.target.closest('.ms-wrap')) {
      document.querySelectorAll('.ms-panel').forEach(p => p.hidden = true);
    }
  });
  // Escape closes panels
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      document.querySelectorAll('.ms-panel').forEach(p => p.hidden = true);
    }
  });
})();
