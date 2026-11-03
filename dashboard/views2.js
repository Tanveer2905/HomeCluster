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
      root.appendChild(page('Files', [h('button', { class: 'btn', onclick: function () { load(); } }, 'Refresh')]));
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
              h('button', { class: 'btn btn-sm', onclick: function () { PC.api.download('/files/' + encodeURIComponent(f.id) + '/download', f.name); } }, 'Download'),
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
              h('button', { class: 'btn btn-sm', onclick: function () { PC.api.download('/storage/' + encodeURIComponent(b.id), b.name); } }, 'Download'),
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
  /* A shell outlives this page: leaving Terminal (or reloading the tab) keeps it
     open on the device, and coming back picks it up again with its scrollback
     from the coordinator.  The session and the command history are remembered
     per browser tab (sessionStorage, else memory); shells this tab doesn't know
     about (another tab's, a closed tab's) are listed with Resume / Close.  The
     coordinator still closes a shell after 30 idle minutes. */
  var TERM_KEY = 'pc_terminal', TERM_HISTORY_KEY = 'pc_terminal_history';
  var termMemory = {};

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

  views.terminal = {
    title: 'Terminal',
    render: function (root) {
      var sessionId = null, closed = false, lastPos = 0;
      var opening = false, early = {};
      var attaching = null, pending = [];
      var devices = {}, wantDevice = null;
      var history = termLoad(TERM_HISTORY_KEY, []), histPos = history.length;

      var devSel = h('select', { class: 'input compact' });
      var openBtn = h('button', { class: 'btn btn-primary', onclick: open }, 'Open session');
      var closeBtn = h('button', { class: 'btn btn-danger', onclick: closeSession, disabled: true }, 'Close');
      root.appendChild(page('Terminal', [devSel, openBtn, closeBtn]));
      root.appendChild(h('p', { class: 'muted' }, 'Line-oriented shell over the worker long-poll. Interactive full-screen programs are out of scope. ' +
        'The shell stays open when you leave this page; it closes with Close, or after 30 minutes without use.'));
      var openShells = h('div', { class: 'terminal-open-shells' });
      root.appendChild(openShells);

      var out = h('pre', { class: 'terminal-out', tabindex: '0' });
      var input = h('input', {
        class: 'input mono terminal-in', placeholder: 'Open a session first', disabled: true,
        autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false'
      });
      var status = h('div', { class: 'muted small' }, 'No session.');
      root.appendChild(h('div', { class: 'card terminal-card' }, out,
        h('div', { class: 'row gap terminal-row' }, h('span', { class: 'prompt' }, '$'), input), status));

      PC.loadDevices().then(function (list) {
        PC.clear(devSel);
        list.forEach(function (d) { devices[d.id] = d; });
        var usable = list.filter(function (d) { return d.platform !== 'browser'; });
        if (!usable.length) devSel.appendChild(h('option', { value: '' }, 'no devices with a shell'));
        usable.forEach(function (d) {
          devSel.appendChild(h('option', { value: d.id },
            (d.name || PC.shortId(d.id)) + ' (' + (d.platform || '?') + ', ' + (d.status || '?') + ')'));
        });
        if (wantDevice) devSel.value = wantDevice;
        if (sessionId) status.textContent = describe(sessionId, wantDevice);
        if (!sessionId && !attaching) listOpenShells();
      }).catch(PC.err);

      function deviceName(id) {
        var d = devices[id];
        return d ? (d.name || PC.shortId(d.id)) : PC.shortId(id);
      }

      function describe(sid, deviceId) {
        return 'Session ' + PC.shortId(sid) + ' on ' + deviceName(deviceId);
      }

      function write(txt) {
        var atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
        out.appendChild(document.createTextNode(txt));
        if (atBottom) out.scrollTop = out.scrollHeight;
      }

      /* Live output for our session.  `pos` counts every character the session
         ever held, so output already in the replayed scrollback is skipped. */
      function onTerm(m) {
        if (sessionId && m.session_id === sessionId) {
          if (m.pos && m.pos <= lastPos) return;
          write(m.data || '');
          if (m.pos) lastPos = m.pos;
          return;
        }
        if (attaching && m.session_id === attaching) { pending.push(m); return; }
        // The shell banner can arrive over the WebSocket before the open
        // response does; keep it for our session.
        if (opening && !sessionId && m.session_id) {
          (early[m.session_id] = early[m.session_id] || []).push(m);
        }
      }

      function attached(sid, deviceId) {
        sessionId = sid;
        closed = false;
        wantDevice = deviceId;
        if (deviceId) devSel.value = deviceId;
        status.textContent = describe(sid, deviceId);
        input.disabled = false;
        input.placeholder = 'type a command and press Enter';
        closeBtn.disabled = false;
        openBtn.disabled = true;
        PC.clear(openShells);
        termSave(TERM_KEY, { session_id: sid, device_id: deviceId });
        input.focus();
      }

      function open() {
        var id = devSel.value;
        if (!id) { PC.toast('Pick a device first', 'error'); return; }
        openBtn.disabled = true;
        opening = true;
        early = {};
        PC.api.openTerminal(id).then(function (res) {
          var sid = res && (res.session_id || res.id);
          if (!sid) throw new Error('coordinator did not return a session_id');
          lastPos = 0;
          attached(sid, id);
          write('*** session ' + sid + ' opened ***\n');
          (early[sid] || []).forEach(onTerm);
          opening = false;
          early = {};
        }).catch(function (e) { PC.err(e); openBtn.disabled = false; opening = false; early = {}; });
      }

      /* Pick up a shell that is still open: replay what it printed (and the
         commands typed), then carry on live.  Also used after the WebSocket
         was down, to fill in what was missed. */
      function resume(sid) {
        attaching = sid;
        pending = [];
        sessionId = null;
        closed = false;
        openBtn.disabled = true;
        PC.api.terminalScrollback(sid).then(function (res) {
          if (attaching !== sid) return;
          attaching = null;
          out.textContent = '';
          write(res.output || '');
          lastPos = res.pos || 0;
          attached(sid, res.device_id);
          var waiting = pending;
          pending = [];
          waiting.forEach(onTerm);
        }).catch(function (e) {
          if (attaching !== sid) return;
          attaching = null;
          if (e && e.status === 404) {
            if (termLoad(TERM_KEY, null)) {
              write('*** the shell you had open has ended (closed after 30 idle minutes, or its device left) ***\n');
            }
            termSave(TERM_KEY, null);
          } else {
            PC.err(e);
          }
          idle();
          listOpenShells();
        });
      }

      /* Shells still open on devices that this tab isn't showing. */
      function listOpenShells() {
        PC.api.terminals().then(function (list) {
          PC.clear(openShells);
          if (sessionId || attaching) return;
          list.filter(function (s) { return !s.closed; }).forEach(function (s) {
            openShells.appendChild(h('div', { class: 'card row gap' },
              h('span', {}, 'A shell is still open on ', h('strong', {}, deviceName(s.device_id)),
                ' (opened ', PC.timeEl(s.created), ')'),
              h('button', { class: 'btn btn-sm btn-primary', onclick: function () { resume(s.session_id); } }, 'Resume'),
              h('button', {
                class: 'btn btn-sm btn-danger', onclick: function () {
                  PC.api.closeTerminal(s.session_id).then(listOpenShells).catch(PC.err);
                }
              }, 'Close')));
          });
        }).catch(function () { /* the list is a convenience */ });
      }

      function closeSession() {
        if (!sessionId) return;
        var sid = sessionId;
        PC.api.closeTerminal(sid).then(function () { finish(null); }).catch(function (e) {
          if (e && e.status === 404) finish(null);
          else PC.err(e);
        });
      }

      function idle() {
        status.textContent = 'No session.';
        input.disabled = true;
        input.placeholder = 'Open a session first';
        closeBtn.disabled = true;
        openBtn.disabled = false;
        sessionId = null;
      }

      function finish(code) {
        if (closed) return;
        closed = true;
        write('\n*** session closed' + (code === null || code === undefined ? '' : ' (exit ' + code + ')') + ' ***\n');
        idle();
        status.textContent = 'Session closed.';
        termSave(TERM_KEY, null);
        listOpenShells();
      }

      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          var line = input.value;
          if (!sessionId) return;
          write('$ ' + line + '\n');
          if (!PC.ws.termInput(sessionId, line + '\n')) {
            write('*** not connected - the WebSocket is down, input was not sent ***\n');
          }
          if (line.trim()) {
            history.push(line);
            if (history.length > 200) history.shift();
            termSave(TERM_HISTORY_KEY, history);
          }
          histPos = history.length;
          input.value = '';
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          if (!history.length) return;
          histPos = Math.max(0, histPos - 1);
          input.value = history[histPos] || '';
          setTimeout(function () { input.setSelectionRange(input.value.length, input.value.length); }, 0);
        } else if (e.key === 'ArrowDown') {
          e.preventDefault();
          if (!history.length) return;
          histPos = Math.min(history.length, histPos + 1);
          input.value = histPos >= history.length ? '' : history[histPos];
        }
      });

      var socketWasDown = false;
      var offs = [
        PC.bus.on('term', onTerm),
        PC.bus.on('term_closed', function (m) {
          if (!m.session_id || (m.session_id !== sessionId && m.session_id !== attaching)) return;
          attaching = null;
          finish(m.exit_code);
        }),
        PC.bus.on('conn', function (c) {
          if (c.state === 'closed') socketWasDown = true;
          else if (c.state === 'open' && socketWasDown) {
            socketWasDown = false;
            if (sessionId) resume(sessionId);  // output sent while the socket was down
          }
        })
      ];

      var saved = termLoad(TERM_KEY, null);
      if (saved && saved.session_id) {
        wantDevice = saved.device_id || null;
        resume(saved.session_id);
      }

      // Leaving the page keeps the shell open; only stop listening.
      return function () {
        offs.forEach(function (f) { f(); });
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
            PC.field('Only for inputs from (MB)', peerMin)),
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
                  transfers: { peer: peer.checked, min_mb: floatOr(peerMin) }
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
