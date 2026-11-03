/* PhoneCluster dashboard - small helpers. No dependencies. */
(function () {
  'use strict';
  var PC = (window.PC = window.PC || {});

  /* ---------- escaping / DOM ---------- */
  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  PC.esc = function (s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function (c) { return ESC[c]; });
  };

  PC.qs = function (sel, root) { return (root || document).querySelector(sel); };
  PC.qsa = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  /** h('div', {class:'x', onclick:fn}, child, child) */
  PC.h = function (tag, attrs) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === 'class') node.className = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k === 'text') node.textContent = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
      else if (k.slice(0, 2) === 'on' && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, '');
      else node.setAttribute(k, v);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      if (Array.isArray(c)) {
        c.forEach(function (x) { if (x) node.appendChild(typeof x === 'string' ? document.createTextNode(x) : x); });
      } else node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return node;
  };

  PC.clear = function (node) { while (node && node.firstChild) node.removeChild(node.firstChild); return node; };

  /* ---------- formatting ---------- */
  PC.fmtBytes = function (n) {
    if (n === null || n === undefined || isNaN(n)) return '-';
    n = Number(n);
    if (n < 1024) return n + ' B';
    var units = ['KB', 'MB', 'GB', 'TB', 'PB'], i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return (n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)) + ' ' + units[i];
  };

  PC.fmtNum = function (n, digits) {
    if (n === null || n === undefined || n === '' || isNaN(n)) return '-';
    n = Number(n);
    if (digits !== undefined) return n.toFixed(digits);
    if (Math.abs(n) >= 1000) return n.toLocaleString();
    return String(Math.round(n * 100) / 100);
  };

  PC.fmtDur = function (secs) {
    if (secs === null || secs === undefined || isNaN(secs)) return '-';
    secs = Number(secs);
    if (secs < 1) return (secs * 1000).toFixed(0) + ' ms';
    if (secs < 60) return secs.toFixed(secs < 10 ? 2 : 1) + ' s';
    var m = Math.floor(secs / 60), s = Math.round(secs % 60);
    if (m < 60) return m + 'm ' + s + 's';
    var h = Math.floor(m / 60);
    return h + 'h ' + (m % 60) + 'm';
  };

  /** Unix seconds (float) -> "3m ago" */
  PC.relTime = function (ts) {
    if (!ts && ts !== 0) return '-';
    var now = Date.now() / 1000, d = now - Number(ts), future = d < 0;
    d = Math.abs(d);
    var out;
    if (d < 5) out = 'just now';
    else if (d < 60) out = Math.floor(d) + 's';
    else if (d < 3600) out = Math.floor(d / 60) + 'm';
    else if (d < 86400) out = Math.floor(d / 3600) + 'h';
    else if (d < 86400 * 30) out = Math.floor(d / 86400) + 'd';
    else out = Math.floor(d / (86400 * 30)) + 'mo';
    if (out === 'just now') return out;
    return future ? 'in ' + out : out + ' ago';
  };

  PC.absTime = function (ts) {
    if (!ts && ts !== 0) return '-';
    try { return new Date(Number(ts) * 1000).toLocaleString(); } catch (e) { return String(ts); }
  };

  /** <span data-ts="...">, refreshed by the global ticker */
  PC.timeEl = function (ts) {
    var s = PC.h('span', { class: 'rel', title: PC.absTime(ts) }, PC.relTime(ts));
    if (ts) s.setAttribute('data-ts', ts);
    return s;
  };

  PC.startClock = function () {
    setInterval(function () {
      PC.qsa('[data-ts]').forEach(function (n) {
        var t = n.getAttribute('data-ts');
        n.textContent = PC.relTime(t);
      });
    }, 20000);
  };

  PC.shortId = function (id) { return id ? String(id).slice(0, 8) : '-'; };

  PC.pretty = function (v) {
    try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
  };

  /** Very small JSON highlighter (returns escaped HTML). */
  PC.jsonHtml = function (v) {
    var txt = PC.pretty(v);
    return PC.esc(txt).replace(
      /(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
      function (m, str, colon, kw, num) {
        if (str) return '<span class="j-' + (colon ? 'key' : 'str') + '">' + str + '</span>' + (colon || '');
        if (kw) return '<span class="j-kw">' + kw + '</span>';
        if (num) return '<span class="j-num">' + num + '</span>';
        return m;
      }
    );
  };

  /* ---------- tiny event bus ---------- */
  PC.bus = (function () {
    var map = {};
    return {
      on: function (ev, fn) {
        (map[ev] = map[ev] || []).push(fn);
        return function () { PC.bus.off(ev, fn); };
      },
      off: function (ev, fn) {
        if (!map[ev]) return;
        map[ev] = map[ev].filter(function (f) { return f !== fn; });
      },
      emit: function (ev, data) {
        (map[ev] || []).slice().forEach(function (f) {
          try { f(data); } catch (e) { console.warn('bus handler failed for ' + ev, e); }
        });
      }
    };
  })();

  /* ---------- toasts ---------- */
  PC.toast = function (msg, kind, ms) {
    var root = document.getElementById('toasts');
    if (!root) return;
    var t = PC.h('div', { class: 'toast toast-' + (kind || 'info'), role: 'status' },
      PC.h('span', { class: 'toast-msg' }, String(msg)),
      PC.h('button', { class: 'toast-x', 'aria-label': 'Dismiss', onclick: function () { close(); } }, '×')
    );
    function close() { if (t.parentNode) { t.classList.add('out'); setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 200); } }
    root.appendChild(t);
    setTimeout(close, ms || (kind === 'error' ? 7000 : 3500));
    return t;
  };
  PC.err = function (e) { PC.toast((e && e.message) || String(e), 'error'); };
  PC.ok = function (m) { PC.toast(m, 'ok'); };

  /* ---------- modal ---------- */
  PC.modal = function (opts) {
    var root = document.getElementById('modal-root');
    var prevFocus = document.activeElement;
    var box = PC.h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': opts.title || 'Dialog' });
    var overlay = PC.h('div', { class: 'modal-overlay' }, box);
    var head = PC.h('div', { class: 'modal-head' },
      PC.h('h2', { class: 'modal-title' }, opts.title || ''),
      PC.h('button', { class: 'icon-btn', 'aria-label': 'Close', onclick: function () { close(); } }, '×'));
    var body = PC.h('div', { class: 'modal-body' });
    if (typeof opts.body === 'string') body.innerHTML = opts.body;
    else if (opts.body) body.appendChild(opts.body);
    var foot = PC.h('div', { class: 'modal-foot' });
    (opts.actions || []).forEach(function (a) {
      foot.appendChild(PC.h('button', {
        class: 'btn ' + (a.kind ? 'btn-' + a.kind : ''),
        onclick: function () { try { a.onClick && a.onClick(close); } catch (e) { PC.err(e); } }
      }, a.label));
    });
    box.appendChild(head); box.appendChild(body);
    if (foot.childNodes.length) box.appendChild(foot);
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if (e.key === 'Tab') {
        var f = PC.qsa('button,input,select,textarea,a[href],[tabindex]:not([tabindex="-1"])', box)
          .filter(function (n) { return !n.disabled && n.offsetParent !== null; });
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    overlay.addEventListener('mousedown', function (e) { if (e.target === overlay && opts.dismissable !== false) close(); });
    document.addEventListener('keydown', onKey, true);
    function close() {
      document.removeEventListener('keydown', onKey, true);
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      if (prevFocus && prevFocus.focus) try { prevFocus.focus(); } catch (e) { /* ignore */ }
      if (opts.onClose) opts.onClose();
    }
    root.appendChild(overlay);
    setTimeout(function () {
      var target = PC.qs('[autofocus]', box) || PC.qs('input,select,textarea,button', box);
      if (target) try { target.focus(); } catch (e) { /* ignore */ }
    }, 20);
    return { close: close, body: body, el: box };
  };

  PC.confirm = function (message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var done = false;
      var m = PC.modal({
        title: opts.title || 'Are you sure?',
        body: PC.h('p', { class: 'muted' }, message),
        actions: [
          { label: 'Cancel', onClick: function (c) { done = true; c(); resolve(false); } },
          { label: opts.confirmLabel || 'Confirm', kind: opts.kind || 'danger', onClick: function (c) { done = true; c(); resolve(true); } }
        ],
        onClose: function () { if (!done) resolve(false); }
      });
      return m;
    });
  };

  PC.prompt = function (label, value, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var input = PC.h('input', { class: 'input', value: value === undefined || value === null ? '' : value, autofocus: true, type: opts.type || 'text' });
      var done = false;
      var m = PC.modal({
        title: opts.title || label,
        body: PC.h('label', { class: 'field' }, PC.h('span', { class: 'field-label' }, label), input),
        actions: [
          { label: 'Cancel', onClick: function (c) { done = true; c(); resolve(null); } },
          { label: opts.confirmLabel || 'OK', kind: 'primary', onClick: function (c) { done = true; c(); resolve(input.value); } }
        ],
        onClose: function () { if (!done) resolve(null); }
      });
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); done = true; m.close(); resolve(input.value); }
      });
    });
  };

  /* ---------- clipboard (works on plain http, where navigator.clipboard is absent) ---------- */
  PC.copy = function (text) {
    var ta = PC.h('textarea', { class: 'offscreen' });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    var okd = false;
    try { okd = document.execCommand('copy'); } catch (e) { okd = false; }
    document.body.removeChild(ta);
    if (!okd && navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text)
        .then(function () { PC.ok('Copied'); })
        .catch(function () { PC.toast('Copy failed - select the text manually', 'error'); });
    }
    if (okd) PC.ok('Copied'); else PC.toast('Copy failed - select the text manually', 'error');
    return Promise.resolve();
  };

  /* ---------- misc ---------- */
  PC.debounce = function (fn, ms) {
    var t;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms || 200);
    };
  };

  /** Accept a bare array or a {items|jobs|devices|...: [...]} envelope. */
  PC.asList = function (res, key) {
    if (Array.isArray(res)) return res;
    if (!res || typeof res !== 'object') return [];
    var keys = [key, 'items', 'results', 'data', 'jobs', 'devices', 'tasks', 'files', 'blobs', 'events', 'schedules', 'templates', 'plugins'];
    for (var i = 0; i < keys.length; i++) {
      if (keys[i] && Array.isArray(res[keys[i]])) return res[keys[i]];
    }
    return [];
  };

  PC.STATUS_KINDS = ['waiting', 'pending', 'running', 'done', 'failed', 'cancelled'];
  PC.EVENT_KINDS = ['device_joined', 'device_offline', 'device_online', 'device_removed', 'device_paused',
    'job_submitted', 'job_started', 'job_done', 'job_failed', 'job_cancelled', 'task_failed', 'task_requeued',
    'blob_stored', 'blob_degraded', 'schedule_fired', 'notify_sent', 'error'];

  PC.badge = function (status) {
    return PC.h('span', { class: 'badge badge-' + String(status || 'unknown').replace(/[^a-z_]/gi, '') }, String(status || '-'));
  };
})();
