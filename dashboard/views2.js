/* PhoneCluster dashboard - Files, Storage, Terminal, Schedules, Events, Settings, Join. */
(function () {
  'use strict';
  var PC = (window.PC = window.PC || {});
  var views = PC.views;
  var h = PC.h;
  var page = PC.page, table = PC.table;

  function emptyRow(cols, text) { return h('tr', {}, h('td', { colspan: String(cols), class: 'empty' }, text)); }

  function dropZone(label, onFiles) {
    var picker = h('input', { type: 'file', class: 'offscreen', multiple: true });
    var zone = h('div', {
      class: 'dropzone', tabindex: '0', role: 'button', 'aria-label': label,
      onclick: function () { picker.click(); },
      onkeydown: function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); picker.click(); } }
    }, h('span', {}, label), picker);
    ['dragenter', 'dragover'].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); zone.classList.add('over'); });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); zone.classList.remove('over'); });
    });
    zone.addEventListener('drop', function (e) {
      var files = e.dataTransfer && e.dataTransfer.files ? Array.prototype.slice.call(e.dataTransfer.files) : [];
      if (files.length) onFiles(files);
    });
    picker.addEventListener('change', function () {
      var files = Array.prototype.slice.call(picker.files || []);
      picker.value = '';
      if (files.length) onFiles(files);
    });
    return zone;
  }

  function uploadQueue(files, uploader, done) {
    var bar = h('div', { class: 'upload-bar' });
    var label = h('span', { class: 'muted' }, '');
    var prog = h('div', { class: 'progress' }, h('div', { class: 'progress-done', style: { width: '0%' } }));
    bar.appendChild(label); bar.appendChild(prog);
    var fill = prog.firstChild;
    var i = 0;
    function next() {
      if (i >= files.length) { if (bar.parentNode) bar.parentNode.removeChild(bar); done(); return; }
      var f = files[i];
      label.textContent = 'Uploading ' + f.name + ' (' + (i + 1) + '/' + files.length + ')';
      uploader(f, function (frac) { fill.style.width = Math.round(frac * 100) + '%'; })
        .then(function () { i++; fill.style.width = '0%'; next(); })
        .catch(function (e) { PC.err(e); i++; next(); });
    }
    next();
    return bar;
  }

  /* =========================================================================
     Files
     ========================================================================= */
  views.files = {
    title: 'Files',
    render: function (root) {
      root.appendChild(page('Files', [
        h('button', { class: 'btn', title: 'Where Save puts files: data\\downloads in the project folder',
          onclick: function () { PC.api.openDownloads(); } }, 'Open downloads folder'),
        h('button', { class: 'btn', onclick: function () { load(); } }, 'Refresh')]));
      root.appendChild(h('p', { class: 'muted' }, 'Save copies a file into data\\downloads in the project folder ',
        '(the PC writes it there itself; nothing goes to the browser\'s Downloads folder).'));
      var zone = dropZone('Drop files here, or click to choose', function (files) {
        var bar = uploadQueue(files, function (f, onp) { return PC.api.uploadFile(f, onp); }, function () { PC.ok('Upload complete'); load(); });
        root.insertBefore(bar, t.el);
      });
      root.appendChild(zone);
      var t = table(['Name', 'Size', 'Type', 'Created', 'ID', '']);
      root.appendChild(t.el);

      function load() {
        PC.loadFiles(true).then(draw).catch(PC.err);
      }
      function draw(list) {
        PC.clear(t.body);
        if (!list || !list.length) { t.body.appendChild(emptyRow(6, 'The file store is empty.')); return; }
        list.slice().sort(function (a, b) { return (b.created || 0) - (a.created || 0); }).forEach(function (f) {
          t.body.appendChild(h('tr', {},
            h('td', {}, f.name || '-'),
            h('td', {}, PC.fmtBytes(f.size)),
            h('td', {}, f.mime || '-'),
            h('td', {}, PC.timeEl(f.created)),
            h('td', {}, h('code', { class: 'tiny', title: f.id }, PC.shortId(f.id))),
            h('td', { class: 'right' }, h('div', { class: 'row gap end' },
              h('button', { class: 'btn btn-sm', title: 'Save into data\\downloads', onclick: function () { PC.api.save('/files/' + encodeURIComponent(f.id) + '/save', f.name); } }, 'Save'),
              h('button', {
                class: 'btn btn-sm btn-danger', onclick: function () {
                  PC.confirm('Delete "' + (f.name || f.id) + '" from the file store?', { title: 'Delete file', confirmLabel: 'Delete' })
                    .then(function (yes) { if (yes) PC.api.deleteFile(f.id).then(function () { PC.ok('Deleted'); load(); }).catch(PC.err); });
                }
              }, 'Delete')))));
        });
      }
      load();
      return function () { };
    }
  };

  /* =========================================================================
     Storage (pooled blobs)
     ========================================================================= */
  views.storage = {
    title: 'Storage',
    render: function (root) {
      /* protection (SPEC 10): automatic = parity pieces (erasure coding) from 3 storage phones on */
      var repl = h('select', { class: 'input compact', title: 'How stored files survive a lost phone' },
        h('option', { value: 'auto' }, 'automatic (parity pieces from 3 phones)'),
        [1, 2, 3, 4, 5].map(function (n) { return h('option', { value: String(n) }, n + ' full cop' + (n === 1 ? 'y' : 'ies')); }));
      repl.value = 'auto';
      root.appendChild(page('Pooled storage', [repl, h('button', { class: 'btn', onclick: function () { load(); } }, 'Refresh')]));
      root.appendChild(h('p', { class: 'muted' }, 'Files uploaded here are split into chunks and spread across phones with the "storage" capability. ',
        'With three or more storage phones each group of chunks gets a parity piece instead of a second copy: ',
        'losing any one phone loses nothing, for about 1.3x the space instead of 2x. Every piece is re-checked on its phone once a day ',
        'and a damaged one is rebuilt from the others.'));
      var zone = dropZone('Drop files to store in the pool, or click to choose', function (files) {
        var auto = repl.value === 'auto';
        var r = auto ? null : parseInt(repl.value, 10);
        var bar = uploadQueue(files, function (f, onp) { return PC.api.uploadBlob(f, r, onp, auto ? 'auto' : 'copies'); },
          function () { PC.ok('Stored'); load(); });
        root.insertBefore(bar, t.el);
      });
      root.appendChild(zone);
      var t = table(['Name', 'Size', 'Status', 'Chunks', 'Protection', 'Created', '']);
      root.appendChild(t.el);

      function load() { PC.api.storage().then(draw).catch(PC.err); }

      function draw(list) {
        PC.clear(t.body);
        if (!list || !list.length) { t.body.appendChild(emptyRow(7, 'Nothing in the pool yet.')); return; }
        list.slice().sort(function (a, b) { return (b.created || 0) - (a.created || 0); }).forEach(function (b) {
          var detailRow = h('tr', { class: 'detail-row', hidden: true }, h('td', { colspan: '7' }, h('div', { class: 'muted' }, 'Loading chunk map…')));
          var tr = h('tr', {},
            h('td', {}, b.name || PC.shortId(b.id)),
            h('td', {}, PC.fmtBytes(b.size)),
            h('td', {}, PC.badge(b.status)),
            h('td', {}, String(b.n_chunks === undefined ? '-' : b.n_chunks)),
            h('td', { title: b.protection ? 'takes ' + b.protection.overhead + 'x the file size; survives losing ' + b.protection.survives + ' phone(s)' : '' },
              b.protection ? b.protection.label : String(b.replication === undefined ? '-' : b.replication)),
            h('td', {}, PC.timeEl(b.created)),
            h('td', { class: 'right' }, h('div', { class: 'row gap end' },
              h('button', { class: 'btn btn-sm', onclick: toggle }, 'Chunks'),
              h('button', { class: 'btn btn-sm', title: 'Read it back from the phones into data\\downloads', onclick: function () { PC.api.save('/storage/' + encodeURIComponent(b.id) + '/save'); } }, 'Save'),
              h('button', {
                class: 'btn btn-sm btn-danger', onclick: function () {
                  PC.confirm('Delete "' + (b.name || b.id) + '" from the pool? Chunks are removed from every device.',
                    { title: 'Delete blob', confirmLabel: 'Delete' })
                    .then(function (yes) { if (yes) PC.api.deleteBlob(b.id).then(function () { PC.ok('Deleting'); load(); }).catch(PC.err); });
                }
              }, 'Delete'))));
          t.body.appendChild(tr);
          t.body.appendChild(detailRow);

          function toggle() {
            if (!detailRow.hidden) { detailRow.hidden = true; return; }
            detailRow.hidden = false;
            PC.api.storageStatus(b.id).then(function (st) {
              PC.clear(detailRow.firstChild);
              detailRow.firstChild.appendChild(chunkMap(st, b));
            }).catch(function (e) {
              PC.clear(detailRow.firstChild);
              detailRow.firstChild.appendChild(h('span', { class: 'error-text' }, 'Could not load chunk status: ' + (e.message || e)));
            });
          }
        });
      }

      function chunkMap(st, blob) {
        var wrap = h('div', { class: 'chunkmap-wrap' });
        var chunks = PC.asList(st && st.chunks ? st.chunks : st, 'chunks');
        var ec = st && st.scheme === 'ec';
        var want = ec ? 1 : ((st && st.replication) || blob.replication || 1);
        var parity = ec ? PC.asList(st.parity, 'parity') : [];
        wrap.appendChild(h('div', { class: 'muted small' },
          'status ' + ((st && st.status) || blob.status || '?') + ' · ' + chunks.length + ' chunks' +
          (ec ? ' + ' + parity.length + ' parity pieces (' + st.ec_k + ' data + ' + st.ec_m + ' parity per group, each on its own phone)'
              : ' · target replication ' + want)));
        if (!chunks.length) { wrap.appendChild(h('div', { class: 'muted' }, 'No chunk detail reported.')); return wrap; }
        var map = h('div', { class: 'chunkmap' });
        chunks.concat(parity.map(function (p) { p.parity = true; return p; })).forEach(function (c, i) {
          var locs = c.locations || c.devices || [];
          var n = Array.isArray(locs) ? locs.length : Number(locs) || 0;
          var cls = n === 0 ? 'lost' : n < want ? 'degraded' : 'ok';
          var names = (Array.isArray(locs) ? locs : []).map(function (l) {
            var did = (l && typeof l === 'object') ? (l.device_id || l.id) : l;
            var dev = PC.store.devices.filter(function (d) { return d.id === did; })[0];
            return dev ? (dev.name || PC.shortId(did)) : PC.shortId(did);
          });
          map.appendChild(h('span', {
            class: 'chunk ' + cls + (c.parity ? ' parity' : ''),
            title: (c.parity ? 'parity piece ' + c.j + ' of group ' + c.stripe : 'chunk ' + (c.idx === undefined ? i : c.idx)) +
              ' · ' + n + ' cop' + (n === 1 ? 'y' : 'ies') + (names.length ? ' · ' + names.join(', ') : '')
          }, ''));
        });
        wrap.appendChild(map);
        wrap.appendChild(h('div', { class: 'muted small legend' },
          h('span', { class: 'chunk ok' }, ''), ' stored ',
          h('span', { class: 'chunk ok parity' }, ''), ' parity piece ',
          h('span', { class: 'chunk degraded' }, ''), ' under-replicated ',
          h('span', { class: 'chunk lost' }, ''), ' no copy online'));
        return wrap;
      }

      load();
      PC.loadDevices().catch(function () { });
      return function () { };
    }
  };

  /* =========================================================================
     Terminal
     ========================================================================= */
  /* Several terminals at once, on different devices or the same one: each has a
     tab and keeps running while another is on screen, and the view switch shows
     one, 2, 3... or all of them, with a picker in each section (SPEC §14).

     A shell outlives this page: leaving Terminal (or reloading the tab) keeps it
     open on the device, and coming back picks it up again with its scrollback
     from the coordinator.  The sessions, the view and the command history are
     remembered per browser tab (sessionStorage, else memory); shells this tab
     doesn't know about (another tab's, a closed tab's) are listed with Resume /
     Close.  The coordinator closes a shell 30 idle minutes after no dashboard tab
     has it open.

     Two kinds of shell (SPEC §10).  Full terminal: xterm.js (served from
     /static/vendor/xterm, loaded the first time it is needed) on a pseudo-terminal
     on the device, for devices whose worker has the "pty" capability: keystrokes
     go out as typed, so htop, vim and Ctrl+C work.  Line mode: the shell reads
     whole lines from a pipe; the page shows a log and an input line. */
  var TERM_KEY = 'pc_terminal', TERM_HISTORY_KEY = 'pc_terminal_history', TERM_MODE_KEY = 'pc_terminal_mode';
  var XTERM_DIR = '/static/vendor/xterm/', XTERM_V = '6';
  var termMemory = {}, xtermReady = null;

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = resolve;
      s.onerror = function () { reject(new Error('could not load ' + src)); };
      document.head.appendChild(s);
    });
  }

  /* xterm.js and its fit addon, once per page load; the monospace font is
     loaded first so the terminal measures its cells with it. */
  function loadXterm() {
    if (xtermReady) return xtermReady;
    if (!document.querySelector('link[data-xterm]')) {
      document.head.appendChild(h('link', { rel: 'stylesheet', href: XTERM_DIR + 'xterm.css?v=' + XTERM_V, 'data-xterm': '1' }));
    }
    xtermReady = (window.Terminal ? Promise.resolve() : loadScript(XTERM_DIR + 'xterm.js?v=' + XTERM_V))
      .then(function () { return window.FitAddon ? null : loadScript(XTERM_DIR + 'addon-fit.js?v=' + XTERM_V); })
      .then(function () {
        if (document.fonts && document.fonts.load) {
          return Promise.all([document.fonts.load('13px "JetBrains Mono"'), document.fonts.load('bold 13px "JetBrains Mono"')])
            .catch(function () { /* the fallback font will do */ });
        }
      })
      .then(function () {
        if (!window.Terminal || !window.FitAddon) throw new Error('xterm.js did not load');
      });
    xtermReady.catch(function () { xtermReady = null; });
    return xtermReady;
  }

  /* Terminal colours that sit on the page's code background in either theme. */
  var TERM_PALETTE = {
    dark: {
      black: '#2a2a30', red: '#f0706a', green: '#4cc38a', yellow: '#e2b04a', blue: '#6ea8fe',
      magenta: '#c792ea', cyan: '#5fd0d6', white: '#d4d4d8', brightBlack: '#6c6c75', brightRed: '#ff8f88',
      brightGreen: '#6fdba4', brightYellow: '#f0c66b', brightBlue: '#93bfff', brightMagenta: '#d8a8f5',
      brightCyan: '#85e3e8', brightWhite: '#fafafa', selectionBackground: 'rgba(124,133,255,.35)'
    },
    light: {
      black: '#27272a', red: '#cf3b35', green: '#1f8a5b', yellow: '#8a5a00', blue: '#2f6fdc',
      magenta: '#8250df', cyan: '#1b7c83', white: '#6e7781', brightBlack: '#57606a', brightRed: '#a40e26',
      brightGreen: '#1a7f37', brightYellow: '#633c01', brightBlue: '#1f63c6', brightMagenta: '#a475f9',
      brightCyan: '#3192aa', brightWhite: '#8c959f', selectionBackground: 'rgba(94,106,210,.25)'
    }
  };

  function termTheme() {
    var css = getComputedStyle(document.documentElement);
    var light = (css.colorScheme || '').indexOf('light') >= 0;
    var bg = css.getPropertyValue('--code-bg').trim() || (light ? '#f6f6f8' : '#0e0e10');
    var fg = css.getPropertyValue('--code-fg').trim() || (light ? '#27272a' : '#d4d4d8');
    var theme = { background: bg, foreground: fg, cursor: fg, cursorAccent: bg };
    var palette = TERM_PALETTE[light ? 'light' : 'dark'];
    Object.keys(palette).forEach(function (k) { theme[k] = palette[k]; });
    return theme;
  }

  function stripAnsi(text) {
    return String(text || '').replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[@-_]/g, '');
  }

  function termLoad(key, fallback) {
    try {
      var raw = sessionStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) {
      return termMemory[key] !== undefined ? termMemory[key] : fallback;
    }
  }

  function termSave(key, value) {
    termMemory[key] = value;
    try {
      if (value === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* memory only */ }
  }

  /* What this tab has open (a page from before several terminals saved just one). */
  function termSaved() {
    var saved = termLoad(TERM_KEY, null) || {};
    if (saved.session_id && !saved.sessions) {
      return { sessions: [saved.session_id], view: [saved.session_id], active: saved.session_id };
    }
    return saved;
  }

  /* A terminal this tab has open must not close as idle while you work in another
     one or on another dashboard page, so the timer runs on every page.  Once the
     tab is closed the coordinator's 30 idle minutes start. */
  setInterval(function () {
    var ids = termSaved().sessions || [];
    if (ids.length) PC.ws.termKeepOpen(ids);
  }, 60000);

  views.terminal = {
    title: 'Terminal',
    render: function (root) {
      var panes = [];           // every terminal on this page, in tab order
      var shown = [];           // the panes on screen, in the order of their sections
      var active = null;        // the pane the keys go to
      var layout = 1;           // how many sections: 1, 2, 3... or 'all'
      var byId = {};            // session id -> pane
      var nextKey = 1;
      var opening = 0, early = {};
      var devices = {}, gone = false, socketWasDown = false;
      var history = termLoad(TERM_HISTORY_KEY, []);

      var devSel = h('select', { class: 'input compact', 'aria-label': 'Device', onchange: syncMode });
      var modeSel = h('select', {
        class: 'input compact', 'aria-label': 'Terminal kind',
        onchange: function () { termSave(TERM_MODE_KEY, modeSel.value); }
      },
        h('option', { value: 'full' }, 'Full terminal'),
        h('option', { value: 'line' }, 'Line mode'));
      var openBtn = h('button', { class: 'btn btn-primary', onclick: open }, 'Open terminal');
      root.appendChild(page('Terminal', [devSel, modeSel, openBtn]));
      root.appendChild(h('p', { class: 'muted' },
        'Open as many terminals as you like, on different devices or the same one. Each keeps running while you look at ' +
        'another: switch with the tabs, or use Show to see 2, 3 or all of them at once. ' +
        'Full terminal: keys go to the device as you type, so htop, vim, top and Ctrl+C work. ' +
        'Line mode sends whole commands, for devices whose worker has no full terminal. ' +
        'A terminal stays open until you close it, or 30 minutes after no dashboard tab has it open.'));
      var openShells = h('div', { class: 'terminal-open-shells' });
      root.appendChild(openShells);

      var tabs = h('div', { class: 'term-tabs' });
      var viewBar = h('div', { class: 'term-view', role: 'group', 'aria-label': 'How many terminals to show at once', hidden: true });
      var empty = h('div', { class: 'term-empty muted' }, 'No terminal open. Pick a device above and press Open terminal.');
      var box = h('div', { class: 'term-panes' });
      // keys a phone's keyboard lacks; they go to the active terminal
      var keys = h('div', { class: 'terminal-keys', hidden: true }, [
        ['Esc', '\x1b'], ['Tab', '\t'], ['Ctrl+C', '\x03'], ['Ctrl+D', '\x04'], ['Ctrl+Z', '\x1a'],
        ['↑', 'A'], ['↓', 'B'], ['←', 'D'], ['→', 'C']
      ].map(function (k) {
        return h('button', {
          class: 'btn btn-sm', type: 'button', tabindex: '-1',
          onmousedown: function (e) { e.preventDefault(); },  // keep the terminal focused
          onclick: function () {
            if (!active) return;
            var data = k[1];
            if (data.length === 1 && /[A-D]/.test(data)) {
              // arrows: full-screen programs switch the terminal to "application" cursor keys
              data = (active.xt && active.xt.term.modes.applicationCursorKeysMode ? '\x1bO' : '\x1b[') + data;
            }
            send(active, data);
            focusPane(active);
          }
        }, k[0]);
      }));
      var status = h('div', { class: 'muted small' });
      root.appendChild(h('div', { class: 'card terminal-card' },
        h('div', { class: 'term-bar' }, tabs, viewBar), empty, box, keys, status));

      /* All the xterm.js terminals of this page share these two watchers. */
      var fitWatch = window.ResizeObserver ? new ResizeObserver(fitShown) : null;
      window.addEventListener('resize', fitShown);
      var recolour = function () {
        panes.forEach(function (p) { if (p.xt) p.xt.term.options.theme = termTheme(); });
      };
      var themeWatch = new MutationObserver(recolour);
      themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
      var media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
      if (media && media.addEventListener) media.addEventListener('change', recolour);

      PC.loadDevices().then(function (list) {
        PC.clear(devSel);
        list.forEach(function (d) { devices[d.id] = d; });
        var usable = list.filter(function (d) { return d.platform !== 'browser'; });
        if (!usable.length) devSel.appendChild(h('option', { value: '' }, 'no devices with a shell'));
        usable.forEach(function (d) {
          devSel.appendChild(h('option', { value: d.id },
            (d.name || PC.shortId(d.id)) + ' (' + (d.platform || '?') + ', ' + (d.status || '?') + ')'));
        });
        syncMode();
        refresh();
        listOpenShells();
      }).catch(PC.err);

      function hasPty(id) {
        var d = devices[id];
        return !!(d && (d.caps || []).indexOf('pty') >= 0);
      }

      /* Full terminal where the device's worker offers one, else line mode. */
      function syncMode() {
        var ok = hasPty(devSel.value);
        modeSel.disabled = !ok;
        modeSel.title = ok ? '' : 'This device\'s worker has no full terminal (an older worker updates itself when idle)';
        modeSel.value = ok ? (termLoad(TERM_MODE_KEY, 'full') === 'line' ? 'line' : 'full') : 'line';
      }

      function deviceName(id) {
        var d = devices[id];
        return d ? (d.name || PC.shortId(d.id)) : PC.shortId(id);
      }

      /* "Redmi 5A", and "Redmi 5A (2)" for a second terminal on it. */
      function label(p) {
        if (!p.deviceId) return 'Terminal';
        return deviceName(p.deviceId) + (p.number > 1 ? ' (' + p.number + ')' : '');
      }

      function setDevice(p, deviceId) {
        if (p.deviceId === deviceId && p.number) return;
        p.deviceId = deviceId;
        var used = panes.filter(function (q) { return q !== p && q.deviceId === deviceId; })
          .map(function (q) { return q.number; });
        p.number = 1;
        while (used.indexOf(p.number) >= 0) p.number++;
      }

      /* ---------------- one terminal ---------------- */
      function newPane(deviceId) {
        var p = {
          key: 't' + (nextKey++), id: null, deviceId: null, number: 0,
          full: false, alive: false, activity: false, calmUntil: 0,
          lastPos: 0, attaching: false, pending: [],
          xt: null, sentSize: '', resizeTimer: null, wantRedraw: false, histPos: history.length
        };
        if (deviceId) setDevice(p, deviceId);
        // line mode: a log and an input line
        p.out = h('pre', { class: 'terminal-out', tabindex: '0' });
        p.input = h('input', {
          class: 'input mono terminal-in', placeholder: 'type a command and press Enter', disabled: true,
          autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false'
        });
        p.inputRow = h('div', { class: 'row gap terminal-row' }, h('span', { class: 'prompt' }, '$'), p.input);
        p.input.addEventListener('keydown', function (e) { lineKey(p, e); });
        // full terminal: xterm.js in a mount without padding, so the fit addon measures the real space
        p.xmount = h('div', { class: 'terminal-xterm-mount' });
        p.xbox = h('div', { class: 'terminal-xterm', hidden: true }, p.xmount);
        // in split view: which terminal this section shows
        p.pick = h('select', {
          class: 'input compact term-pick', 'aria-label': 'Terminal shown in this section',
          onchange: function () { place(p, p.pick.value); }
        });
        p.size = h('span', { class: 'muted small term-size' });
        p.head = h('div', { class: 'term-pane-head', hidden: true }, p.pick, p.size,
          h('button', {
            class: 'term-x', type: 'button', title: 'Close this terminal', 'aria-label': 'Close this terminal',
            onclick: function () { closeTab(p); }
          }, '×'));
        p.el = h('div', { class: 'term-pane off' }, p.head, p.out, p.inputRow, p.xbox);
        p.el.addEventListener('focusin', function () { setActive(p); });
        box.appendChild(p.el);
        panes.push(p);
        return p;
      }

      function setFull(p, on) {
        p.full = !!on;
        p.xbox.hidden = !p.full;
        p.out.hidden = p.full;
        p.inputRow.hidden = p.full;
      }

      /* The xterm.js terminal of a pane. */
      function terminal(p) {
        if (p.xt) return p.xt;
        var term = new window.Terminal({
          fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--mono').trim() || 'monospace',
          fontSize: 13, cursorBlink: true, scrollback: 5000, theme: termTheme(), macOptionIsMeta: true, disableStdin: true
        });
        var fit = new window.FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(p.xmount);
        term.onData(function (data) { if (p.full) send(p, data); });
        term.onResize(function () { queueResize(p, false); });
        // Ctrl+C copies when text is selected (and is ^C otherwise); Ctrl+V pastes
        term.attachCustomKeyEventHandler(function (e) {
          if (e.type !== 'keydown') return true;
          if ((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C') && term.hasSelection()) {
            var text = term.getSelection();
            if (navigator.clipboard) navigator.clipboard.writeText(text).catch(function () { });
            term.clearSelection();
            return false;
          }
          // Ctrl+letter from a source that gives the key but no key code (some
          // remote-desktop and automation tools): xterm maps only key codes
          if (e.ctrlKey && !e.altKey && !e.metaKey && !e.keyCode && /^[a-z]$/i.test(e.key || '')) {
            e.preventDefault();
            if (p.full) send(p, String.fromCharCode(e.key.toUpperCase().charCodeAt(0) - 64));
            return false;
          }
          return true;
        });
        p.xt = { term: term, fit: fit };
        if (fitWatch) fitWatch.observe(p.xbox);
        return p.xt;
      }

      function dropTerminal(p) {
        if (!p.xt) return;
        if (fitWatch) fitWatch.unobserve(p.xbox);
        var term = p.xt.term;
        p.xt = null;
        // after xterm's own pending frame (queued when it opened), which fails
        // on a terminal that is already gone (a page left before it drew)
        var drop = function () { try { term.dispose(); } catch (e) { /* already gone */ } };
        if (window.requestAnimationFrame) requestAnimationFrame(drop); else drop();
      }

      /* A terminal off screen keeps its size: it only changes when it is shown, so
         the program on it is not redrawn for nothing. */
      function fitNow(p) {
        if (!p.xt || !p.full || gone || shown.indexOf(p) < 0) return;
        try { p.xt.fit.fit(); } catch (e) { /* not laid out yet */ }
      }

      function fitShown() { shown.forEach(fitNow); }

      /* Tell the device the new size (once it settles); `redraw` makes the
         program repaint, for a page picking a session up again. */
      function queueResize(p, redraw) {
        if (!p.full || !p.xt) return;
        showSize(p);
        if (redraw) p.wantRedraw = true;
        clearTimeout(p.resizeTimer);
        p.resizeTimer = setTimeout(function () {
          if (!p.id || !p.alive || !p.xt) return;
          var cols = p.xt.term.cols, rows = p.xt.term.rows, size = cols + 'x' + rows, again = p.wantRedraw;
          if (size === p.sentSize && !again) return;
          p.sentSize = size;
          p.wantRedraw = false;
          if (!PC.ws.termResize(p.id, cols, rows, again)) {
            PC.api.resizeTerminal(p.id, cols, rows, again).catch(function () { });
          }
        }, redraw ? 0 : 120);
      }

      function showSize(p) {
        p.size.textContent = !p.id ? '' : (!p.full ? 'line mode' : (p.xt ? p.xt.term.cols + '×' + p.xt.term.rows : ''));
        if (p === active) showStatus();
      }

      function send(p, data) {
        if (!p.id || !p.alive) return;
        if (!PC.ws.termInput(p.id, data)) {
          status.textContent = 'Not connected: the WebSocket is down, keys were not sent.';
        }
      }

      function write(p, txt) {
        if (p.full && p.xt) { p.xt.term.write(txt); return; }
        var atBottom = p.out.scrollTop + p.out.clientHeight >= p.out.scrollHeight - 30;
        p.out.appendChild(document.createTextNode(txt));
        if (atBottom) p.out.scrollTop = p.out.scrollHeight;
      }

      /* A line of our own between the shell's output. */
      function note(p, text) {
        if (p.full && p.xt) p.xt.term.write('\r\n\x1b[2m*** ' + text + ' ***\x1b[0m\r\n');
        else write(p, '*** ' + text + ' ***\n');
      }

      function goLive(p) {
        p.alive = true;
        p.input.disabled = p.full;
        if (p.xt) {
          p.xt.term.options.disableStdin = !p.full;
          p.xt.term.options.cursorBlink = true;
        }
      }

      function focusPane(p) {
        if (!p || gone) return;
        if (p.full && p.xt) p.xt.term.focus();
        else if (!p.input.disabled) p.input.focus();
      }

      /* Live output.  `pos` counts every character the session ever held, so
         output already in the replayed scrollback is skipped. */
      function onTerm(m) {
        var p = byId[m.session_id];
        if (!p) {
          // the shell banner can arrive over the WebSocket before the open
          // response says whose it is
          if (opening && m.session_id) (early[m.session_id] = early[m.session_id] || []).push(m);
          return;
        }
        if (p.attaching) { p.pending.push(m); return; }
        if (m.pos && m.pos <= p.lastPos) return;
        write(p, m.data || '');
        if (m.pos) p.lastPos = m.pos;
        // a tab off screen shows it has news (not the repaint a replay asks for)
        if (shown.indexOf(p) < 0 && !p.activity && Date.now() > p.calmUntil) { p.activity = true; renderTabs(); }
      }

      /* ---------------- which terminals are on screen ---------------- */
      function sections() {
        return layout === 'all' ? panes.length : Math.min(layout, panes.length);
      }

      /* Fill the sections and lay them out.  The panes stay where they are in the
         page, so a terminal off screen keeps its screen, scrollback and program. */
      function arrange() {
        var want = sections();
        shown = shown.filter(function (p) { return panes.indexOf(p) >= 0; });
        if (panes.indexOf(active) < 0) active = null;
        while (shown.length > want) {
          // fewer sections: the active terminal stays
          var last = shown.length - 1;
          shown.splice(shown[last] === active ? last - 1 : last, 1);
        }
        panes.forEach(function (p) {
          if (shown.length < want && shown.indexOf(p) < 0) shown.push(p);
        });
        if (shown.indexOf(active) < 0) active = shown[0] || null;

        var n = shown.length, cols = n < 2 ? 1 : (n <= 4 ? 2 : 3), rest = n % cols;
        box.style.setProperty('--term-cols', String(cols));
        box.setAttribute('data-rows', n > cols ? 'many' : 'one');
        panes.forEach(function (p) {
          var at = shown.indexOf(p);
          p.el.classList.toggle('off', at < 0);
          p.el.style.order = at < 0 ? '' : String(at);
          // an odd one out takes the rest of its row
          p.el.style.gridColumn = rest && at === n - 1 ? 'span ' + (cols - rest + 1) : '';
          p.head.hidden = n < 2;
          if (at >= 0) p.activity = false;
        });
        fitShown();
        refresh();
      }

      /* Tabs, section pickers, status and what this tab remembers. */
      function refresh() {
        panes.forEach(function (p) {
          PC.clear(p.pick);
          panes.forEach(function (q) {
            var option = h('option', { value: q.key }, label(q) + (q.id && !q.alive ? ' (closed)' : ''));
            if (q === p) option.selected = true;
            p.pick.appendChild(option);
          });
          showSize(p);
        });
        empty.hidden = panes.length > 0;
        renderView();
        markActive();
        save();
      }

      function renderTabs() {
        PC.clear(tabs);
        panes.forEach(function (p) {
          var cls = 'term-tab' + (p === active ? ' active' : '') + (shown.indexOf(p) >= 0 ? ' in-view' : '') +
            (p.id && !p.alive ? ' ended' : '') + (p.activity ? ' activity' : '');
          tabs.appendChild(h('div', { class: cls },
            h('button', {
              class: 'term-tab-main', type: 'button', 'aria-current': p === active ? 'true' : null,
              title: p.activity ? 'New output' : null, onclick: function () { showPane(p); }
            }, h('span', { class: 'term-dot', 'aria-hidden': 'true' }), h('span', { class: 'term-tab-name' }, label(p))),
            h('button', {
              class: 'term-x', type: 'button', title: 'Close ' + label(p), 'aria-label': 'Close ' + label(p),
              onclick: function () { closeTab(p); }
            }, '×')));
        });
      }

      /* One / 2 / 3 ... / All */
      function renderView() {
        PC.clear(viewBar);
        viewBar.hidden = panes.length < 2;
        if (viewBar.hidden) return;
        var current = layout === 'all' ? 'all' : Math.min(layout, panes.length);
        var choices = [[1, 'One', 'One at a time; the tabs switch between them']];
        for (var i = 2; i <= panes.length && i <= 8; i++) choices.push([i, String(i), i + ' side by side']);
        choices.push(['all', 'All', 'Every open terminal, and new ones as you open them']);
        viewBar.appendChild(h('span', { class: 'muted small' }, 'Show'));
        var group = h('div', { class: 'seg' });
        choices.forEach(function (c) {
          group.appendChild(h('button', {
            class: 'seg-btn' + (current === c[0] ? ' on' : ''), type: 'button', title: c[2],
            'aria-pressed': current === c[0] ? 'true' : 'false',
            onclick: function () { layout = c[0]; arrange(); focusPane(active); }
          }, c[1]));
        });
        viewBar.appendChild(group);
      }

      function markActive() {
        renderTabs();
        panes.forEach(function (p) {
          p.el.classList.toggle('active', p === active && shown.length > 1);
        });
        keys.hidden = !(active && active.full && active.alive);
        showStatus();
      }

      function showStatus() {
        if (!active) { status.textContent = ''; return; }
        if (!active.id) { status.textContent = 'Opening a terminal on ' + label(active) + '…'; return; }
        status.textContent = active.alive
          ? 'Session ' + PC.shortId(active.id) + ' on ' + label(active) + (active.size.textContent ? ' · ' + active.size.textContent : '')
          : 'Closed: ' + label(active) + '. Its last screen stays here until you close the tab.';
      }

      function setActive(p) {
        if (active === p) return;
        active = p;
        markActive();
        save();
      }

      /* A tab was clicked: on screen already, or it takes the active section. */
      function showPane(p) {
        if (shown.indexOf(p) < 0) {
          var at = shown.indexOf(active);
          if (at >= 0 && shown.length >= sections()) shown[at] = p;
          else shown.push(p);
        }
        active = p;
        arrange();
        focusPane(p);
      }

      /* A section's picker: show another terminal there.  One already on screen
         changes places with it. */
      function place(p, key) {
        var q = panes.filter(function (x) { return x.key === key; })[0];
        var here = shown.indexOf(p);
        if (!q || q === p || here < 0) return;
        var there = shown.indexOf(q);
        if (there >= 0) shown[there] = p;
        shown[here] = q;
        active = q;
        arrange();
        focusPane(q);
      }

      function save() {
        if (gone) return;
        var ids = function (list) {
          return list.filter(function (p) { return p.id && p.alive; }).map(function (p) { return p.id; });
        };
        termSave(TERM_KEY, { sessions: ids(panes), view: ids(shown), active: active && active.id, layout: layout });
      }

      /* ---------------- opening and closing ---------------- */
      function open() {
        var id = devSel.value;
        if (!id) { PC.toast('Pick a device first', 'error'); return; }
        var wantFull = modeSel.value === 'full' && hasPty(id);
        var p = newPane(id);
        openBtn.disabled = true;
        opening++;
        early = opening > 1 ? early : {};
        showPane(p);
        var ready = wantFull ? loadXterm().then(function () {
          if (gone) throw new Error('left the page');
          setFull(p, true);
          var t = terminal(p);
          fitNow(p);
          return { pty: true, cols: t.term.cols, rows: t.term.rows };
        }).catch(function (e) {
          if (gone) throw e;
          PC.toast('Full terminal unavailable (' + (e && e.message) + '); using line mode', 'error');
          return {};
        }) : Promise.resolve({});
        ready.then(function (opts) {
          return PC.api.openTerminal(id, opts);
        }).then(function (res) {
          var sid = res && (res.session_id || res.id);
          if (!sid) throw new Error('coordinator did not return a session_id');
          opening--;
          openBtn.disabled = false;
          if (gone || panes.indexOf(p) < 0) return;
          p.id = sid;
          byId[sid] = p;
          setFull(p, res.pty && p.xt);
          if (!p.full) dropTerminal(p);
          p.sentSize = res.pty ? res.cols + 'x' + res.rows : '';
          goLive(p);
          if (!p.full) note(p, 'session ' + sid + ' opened');
          (early[sid] || []).forEach(onTerm);
          if (!opening) early = {};
          refresh();
          if (p.full) queueResize(p, false);  // the section may have changed size meanwhile
          if (p === active) focusPane(p);
        }).catch(function (e) {
          opening--;
          openBtn.disabled = false;
          if (gone) return;
          removePane(p);
          PC.err(e);
        });
      }

      /* Pick up a shell that is still open: replay what it printed (and, in line
         mode, the commands typed), then carry on live.  Also used after the
         WebSocket was down, to fill in what was missed. */
      function resume(p, opts) {
        opts = opts || {};
        p.attaching = true;
        p.pending = [];
        return PC.api.terminalScrollback(p.id).then(function (res) {
          return res.pty ? loadXterm().then(function () { return res; }) : res;
        }).then(function (res) {
          if (gone || panes.indexOf(p) < 0) return;
          p.attaching = false;
          setDevice(p, res.device_id);
          setFull(p, res.pty);
          if (res.pty) {
            var t = terminal(p);
            t.term.reset();
            // replay at the size the program drew for; it redraws for this page below
            p.sentSize = (res.cols || 80) + 'x' + (res.rows || 24);
            t.term.resize(res.cols || 80, res.rows || 24);
            t.term.write(res.output || '');
          } else {
            p.out.textContent = '';
            write(p, res.output || '');
          }
          p.lastPos = res.pos || 0;
          p.calmUntil = Date.now() + 2000;
          goLive(p);
          var waiting = p.pending;
          p.pending = [];
          waiting.forEach(onTerm);
          fitNow(p);
          if (res.pty) queueResize(p, true);
          refresh();
          if (opts.focus && p === active) focusPane(p);
        }).catch(function (e) {
          if (gone || panes.indexOf(p) < 0) return;
          p.attaching = false;
          if (e && e.status === 404) {
            if (opts.restoring) {
              removePane(p);
              PC.toast('A terminal you had open has ended (closed after 30 idle minutes, or its device left)');
            } else {
              finish(p, null);
            }
          } else if (opts.restoring) {
            removePane(p);
            PC.err(e);
          } else {
            PC.err(e);
          }
        });
      }

      /* Add a shell that is open on a device to this page. */
      function adopt(sid, deviceId) {
        if (byId[sid]) { showPane(byId[sid]); return; }
        var p = newPane(deviceId);
        p.id = sid;
        p.alive = true;
        byId[sid] = p;
        showPane(p);
        resume(p, { restoring: true, focus: true }).then(listOpenShells);
      }

      /* Shells still open on devices that this page isn't showing (another tab's,
         a closed tab's). */
      function listOpenShells() {
        PC.api.terminals().then(function (list) {
          if (gone) return;
          PC.clear(openShells);
          list.filter(function (s) { return !s.closed && !byId[s.session_id]; }).forEach(function (s) {
            openShells.appendChild(h('div', { class: 'card row gap' },
              h('span', {}, 'A ' + (s.pty ? 'terminal' : 'shell') + ' is still open on ', h('strong', {}, deviceName(s.device_id)),
                ' (opened ', PC.timeEl(s.created), ')'),
              h('button', { class: 'btn btn-sm btn-primary', onclick: function () { adopt(s.session_id, s.device_id); } }, 'Resume'),
              h('button', {
                class: 'btn btn-sm btn-danger', onclick: function () {
                  PC.api.closeTerminal(s.session_id).then(listOpenShells).catch(PC.err);
                }
              }, 'Close')));
          });
        }).catch(function () { /* the list is a convenience */ });
      }

      /* The × on a tab or section.  Still running: ask, it ends what runs there. */
      function closeTab(p) {
        if (!p.id) return;   // still opening
        if (!p.alive) { removePane(p); return; }
        PC.confirm('Close the terminal on ' + label(p) + '? Anything running in it stops.',
          { title: 'Close terminal', confirmLabel: 'Close' }).then(function (yes) {
            if (!yes || panes.indexOf(p) < 0) return;
            PC.api.closeTerminal(p.id).then(function () { removePane(p); }).catch(function (e) {
              if (e && e.status === 404) removePane(p);
              else PC.err(e);
            });
          });
      }

      function removePane(p) {
        var at = panes.indexOf(p);
        if (at < 0) return;
        clearTimeout(p.resizeTimer);
        p.alive = false;
        dropTerminal(p);
        if (p.id) delete byId[p.id];
        panes.splice(at, 1);
        if (p.el.parentNode) p.el.parentNode.removeChild(p.el);
        arrange();
        listOpenShells();
        focusPane(active);
      }

      /* The session ended by itself: its tab and last screen stay until dismissed. */
      function finish(p, code) {
        if (!p.alive) return;
        p.alive = false;
        p.attaching = false;
        note(p, 'session closed' + (code === null || code === undefined ? '' : ' (exit ' + code + ')'));
        p.input.disabled = true;
        p.input.placeholder = 'Session closed';
        if (p.xt) { p.xt.term.options.disableStdin = true; p.xt.term.options.cursorBlink = false; }
        refresh();
        listOpenShells();
      }

      function lineKey(p, e) {
        var input = p.input;
        if (e.key === 'Enter') {
          e.preventDefault();
          var line = input.value;
          if (!p.id || !p.alive) return;
          write(p, '$ ' + line + '\n');
          if (!PC.ws.termInput(p.id, line + '\n')) {
            write(p, '*** not connected - the WebSocket is down, input was not sent ***\n');
          }
          if (line.trim()) {
            history.push(line);
            if (history.length > 200) history.shift();
            termSave(TERM_HISTORY_KEY, history);
          }
          p.histPos = history.length;
          input.value = '';
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          if (!history.length) return;
          p.histPos = Math.max(0, p.histPos - 1);
          input.value = history[p.histPos] || '';
          setTimeout(function () { input.setSelectionRange(input.value.length, input.value.length); }, 0);
        } else if (e.key === 'ArrowDown') {
          e.preventDefault();
          if (!history.length) return;
          p.histPos = Math.min(history.length, p.histPos + 1);
          input.value = p.histPos >= history.length ? '' : history[p.histPos];
        }
      }

      var offs = [
        PC.bus.on('term', onTerm),
        PC.bus.on('term_closed', function (m) {
          var p = byId[m.session_id];
          if (p) finish(p, m.exit_code);
        }),
        PC.bus.on('conn', function (c) {
          if (c.state === 'closed') socketWasDown = true;
          else if (c.state === 'open' && socketWasDown) {
            socketWasDown = false;
            // output sent while the socket was down
            panes.forEach(function (p) { if (p.id && p.alive) resume(p); });
          }
        })
      ];

      /* Back on the page, or the tab reloaded: everything this tab had open. */
      var saved = termSaved();
      if (saved.layout === 'all' || saved.layout >= 1) layout = saved.layout;
      (saved.sessions || []).forEach(function (sid) {
        var p = newPane(null);
        p.id = sid;
        p.alive = true;
        byId[sid] = p;
      });
      shown = (saved.view || []).map(function (sid) { return byId[sid]; }).filter(Boolean);
      active = byId[saved.active] || null;
      arrange();
      panes.slice().forEach(function (p) { resume(p, { restoring: true, focus: true }); });

      // Leaving the page keeps the shells open; only stop listening.
      return function () {
        gone = true;
        offs.forEach(function (f) { f(); });
        window.removeEventListener('resize', fitShown);
        if (fitWatch) fitWatch.disconnect();
        themeWatch.disconnect();
        if (media && media.removeEventListener) media.removeEventListener('change', recolour);
        panes.forEach(function (p) { clearTimeout(p.resizeTimer); dropTerminal(p); });
      };
    }
  };

  /* =========================================================================
     Schedules
     ========================================================================= */
  var CRON_HELP = 'Standard 5 fields: min hour day-of-month month day-of-week. Supports * , - and */N.';

  function cronHelper(initial) {
    var text = h('input', { class: 'input mono', value: initial || '*/10 * * * *' });
    var mode = h('select', { class: 'input compact' },
      h('option', { value: 'every' }, 'every N minutes'),
      h('option', { value: 'hourly' }, 'hourly at minute'),
      h('option', { value: 'daily' }, 'daily at time'),
      h('option', { value: 'free' }, 'free text'));
    var nInput = h('input', { class: 'input compact', type: 'number', min: '1', max: '59', value: '10' });
    var timeInput = h('input', { class: 'input compact', type: 'time', value: '03:00' });
    var nWrap = h('span', { class: 'inline' }, nInput);
    var tWrap = h('span', { class: 'inline hidden-block' }, timeInput);

    function sync() {
      var m = mode.value;
      nWrap.classList.toggle('hidden-block', m !== 'every' && m !== 'hourly');
      nWrap.classList.toggle('open', m === 'every' || m === 'hourly');
      tWrap.classList.toggle('open', m === 'daily');
      if (m === 'every') text.value = '*/' + (parseInt(nInput.value, 10) || 10) + ' * * * *';
      else if (m === 'hourly') text.value = (parseInt(nInput.value, 10) || 0) % 60 + ' * * * *';
      else if (m === 'daily') {
        var parts = (timeInput.value || '03:00').split(':');
        text.value = (parseInt(parts[1], 10) || 0) + ' ' + (parseInt(parts[0], 10) || 0) + ' * * *';
      }
    }
    mode.addEventListener('change', sync);
    nInput.addEventListener('input', sync);
    timeInput.addEventListener('input', sync);
    mode.value = 'free';
    sync();

    var el = h('div', {},
      h('div', { class: 'row gap wrap' }, mode, nWrap, tWrap,
        h('button', { class: 'btn btn-sm', type: 'button', onclick: function () { mode.value = 'every'; nInput.value = '5'; sync(); } }, 'every 5 min'),
        h('button', { class: 'btn btn-sm', type: 'button', onclick: function () { mode.value = 'hourly'; nInput.value = '0'; sync(); } }, 'hourly'),
        h('button', { class: 'btn btn-sm', type: 'button', onclick: function () { mode.value = 'daily'; sync(); } }, 'daily 03:00')),
      text,
      h('div', { class: 'field-help' }, CRON_HELP));
    return { el: el, get: function () { return text.value.trim(); }, set: function (v) { text.value = v || ''; mode.value = 'free'; } };
  }

  function scheduleDialog(existing, onSaved) {
    var box = h('div', { class: 'dialog-body' }, h('p', { class: 'muted' }, 'Loading plugins…'));
    var m = PC.modal({ title: existing ? 'Edit schedule' : 'New schedule', body: box, actions: [] });
    Promise.all([PC.loadPlugins(), PC.loadDevices()]).then(function (r) {
      PC.clear(box);
      var nameInput = h('input', { class: 'input', value: existing ? (existing.name || '') : '' });
      var enabled = h('input', { type: 'checkbox' });
      enabled.checked = existing ? !!existing.enabled : true;
      var cron = cronHelper(existing ? existing.cron : '*/10 * * * *');
      if (existing && existing.cron) cron.set(existing.cron);
      var form = new PC.JobForm({ plugins: r[0], devices: r[1] });
      box.appendChild(PC.field('Name', nameInput));
      box.appendChild(PC.field('Cron', cron.el));
      box.appendChild(h('label', { class: 'check' }, enabled, h('span', {}, 'Enabled')));
      box.appendChild(h('h3', { class: 'form-section-title' }, 'Job to run'));
      box.appendChild(form.el);
      box.appendChild(h('div', { class: 'row gap wrap dialog-bar' },
        h('button', { class: 'btn btn-primary', onclick: save }, existing ? 'Save changes' : 'Create schedule'),
        h('button', { class: 'btn', onclick: function () { m.close(); } }, 'Cancel')));
      if (existing && existing.job_spec) form.setSpec(existing.job_spec);

      function save() {
        var spec;
        try { spec = form.getSpec(); } catch (e) { PC.err(e); return; }
        if (!nameInput.value.trim()) { PC.toast('Give the schedule a name', 'error'); return; }
        if (!cron.get()) { PC.toast('Cron expression is required', 'error'); return; }
        var body = { name: nameInput.value.trim(), cron: cron.get(), job_spec: spec, enabled: enabled.checked };
        var p = existing ? PC.api.patchSchedule(existing.id, body) : PC.api.createSchedule(body);
        p.then(function () { m.close(); PC.ok('Saved'); onSaved && onSaved(); }).catch(PC.err);
      }
    }).catch(function (e) {
      PC.clear(box);
      box.appendChild(h('p', { class: 'error-text' }, 'Could not load plugins: ' + (e.message || e)));
    });
  }

  views.schedules = {
    title: 'Schedules',
    render: function (root) {
      root.appendChild(page('Schedules', [
        h('button', { class: 'btn btn-primary', onclick: function () { scheduleDialog(null, load); } }, 'New schedule'),
        h('button', { class: 'btn', onclick: function () { load(); } }, 'Refresh')]));
      var t = table(['Name', 'Cron', 'Job', 'Enabled', 'Last run', 'Next run', '']);
      root.appendChild(t.el);

      function load() { PC.api.schedules().then(draw).catch(PC.err); }
      function draw(list) {
        PC.clear(t.body);
        if (!list || !list.length) { t.body.appendChild(emptyRow(7, 'No schedules.')); return; }
        list.forEach(function (s) {
          var cb = h('input', { type: 'checkbox' });
          cb.checked = !!s.enabled;
          cb.addEventListener('change', function () {
            PC.api.patchSchedule(s.id, { enabled: cb.checked })
              .then(function () { PC.ok(cb.checked ? 'Enabled' : 'Disabled'); })
              .catch(function (e) { PC.err(e); cb.checked = !cb.checked; });
          });
          t.body.appendChild(h('tr', {},
            h('td', {}, s.name || PC.shortId(s.id)),
            h('td', {}, h('code', {}, s.cron || '-')),
            h('td', {}, h('code', {}, (s.job_spec && s.job_spec.type) || '-')),
            h('td', {}, h('label', { class: 'check' }, cb, h('span', { class: 'sr-only' }, 'Enabled'))),
            h('td', {}, s.last_run ? PC.timeEl(s.last_run) : h('span', { class: 'muted' }, 'never')),
            h('td', {}, s.next_run ? PC.timeEl(s.next_run) : h('span', { class: 'muted' }, '-')),
            h('td', { class: 'right' }, h('div', { class: 'row gap end' },
              h('button', { class: 'btn btn-sm', onclick: function () { PC.api.runSchedule(s.id).then(function () { PC.ok('Fired'); load(); }).catch(PC.err); } }, 'Run now'),
              h('button', { class: 'btn btn-sm', onclick: function () { scheduleDialog(s, load); } }, 'Edit'),
              h('button', {
                class: 'btn btn-sm btn-danger', onclick: function () {
                  PC.confirm('Delete schedule "' + (s.name || s.id) + '"?', { title: 'Delete schedule', confirmLabel: 'Delete' })
                    .then(function (yes) { if (yes) PC.api.deleteSchedule(s.id).then(function () { PC.ok('Deleted'); load(); }).catch(PC.err); });
                }
              }, 'Delete')))));
        });
      }
      load();
      return function () { };
    }
  };

  /* =========================================================================
     Events
     ========================================================================= */
  views.events = {
    title: 'Events',
    render: function (root) {
      var kind = h('select', { class: 'input compact', onchange: load },
        h('option', { value: '' }, 'all kinds'),
        PC.EVENT_KINDS.map(function (k) { return h('option', { value: k }, k); }));
      root.appendChild(page('Events', [kind, h('button', { class: 'btn', onclick: load }, 'Refresh')]));
      var list = h('ul', { class: 'event-list card' });
      root.appendChild(list);

      function load() {
        PC.api.events(200, kind.value || null).then(draw).catch(PC.err);
      }
      function draw(items) {
        PC.clear(list);
        if (!items || !items.length) { list.appendChild(h('li', { class: 'empty' }, 'No events.')); return; }
        items.forEach(function (e) { list.appendChild(PC.eventItem(e)); });
      }
      load();
      var off = PC.bus.on('event', function (e) {
        if (kind.value && e.kind !== kind.value) return;
        var first = list.firstChild;
        var node = PC.eventItem(e);
        if (first) list.insertBefore(node, first); else list.appendChild(node);
      });
      return function () { off(); };
    }
  };

  /* =========================================================================
     Settings
     ========================================================================= */
  views.settings = {
    title: 'Settings',
    render: function (root) {
      root.appendChild(page('Settings'));
      var box = h('div', {}, h('p', { class: 'muted' }, 'Loading…'));
      root.appendChild(box);

      PC.api.settings().then(draw).catch(function (e) {
        PC.clear(box);
        box.appendChild(h('p', { class: 'error-text' }, 'Could not load settings: ' + (e.message || e)));
      });

      function numInput(v, attrs) {
        return h('input', Object.assign({ class: 'input', type: 'number', value: v === undefined || v === null ? '' : String(v) }, attrs || {}));
      }

      function draw(s) {
        s = s || {};
        var sch = s.scheduler || {}, sto = s.storage || {}, nf = s.notifications || {};
        PC.clear(box);

        var minBattery = numInput(sch.min_battery, { min: '0', max: '100', step: '1' });
        var maxTemp = numInput(sch.max_temp, { step: 'any' });
        var resumeTemp = numInput(sch.resume_temp, { step: 'any' });
        var defTimeout = numInput(sch.default_timeout, { min: '1', step: '1' });
        var defAttempts = numInput(sch.default_max_attempts, { min: '1', step: '1' });
        var schCard = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'Scheduler'),
          h('div', { class: 'grid-2' },
            PC.field('min_battery (%)', minBattery),
            PC.field('max_temp (°C)', maxTemp),
            PC.field('resume_temp (°C)', resumeTemp),
            PC.field('default_timeout (s)', defTimeout),
            PC.field('default_max_attempts', defAttempts)),
          h('button', {
            class: 'btn btn-primary', onclick: function () {
              save({
                scheduler: {
                  min_battery: intOr(minBattery), max_temp: floatOr(maxTemp), resume_temp: floatOr(resumeTemp),
                  default_timeout: intOr(defTimeout), default_max_attempts: intOr(defAttempts)
                }
              });
            }
          }, 'Save scheduler'));

        var chunkMb = numInput(sto.chunk_mb, { min: '1', step: '1' });
        var replication = numInput(sto.replication, { min: '1', step: '1' });
        var cacheMb = numInput(sto.cache_mb, { min: '0', step: '1' });
        var includePc = h('input', { type: 'checkbox' });
        includePc.checked = !!sto.include_pc;
        var scheme = h('select', { class: 'input' },
          h('option', { value: 'auto' }, 'automatic: parity pieces from 3 phones, else copies'),
          h('option', { value: 'copies' }, 'always full copies'));
        scheme.value = sto.scheme === 'copies' ? 'copies' : 'auto';
        var scrubHours = numInput(sto.scrub_hours, { min: '0', step: '1' });
        var stoCard = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'Storage'),
          h('div', { class: 'grid-2' },
            PC.field('chunk_mb', chunkMb),
            PC.field('Protection', scheme, 'parity pieces: safe against one lost phone for ~1.3x the space'),
            PC.field('copies (when copying)', replication),
            PC.field('Re-check every piece every (h)', scrubHours, 'catches silent damage; 0 = never'),
            PC.field('cache_mb', cacheMb),
            h('label', { class: 'check' }, includePc, h('span', {}, 'include the PC in the storage pool'))),
          h('button', {
            class: 'btn btn-primary', onclick: function () {
              save({
                storage: {
                  chunk_mb: intOr(chunkMb), replication: intOr(replication), scheme: scheme.value,
                  scrub_hours: floatOr(scrubHours), cache_mb: intOr(cacheMb), include_pc: includePc.checked
                }
              });
            }
          }, 'Save storage'));

        var desktop = h('input', { type: 'checkbox' }); desktop.checked = !!nf.desktop;
        var phone = h('input', { type: 'checkbox' }); phone.checked = !!nf.phone;
        var webhook = h('input', { class: 'input', type: 'url', placeholder: 'https://…', value: nf.webhook_url || '' });
        var onBoxes = {};
        var onKinds = ['job_done', 'job_failed', 'device_offline', 'device_joined', 'device_online', 'device_removed', 'blob_degraded', 'error'];
        var onWrap = h('div', { class: 'checks' });
        onKinds.forEach(function (k) {
          var cb = h('input', { type: 'checkbox' });
          cb.checked = Array.isArray(nf.on) && nf.on.indexOf(k) >= 0;
          onBoxes[k] = cb;
          onWrap.appendChild(h('label', { class: 'check' }, cb, h('span', {}, k)));
        });
        var nfCard = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'Notifications'),
          h('label', { class: 'check' }, desktop, h('span', {}, 'Desktop toast on this PC')),
          h('label', { class: 'check' }, phone, h('span', {}, 'Push to phones with termux_api')),
          PC.field('Webhook URL', webhook, 'POSTed {kind, message, data, ts} with a 5 s timeout'),
          h('div', { class: 'field-label' }, 'Notify on'), onWrap,
          h('button', {
            class: 'btn btn-primary', onclick: function () {
              var on = Object.keys(onBoxes).filter(function (k) { return onBoxes[k].checked; });
              save({
                notifications: {
                  desktop: desktop.checked, phone: phone.checked,
                  webhook_url: webhook.value.trim() || null, on: on
                }
              });
            }
          }, 'Save notifications'));

        var pin1 = h('input', { class: 'input', type: 'password', inputmode: 'numeric', autocomplete: 'new-password', maxlength: '6', placeholder: '6 digits' });
        var pin2 = h('input', { class: 'input', type: 'password', inputmode: 'numeric', autocomplete: 'new-password', maxlength: '6', placeholder: 'repeat' });
        var pinCard = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'Dashboard PIN'),
          h('p', { class: 'muted' }, 'Changing the PIN signs out other dashboards. Device tokens keep working.'),
          h('div', { class: 'grid-2' }, PC.field('New PIN', pin1), PC.field('Confirm', pin2)),
          h('button', {
            class: 'btn btn-danger', onclick: function () {
              var p = pin1.value.trim();
              if (!/^\d{6}$/.test(p)) { PC.toast('The PIN must be 6 digits', 'error'); return; }
              if (p !== pin2.value.trim()) { PC.toast('The two PINs do not match', 'error'); return; }
              PC.confirm('Change the cluster PIN to ' + p + '?', { title: 'Change PIN', confirmLabel: 'Change PIN' }).then(function (yes) {
                if (!yes) return;
                PC.api.saveSettings({ pin: p }).then(function () {
                  PC.auth.set(p);
                  pin1.value = ''; pin2.value = '';
                  PC.ok('PIN changed');
                  PC.api.login(p).catch(function () { });
                  PC.ws.disconnect(); PC.ws.connect();
                }).catch(PC.err);
              });
            }
          }, 'Change PIN'));

        // automatic behaviour (SPEC 6.4c/d, 9.7)
        var llm = s.llm || {}, svc = s.services || {};
        var idleMin = numInput(llm.idle_minutes, { min: '0', step: '1' });
        var loadWait = numInput(llm.load_wait, { min: '10', step: '10' });
        var reqFirst = h('input', { type: 'checkbox' }); reqFirst.checked = sch.requests_first !== false;
        var holdSec = numInput(sch.hold_seconds, { min: '1', step: '1' });
        var autoscale = h('input', { type: 'checkbox' }); autoscale.checked = svc.autoscale !== false;
        var maxCopies = numInput(svc.max_copies, { min: '1', step: '1' });
        var upAfter = numInput(svc.scale_up_after, { min: '0', step: '1' });
        var downMin = numInput(svc.scale_down_minutes, { min: '0', step: '1' });
        // never compute twice (SPEC 9.8), catching wrong answers (9.9), phone-to-phone (8.3)
        var cch = s.cache || {}, ver = s.verify || {}, trf = s.transfers || {};
        var reuse = h('input', { type: 'checkbox' }); reuse.checked = cch.enabled !== false;
        var cacheMax = numInput(cch.max_mb, { min: '0', step: '16' });
        var checkPct = numInput(ver.share === undefined ? '' : Math.round(ver.share * 1000) / 10, { min: '0', max: '100', step: '1' });
        var recheckMin = numInput(ver.recheck_minutes, { min: '0', step: '5' });
        var strikes = numInput(ver.strikes, { min: '1', step: '1' });
        var peer = h('input', { type: 'checkbox' }); peer.checked = trf.peer !== false;
        var peerMin = numInput(trf.min_mb, { min: '0', step: '1' });
        // only the final result stays (SPEC 9.10)
        var keepDays = numInput((s.cleanup || {}).keep_days, { min: '0', step: '1' });
        var autoCard = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'Automatic'),
          h('div', { class: 'grid-2' },
            PC.field('Unload unused models after (min)', idleMin, '0 keeps models loaded; a message loads them again'),
            PC.field('Wait for a model to load (s)', loadWait),
            h('label', { class: 'check' }, reqFirst, h('span', {}, 'Requests first: tasks on a device pause while it answers')),
            PC.field('Longest pause per request (s)', holdSec),
            h('label', { class: 'check' }, autoscale, h('span', {}, 'Extra copies of busy services')),
            PC.field('Most copies', maxCopies),
            PC.field('Add a copy after a caller waited (s)', upAfter),
            PC.field('Remove an extra copy after quiet (min)', downMin),
            h('label', { class: 'check' }, reuse, h('span', {}, 'Reuse results of identical work (maps, numbers, media, WebAssembly)')),
            PC.field('Keep at most (MB of results)', cacheMax),
            PC.field('Double-check this share of tasks (%)', checkPct, 'on a second device, to catch wrong answers; 0 = off'),
            PC.field('Redo a wrong device\'s work of the last (min)', recheckMin),
            PC.field('Wrong answers in a day before no more jobs', strikes),
            h('label', { class: 'check' }, peer, h('span', {}, 'Phones pass big inputs to each other')),
            PC.field('Only for inputs from (MB)', peerMin),
            PC.field('Clean up finished jobs after (days)', keepDays,
              'then only the job and its final result stay: its tasks, logs and files go, and older events; 0 = never')),
          h('div', { class: 'row gap wrap' },
            h('button', {
              class: 'btn btn-primary', onclick: function () {
                save({
                  llm: { idle_minutes: floatOr(idleMin), load_wait: floatOr(loadWait) },
                  scheduler: { requests_first: reqFirst.checked, hold_seconds: floatOr(holdSec) },
                  services: { autoscale: autoscale.checked, max_copies: intOr(maxCopies),
                              scale_up_after: floatOr(upAfter), scale_down_minutes: floatOr(downMin) },
                  cache: { enabled: reuse.checked, max_mb: intOr(cacheMax) },
                  verify: { share: checkPct.value === '' ? null : parseFloat(checkPct.value) / 100,
                            recheck_minutes: floatOr(recheckMin), strikes: intOr(strikes) },
                  transfers: { peer: peer.checked, min_mb: floatOr(peerMin) },
                  cleanup: { keep_days: floatOr(keepDays) }
                });
              }
            }, 'Save'),
            h('button', {
              class: 'btn', title: 'Forget every stored result: the next identical job computes again',
              onclick: function () {
                PC.api.clearCache().then(function (r) { PC.ok('Removed ' + (r.removed || 0) + ' stored result(s)'); }).catch(PC.err);
              }
            }, 'Forget stored results')));

        box.appendChild(h('div', { class: 'grid-2 stretch' }, schCard, stoCard));
        box.appendChild(h('div', { class: 'grid-2 stretch' }, autoCard, nfCard));
        box.appendChild(h('div', { class: 'grid-2 stretch' }, pinCard));
      }

      function intOr(inp) { return inp.value === '' ? null : parseInt(inp.value, 10); }
      function floatOr(inp) { return inp.value === '' ? null : parseFloat(inp.value); }

      function save(patch) {
        // strip nulls so the coordinator keeps its current value
        Object.keys(patch).forEach(function (sec) {
          Object.keys(patch[sec]).forEach(function (k) {
            if (patch[sec][k] === null && k !== 'webhook_url') delete patch[sec][k];
          });
        });
        PC.api.saveSettings(patch).then(function () { PC.ok('Settings saved'); }).catch(PC.err);
      }
      return function () { };
    }
  };

  /* =========================================================================
     Join
     ========================================================================= */
  views.join = {
    title: 'Join',
    render: function (root) {
      root.appendChild(page('Add a device', [h('button', { class: 'btn', onclick: function () { load(); } }, 'New code')]));
      var box = h('div', {}, h('p', { class: 'muted' }, 'Loading…'));
      root.appendChild(box);
      var timer = null;

      function copyRow(label, value, help) {
        if (!value) return null;
        return h('div', { class: 'copy-row' },
          h('div', { class: 'field-label' }, label),
          h('div', { class: 'row gap' },
            h('code', { class: 'copy-code' }, String(value)),
            h('button', { class: 'btn btn-sm', onclick: function () { PC.copy(String(value)); } }, 'Copy')),
          help ? h('div', { class: 'field-help' }, help) : null);
      }

      function load() {
        if (timer) { clearInterval(timer); timer = null; }
        PC.api.join().then(function (j) {
          PC.clear(box);
          function qrHolder(svg, wide) {
            var holder = h('div', { class: 'qr' + (wide ? ' qr-wide' : '') });
            // the SVG comes from our own coordinator; it is inline markup.
            holder.innerHTML = String(svg || '');
            return holder;
          }
          var termuxCard = h('div', { class: 'card qr-card' },
            h('h2', { class: 'card-title' }, 'Termux phone (Redmi 5A, Note 10)'),
            qrHolder(j.install_qr_svg, true),
            h('p', { class: 'muted small' },
              'Scan with the phone\'s camera or QR scanner and tap Copy, then long-press in Termux and Paste. ',
              'Scanning only copies the text; nothing runs until you paste it.'));
          var browserCard = h('div', { class: 'card qr-card' },
            h('h2', { class: 'card-title' }, 'Browser phone (J2, any phone)'),
            qrHolder(j.qr_svg, false),
            h('p', { class: 'muted small' },
              'Opens the browser worker. The browser warns once that the connection is "not private": ',
              'the cluster uses its own certificate. Continue to the page.'));
          var qrCard = h('div', {}, termuxCard, browserCard);

          var left = h('div', { class: 'muted small' });
          var codeCard = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'One-time join code'),
            h('div', { class: 'pin-big' }, String(j.code || '----')), left,
            h('p', { class: 'muted small' }, 'It lets one device join, once: either QR uses it. ',
              'Press New code for the next phone. The dashboard PIN never leaves this PC.'));
          function tick() {
            var s = Math.max(0, Math.round((j.expires || 0) - Date.now() / 1000));
            left.textContent = s > 0 ? 'works for ' + Math.floor(s / 60) + ' min ' + (s % 60) + ' s more'
              : 'expired: press New code';
            if (s <= 0 && timer) { clearInterval(timer); timer = null; }
          }
          tick();
          timer = setInterval(tick, 1000);

          var cmdCard = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'Join commands'),
            copyRow('Termux / Linux / Mac one-liner', j.install_cmd, 'Termux: install it, then paste this. Linux or Mac: paste into a terminal (needs Python 3.8+). It checks it is really talking to this PC. No root needed.'),
            copyRow('Windows PC or laptop', j.windows_cmd, 'Paste into PowerShell or Command Prompt on it. Everything goes into one folder, phonecluster in the user folder: no admin rights, nothing installed. Later, start-worker.cmd there starts it again.'),
            copyRow('Browser worker URL', j.browser_url, 'Open in Chrome on any phone and keep the tab awake.'),
            copyRow('By hand', j.manual_cmd, 'If the phone already has worker.py.'),
            copyRow('Certificate fingerprint', j.ca_fingerprint, 'What the phone checks; compare it if you like.'));

          box.appendChild(h('div', { class: 'grid-2 stretch' }, qrCard, h('div', {}, codeCard, cmdCard)));
        }).catch(function (e) {
          PC.clear(box);
          box.appendChild(h('p', { class: 'error-text' }, 'Could not make a join code: ' + (e.message || e)));
        });
      }

      load();
      return function () { if (timer) { clearInterval(timer); } };
    }
  };
})();
