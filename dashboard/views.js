/* PhoneCluster dashboard - Overview, Devices, Jobs, Job detail (SPEC §14). */
(function () {
  'use strict';
  var PC = (window.PC = window.PC || {});
  var views = PC.views = PC.views || {};
  var h = PC.h;

  function page(title, actions) {
    var head = h('div', { class: 'page-head' }, h('h1', { class: 'page-title' }, title));
    if (actions) head.appendChild(h('div', { class: 'page-actions' }, actions));
    return head;
  }
  PC.page = page;

  function tile(label, value, sub) {
    return h('div', { class: 'tile' },
      h('div', { class: 'tile-value' }, String(value)),
      h('div', { class: 'tile-label' }, label),
      sub ? h('div', { class: 'tile-sub' }, sub) : null);
  }

  function emptyRow(cols, text) {
    return h('tr', {}, h('td', { colspan: String(cols), class: 'empty' }, text));
  }

  function table(cols) {
    var t = h('table', { class: 'table' });
    var thead = h('thead', {}, h('tr', {}, cols.map(function (c) { return h('th', {}, c); })));
    var tbody = h('tbody', {});
    t.appendChild(thead); t.appendChild(tbody);
    return { el: h('div', { class: 'table-wrap' }, t), body: tbody };
  }
  PC.table = table;

  /* =========================================================================
     Overview
     ========================================================================= */
  views.overview = {
    title: 'Overview',
    render: function (root) {
      root.appendChild(page('Overview'));
      var tiles = h('div', { class: 'tiles' });
      var lbBox = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'Leaderboard'));
      var lbTable = table(['#', 'Device', 'Score', 'Tasks done']);
      lbBox.appendChild(lbTable.el);
      var evBox = h('div', { class: 'card' },
        h('h2', { class: 'card-title' }, 'Recent events'));
      var evList = h('ul', { class: 'event-list' });
      evBox.appendChild(evList);
      root.appendChild(tiles);
      root.appendChild(h('div', { class: 'grid-2 stretch' }, lbBox, evBox));

      function drawStats(s) {
        PC.clear(tiles);
        if (!s) { tiles.appendChild(h('div', { class: 'muted pad' }, 'Loading stats…')); return; }
        var d = s.devices || {}, j = s.jobs || {};
        tiles.appendChild(tile('Devices online', (d.online === undefined ? '-' : d.online), 'of ' + (d.total === undefined ? '-' : d.total) + ' known'));
        tiles.appendChild(tile('Cores', d.cores === undefined ? '-' : d.cores));
        tiles.appendChild(tile('Free slots', d.slots_free === undefined ? '-' : d.slots_free));
        tiles.appendChild(tile('Jobs running', j.running === undefined ? '-' : j.running, (j.pending === undefined ? '' : j.pending + ' pending')));
        tiles.appendChild(tile('Done (24h)', j.done_24h === undefined ? '-' : j.done_24h));
        tiles.appendChild(tile('Failed (24h)', j.failed_24h === undefined ? '-' : j.failed_24h));
        tiles.appendChild(tile('Tasks / min', s.tasks_per_min === undefined ? '-' : PC.fmtNum(s.tasks_per_min, 1)));

        PC.clear(lbTable.body);
        var lb = s.leaderboard || [];
        if (!lb.length) lbTable.body.appendChild(emptyRow(4, 'No benchmark results yet - run a benchmark from the Devices view.'));
        lb.forEach(function (row, i) {
          lbTable.body.appendChild(h('tr', {},
            h('td', {}, String(i + 1)),
            h('td', {}, h('a', { href: '#/devices', class: 'link' }, row.name || PC.shortId(row.device_id))),
            h('td', {}, row.bench_score === null || row.bench_score === undefined ? '-' : PC.fmtNum(row.bench_score, 2)),
            h('td', {}, row.tasks_done === undefined ? '-' : String(row.tasks_done))));
        });
      }

      function drawEvents(list) {
        PC.clear(evList);
        var items = (list || []).slice(0, 15);
        if (!items.length) { evList.appendChild(h('li', { class: 'empty' }, 'No events yet.')); return; }
        items.forEach(function (e) { evList.appendChild(PC.eventItem(e)); });
      }

      drawStats(PC.store.stats);
      drawEvents(PC.store.events);
      var offs = [
        PC.bus.on('stats', drawStats),
        PC.bus.on('events', drawEvents)
      ];
      PC.api.stats().then(function (s) { PC.store.stats = s; drawStats(s); }).catch(PC.err);
      if (!PC.store.events.length) {
        PC.api.events(20).then(function (e) { PC.store.events = e; drawEvents(e); }).catch(function () { });
      }
      return function () { offs.forEach(function (f) { f(); }); };
    }
  };

  PC.eventItem = function (e) {
    var li = h('li', { class: 'event-item' },
      h('span', { class: 'event-kind kind-' + String(e.kind || '').replace(/[^a-z_]/g, '') }, e.kind || 'event'),
      h('span', { class: 'event-msg' }, e.message || ''),
      PC.timeEl(e.ts));
    if (e.job_id) {
      li.appendChild(h('a', { class: 'link tiny', href: '#/jobs/' + encodeURIComponent(e.job_id) }, 'job ' + PC.shortId(e.job_id)));
    }
    return li;
  };

  /* =========================================================================
     Devices
     ========================================================================= */
  views.devices = {
    title: 'Devices',
    render: function (root) {
      root.appendChild(page('Devices', [
        h('a', { class: 'btn btn-primary', href: '#/join' }, 'Add a device'),
        h('button', { class: 'btn', onclick: function () { load(true); } }, 'Refresh')
      ]));
      var grid = h('div', { class: 'card-grid' });
      root.appendChild(grid);

      function load(force) {
        PC.loadDevices(force).then(draw).catch(PC.err);
      }

      function draw(list) {
        PC.clear(grid);
        list = list || [];
        if (!list.length) {
          grid.appendChild(h('div', { class: 'card empty-card' },
            h('p', {}, 'No devices yet.'),
            h('p', { class: 'muted' }, 'Open the Join view and scan the QR code on a phone, or run the Termux one-liner.'),
            h('a', { class: 'btn btn-primary', href: '#/join' }, 'Show join instructions')));
          return;
        }
        list.slice().sort(function (a, b) {
          var s = (b.status === 'online' ? 1 : 0) - (a.status === 'online' ? 1 : 0);
          if (s) return s;
          return (b.bench_score || 0) - (a.bench_score || 0);
        }).forEach(function (d) { grid.appendChild(deviceCard(d)); });
      }

      draw(PC.store.devices);
      load(true);
      var off = PC.bus.on('devices', draw);
      return function () { off(); };
    }
  };

  function batteryClass(b) { return b === null || b === undefined ? '' : b <= 20 ? 'bad' : b <= 40 ? 'warn' : 'good'; }

  function deviceCard(d) {
    var busy = null;
    if (d.running !== undefined && d.running !== null) busy = Array.isArray(d.running) ? d.running.length : d.running;
    else if (d.free_slots !== undefined && d.free_slots !== null && d.slots !== undefined) busy = d.slots - d.free_slots;
    var control = d.control || {};
    var roles = control.roles || { jobs: true, ai: true, storage: true };
    var limits = control.limits || {};
    var safety = control.safety || {};
    var gpu = control.gpu;
    var slotsTxt = (busy === null ? '-' : busy) + ' / ' + (control.job_slots || d.slots || '?');
    var resting = safety.temp_state && safety.temp_state !== 'ok';
    var st = d.paused ? 'off' : (d.status !== 'online' ? (d.status || 'offline') : (resting ? 'resting' : 'online'));

    function roleToggle(key, label, title) {
      return h('button', {
        class: 'role-toggle' + (roles[key] ? ' on' : ''), disabled: !!d.paused, title: title,
        'aria-pressed': roles[key] ? 'true' : 'false', 'aria-label': label + (roles[key] ? ' (on)' : ' (off)'),
        onclick: function () {
          var change = {}; change[key] = !roles[key];
          patch({ settings: { roles: change } }, label + (roles[key] ? ' off' : ' on'));
        }
      }, label);
    }

    var gpuChip = null;
    if (gpu) {
      gpuChip = h('span', { class: 'chip' + (gpu.temp >= 80 ? ' warn' : ''), title: gpu.name || 'GPU' },
        'GPU ' + (gpu.temp === null || gpu.temp === undefined ? '-' : gpu.temp + ' °C') +
        (gpu.vram_free_mb !== undefined && gpu.vram_free_mb !== null ? ' · ' + PC.fmtBytes(gpu.vram_free_mb * 1048576) + ' free' : ''));
    }
    // catching wrong answers (SPEC 9.9): how its double-checked results went
    var trust = d.trust || {};
    var trustChip = null;
    if (trust.checks || trust.benched) {
      trustChip = h('span', {
        class: 'chip' + (trust.benched ? ' bad' : trust.state === 'checking more' ? ' warn' : ' good'),
        title: (trust.note ? trust.note + '. ' : '') + (trust.checks || 0) + ' check(s): ' + (trust.agreed || 0) +
          ' agreed, ' + (trust.wrong || 0) + ' wrong'
      }, trust.benched ? 'wrong answers: no jobs' : trust.state === 'checking more' ? 'checking more' :
        'trusted ' + Math.round((trust.score || 0) * 100) + '%');
    }
    // workers that update themselves (SPEC 8.4): only shown while not up to date
    var upd = d.update || {};
    var updateChip = null;
    if (upd.state === 'by_hand') {
      updateChip = h('span', { class: 'chip warn', title: 'This worker.py is from before self-updates. Fetch it once more ' +
        'by hand (run its join command again, or download /worker.py over it); after that it updates itself.' }, 'update by hand once');
    } else if (upd.state === 'waiting') {
      updateChip = h('span', { class: 'chip', title: 'It updates as soon as it is idle (' + (upd.reason || 'busy') + ')' }, 'update waiting');
    } else if (upd.state === 'due' || upd.state === 'updating') {
      updateChip = h('span', { class: 'chip', title: 'Fetching the new worker.py and restarting' }, 'updating…');
    }
    var limitsTxt = 'CPU ' + (limits.cpu_pct || 100) + '% (' + (control.job_slots || '?') + ' tasks, ' + (control.threads || '?') + ' model threads)' +
      ' · memory ' + (limits.mem_pct || 70) + '% (' + PC.fmtBytes((control.usable_mem_mb || 0) * 1048576) + ' for models)' +
      (gpu ? ' · GPU ' + limits.gpu_pct + '% (' + PC.fmtBytes((control.gpu_budget_mb || 0) * 1048576) + ')' : '');

    var card = h('div', { class: 'card device-card ' + (d.status === 'online' ? 'on' : 'off') },
      h('div', { class: 'device-head' },
        h('div', { class: 'device-name' }, d.name || PC.shortId(d.id)),
        PC.badge(st)),
      h('div', { class: 'device-meta' },
        h('span', { class: 'chip' }, d.platform || '?'),
        h('span', { class: 'chip ' + batteryClass(d.battery) },
          'battery ' + (d.battery === null || d.battery === undefined ? '-' : d.battery + '%') + (d.charging ? ' (charging)' : '')),
        h('span', { class: 'chip' + (resting ? ' warn' : '') }, 'temp ' + (d.temp === null || d.temp === undefined ? '-' : PC.fmtNum(d.temp, 1) + ' °C')),
        gpuChip,
        trustChip,
        updateChip,
        h('span', { class: 'chip' }, 'tasks ' + slotsTxt),
        h('span', { class: 'chip' }, 'score ' + (d.bench_score === null || d.bench_score === undefined ? '-' : PC.fmtNum(d.bench_score, 2))),
        d.storage_free_mb !== null && d.storage_free_mb !== undefined ? h('span', { class: 'chip' }, 'free ' + PC.fmtBytes(d.storage_free_mb * 1024 * 1024)) : null),
      h('div', { class: 'device-use' },
        h('span', { class: 'muted' }, 'Use for:'),
        roleToggle('jobs', 'Jobs', 'Normal tasks (maps, shell, media...)'),
        roleToggle('ai', 'AI models', 'Language models and other services'),
        roleToggle('storage', 'Storage', 'Pieces of pooled storage')),
      h('div', { class: 'device-limits' }, limitsTxt),
      (safety.notes || []).length ? h('ul', { class: 'device-notes' }, safety.notes.map(function (n) { return h('li', {}, n); })) : null,
      trust.benched ? h('div', { class: 'device-notes' }, trust.note || 'gave wrong answers repeatedly',
        ' ', h('button', {
          class: 'btn btn-sm', title: 'Forgive past wrong answers; its tasks are checked often at first',
          onclick: function () {
            PC.api.trustAgain(d.id).then(function () { PC.ok('Trusted again'); }).catch(PC.err);
          }
        }, 'Trust again')) : null,
      h('div', { class: 'device-sub muted' },
        'id ', h('code', {}, PC.shortId(d.id)), ' · caps ',
        h('code', {}, (d.caps || []).join(', ') || '-'), ' · seen ', PC.timeEl(d.last_seen)),
      h('div', { class: 'row gap wrap device-actions' },
        h('button', {
          class: 'btn btn-sm' + (d.paused ? ' btn-primary' : ''),
          title: d.paused ? 'Use this device again' : 'Keep it connected but use it for nothing; models move off it',
          onclick: function () { patch({ paused: !d.paused }, d.paused ? 'Switched on' : 'Switched off'); }
        }, d.paused ? 'Switch on' : 'Switch off'),
        h('button', { class: 'btn btn-sm', onclick: editLimits }, 'Limits'),
        h('button', { class: 'btn btn-sm', onclick: function () { PC.api.benchDevice(d.id).then(function () { PC.ok('Benchmark requested'); }).catch(PC.err); } }, 'Benchmark'),
        h('button', { class: 'btn btn-sm', onclick: notify }, 'Notify'),
        (d.platform === 'termux' || d.platform === 'linux') ? h('button', {
          class: 'btn btn-sm', title: 'Show and control its screen in a window on this PC (needs USB debugging on the phone)',
          onclick: showScreen
        }, 'Screen') : null,
        h('button', { class: 'btn btn-sm', onclick: rename }, 'Rename'),
        h('button', { class: 'btn btn-sm', onclick: editSettings }, 'Safety'),
        h('button', { class: 'btn btn-sm btn-danger', onclick: remove }, 'Remove')));

    function editLimits() {
      var cores = (d.info && d.info.cpu_count) || 1;
      function slider(min, max, value, describe) {
        var input = h('input', { type: 'range', min: String(min), max: String(max), step: '5', value: String(value) });
        var out = h('span', { class: 'limit-val' }, value + '%');
        var note = h('div', { class: 'muted' });
        function update() { out.textContent = input.value + '%'; note.textContent = describe(parseInt(input.value, 10)); }
        input.addEventListener('input', update);
        update();
        return { input: input, el: h('div', { class: 'limit-row' }, input, out, note) };
      }
      var cpu = slider(10, 100, limits.cpu_pct || 100, function (v) {
        var n = Math.max(1, Math.round(cores * v / 100));
        if (cores > 1) n = Math.min(n, cores - 1);
        return n + ' of ' + cores + ' cores for tasks and model threads (one core always stays idle)';
      });
      var mem = slider(10, 70, limits.mem_pct || 70, function (v) {
        return 'a model may take up to ' + v + '% of the free memory (70% is the safety ceiling)';
      });
      var rows = [PC.field('CPU', cpu.el), PC.field('Memory for models', mem.el)];
      var gpuSlider = null;
      if (gpu) {
        gpuSlider = slider(0, 90, limits.gpu_pct === undefined ? 80 : limits.gpu_pct, function (v) {
          var total = gpu.vram_total_mb || gpu.vram_mb || 0;
          return v === 0 ? 'the graphics card is not used'
            : 'models may use ' + v + '% of the ' + PC.fmtBytes(total * 1048576) + ' graphics memory (' + (gpu.name || 'GPU') +
              '); the card is watched for heat and freed at 85 °C';
        });
        rows.push(PC.field('Graphics card', gpuSlider.el));
      }
      PC.modal({
        title: 'Limits - ' + (d.name || PC.shortId(d.id)),
        body: h('div', {}, rows),
        actions: [
          { label: 'Cancel', onClick: function (c) { c(); } },
          {
            label: 'Save', kind: 'primary', onClick: function (c) {
              var settings = { cpu_pct: parseInt(cpu.input.value, 10), mem_pct: parseInt(mem.input.value, 10) };
              if (gpuSlider) settings.gpu_pct = parseInt(gpuSlider.input.value, 10);
              c();
              patch({ settings: settings }, 'Limits saved');
            }
          }]
      });
    }

    function patch(body, okMsg) {
      PC.api.patchDevice(d.id, body).then(function (res) {
        PC.ok(okMsg || 'Saved');
        var updated = (res && res.id) ? res : Object.assign({}, d, body);
        PC.upsert(PC.store.devices, updated);
        PC.bus.emit('devices', PC.store.devices);
      }).catch(PC.err);
    }

    function showScreen() {
      PC.ok('Opening the screen of ' + (d.name || PC.shortId(d.id)) + '...');
      PC.api.deviceScreen(d.id).then(function (res) {
        PC.ok(res && res.already_open ? 'Its screen is already open on the PC'
          : 'Screen open in a window on the PC (' + (res && res.via === 'wifi' ? 'over Wi-Fi' : 'over USB') + ')');
      }).catch(PC.err);
    }

    function rename() {
      PC.prompt('Device name', d.name || '', { title: 'Rename device' }).then(function (v) {
        if (v !== null && v.trim()) patch({ name: v.trim() }, 'Renamed');
      });
    }

    function notify() {
      var title = h('input', { class: 'input', value: 'PhoneCluster', autofocus: true });
      var body = h('input', { class: 'input', value: 'Hello from the dashboard' });
      PC.modal({
        title: 'Notify ' + (d.name || PC.shortId(d.id)),
        body: h('div', {}, PC.field('Title', title), PC.field('Body', body)),
        actions: [
          { label: 'Cancel', onClick: function (c) { c(); } },
          {
            label: 'Send', kind: 'primary', onClick: function (c) {
              PC.api.notifyDevice(d.id, title.value, body.value)
                .then(function () { c(); PC.ok('Notification sent'); }).catch(PC.err);
            }
          }]
      });
    }

    function editSettings() {
      var s = d.settings || {};
      var minB = h('input', { class: 'input', type: 'number', min: '20', max: '100', step: '1', value: s.min_battery !== undefined && s.min_battery !== null ? String(s.min_battery) : '', placeholder: '20 (Balanced)', autofocus: true });
      var maxT = h('input', { class: 'input', type: 'number', step: 'any', max: '40', value: s.max_temp !== undefined && s.max_temp !== null ? String(s.max_temp) : '', placeholder: '40 (Balanced)' });
      var slots = h('input', { class: 'input', type: 'number', min: '1', step: '1', value: d.slots === undefined || d.slots === null ? '' : String(d.slots) });
      PC.modal({
        title: 'Safety - ' + (d.name || PC.shortId(d.id)),
        body: h('div', {},
          h('p', { class: 'muted small' }, 'The Balanced profile always applies: no new work at 40 °C, work moves off at 43 °C, ',
            'back at 36 °C; models only when charging or above 50% battery. You can only make it stricter here.'),
          PC.field('Lowest battery for tasks (%)', minB, 'Not charging and below this: no tasks (at least 20)'),
          PC.field('Rest from (°C)', maxT, 'Battery temperature that pauses new work (at most 40)'),
          PC.field('Worker slots', slots, 'Most tasks the worker accepts at once; the CPU limit may lower it')),
        actions: [
          { label: 'Cancel', onClick: function (c) { c(); } },
          {
            label: 'Save', kind: 'primary', onClick: function (c) {
              var settings = {};
              if (minB.value !== '') settings.min_battery = parseInt(minB.value, 10);
              if (maxT.value !== '') settings.max_temp = parseFloat(maxT.value);
              var body = { settings: settings };
              if (slots.value !== '') body.slots = parseInt(slots.value, 10);
              PC.api.patchDevice(d.id, body).then(function (res) {
                c(); PC.ok('Saved');
                PC.upsert(PC.store.devices, (res && res.id) ? res : Object.assign({}, d, body));
                PC.bus.emit('devices', PC.store.devices);
              }).catch(PC.err);
            }
          }]
      });
    }

    function remove() {
      PC.confirm('Remove "' + (d.name || d.id) + '" from the cluster? Its running tasks are requeued and the worker will exit.',
        { title: 'Remove device', confirmLabel: 'Remove' }).then(function (yes) {
          if (!yes) return;
          PC.api.removeDevice(d.id).then(function () {
            PC.ok('Device removed');
            PC.store.devices = PC.store.devices.filter(function (x) { return x.id !== d.id; });
            PC.bus.emit('devices', PC.store.devices);
          }).catch(PC.err);
        });
    }

    return card;
  }

  /* =========================================================================
     Jobs
     ========================================================================= */
  /* How the cluster did against its fastest device alone (SPEC 9.5). */
  function speedLine(r) {
    if (r.cached) {
      // never compute the same thing twice (SPEC 9.8)
      return h('div', { class: 'speed-line' }, h('strong', { class: 'good' }, 'Reused'),
        ' the result of an identical earlier job ',
        r.from_job ? h('a', { class: 'link', href: '#/jobs/' + encodeURIComponent(r.from_job) }, PC.shortId(r.from_job)) : '',
        r.saved_s ? ' · saved about ' + PC.fmtNum(r.saved_s, 1) + ' s' : '');
    }
    if (!r.fastest_device) {
      return h('div', { class: 'speed-line' }, 'All ' + (r.reused_tasks || 0) + ' task(s) answered from earlier runs');
    }
    var best = r.fastest_device || {};
    var parts = ['Took ' + PC.fmtNum(r.wall_s, 1) + ' s'];
    if (r.single_device) {
      parts.push('only ' + (best.name || 'one device') + ' worked on it');
    } else {
      parts.push((best.name || 'the fastest device') + ' alone ≈ ' + PC.fmtNum(best.alone_s, 1) + ' s');
      parts.push(h('strong', { class: r.speedup >= 1 ? 'good' : 'warn' }, PC.fmtNum(r.speedup, 2) + '× with the cluster'));
    }
    var b = r.backups || {};
    if (b.started) parts.push(b.started + ' backup cop' + (b.started === 1 ? 'y' : 'ies') + ' of slow tasks (' + (b.won || 0) + ' finished first)');
    if (r.reused_tasks) parts.push(r.reused_tasks + ' task(s) answered from earlier runs');
    var c = r.checks || {};
    if (c.checked) {
      // catching wrong answers (SPEC 9.9)
      var text = c.checked + ' task(s) double-checked on another device';
      if (c.decided) text += ', ' + c.decided + ' disagreement(s) settled by a third device';
      if (c.disputed) text += ', ' + c.disputed + ' unresolved';
      parts.push(c.decided || c.disputed ? h('span', { class: 'warn' }, text) : text);
    }
    var out = h('div', { class: 'speed-line', title: (r.devices || []).map(function (x) {
      return x.name + ': ' + x.tasks + ' task(s), ' + PC.fmtNum(x.avg_s, 2) + ' s each';
    }).join('\n') });
    parts.forEach(function (p, i) {
      if (i) out.appendChild(document.createTextNode(' · '));
      out.appendChild(typeof p === 'string' ? document.createTextNode(p) : p);
    });
    return out;
  }

  /* reused / check / verified marks next to a task's status (SPEC 9.8, 9.9) */
  function taskTag(t) {
    var tag = t.reused ? ['reused', 'answered from an earlier run'] :
      t.role === 'check' ? ['check', 'the same work again on another device, to catch wrong answers'] :
      t.role === 'decide' ? ['decider', 'a third device settling a disagreement'] :
      t.verified === 1 ? ['checked', 'another device got the same answer'] :
      t.verified === -1 ? ['disputed', 'devices disagreed and nothing could decide'] : null;
    return tag ? h('span', { class: 'chip chip-sm' + (tag[0] === 'disputed' ? ' warn' : ''), title: tag[1] }, tag[0]) : null;
  }

  function progressBar(job) {
    var dyn = job.progress_units || (job.options || {})._dynamic;
    if (dyn && dyn.units) {
      // automatically sized job (SPEC 9.6): progress in units, pieces are cut as it goes
      var finished = job.status === 'done';
      var units = dyn.units, doneU = finished ? units : Math.min(units, dyn.done || 0);
      return h('div', { class: 'progress', title: doneU + ' of ' + units + ' done in ' + (job.total_tasks || 0) +
          ' piece(s) sized to each device\'s speed' },
        h('div', { class: 'progress-done', style: { width: (doneU / units * 100) + '%' } }),
        h('span', { class: 'progress-text' }, Math.round(doneU / units * 100) + '%'));
    }
    var total = job.total_tasks || 0;
    var done = job.done_tasks || 0, failed = job.failed_tasks || 0;
    var dp = total ? (done / total) * 100 : 0;
    var fp = total ? (failed / total) * 100 : 0;
    return h('div', { class: 'progress', title: done + ' done, ' + failed + ' failed, ' + total + ' total' },
      h('div', { class: 'progress-done', style: { width: dp + '%' } }),
      h('div', { class: 'progress-failed', style: { width: fp + '%' } }),
      h('span', { class: 'progress-text' }, total ? (done + failed) + '/' + total : '-'));
  }
  PC.progressBar = progressBar;

  views.jobs = {
    title: 'Jobs',
    render: function (root) {
      var filter = h('select', { class: 'input compact', onchange: function () { load(); } },
        h('option', { value: '' }, 'all statuses'),
        PC.STATUS_KINDS.map(function (s) { return h('option', { value: s }, s); }));
      var picker = h('input', { type: 'file', accept: '.zip,application/zip', style: 'display:none' });
      picker.addEventListener('change', function () {
        var file = picker.files && picker.files[0];
        picker.value = '';
        if (!file) return;
        PC.toast('Uploading ' + file.name + '…');
        PC.api.uploadPackage(file).then(function (res) {
          if (res && res.kind === 'pipeline') {
            var keys = Object.keys(res.jobs || {});
            PC.toast('Pipeline submitted: ' + keys.length + ' jobs', 'ok');
            load();
          } else if (res && res.job_id) {
            PC.toast('Package submitted', 'ok');
            location.hash = '#/jobs/' + res.job_id;
          } else {
            load();
          }
        }).catch(PC.err);
      });
      root.appendChild(page('Jobs', [
        filter,
        h('button', { class: 'btn btn-primary', onclick: function () { PC.newJobDialog(); } }, 'New job'),
        h('button', { class: 'btn', onclick: function () { PC.pipelineDialog(); } }, 'Pipeline'),
        h('button', { class: 'btn', title: 'Upload a .zip with job.yaml plus code and data files',
                      onclick: function () { picker.click(); } }, 'Run package'),
        h('button', { class: 'btn', onclick: function () { load(); } }, 'Refresh'),
        picker
      ]));
      var t = table(['Name', 'Type', 'Status', 'Progress', 'Created', '']);
      root.appendChild(t.el);

      function draw(jobs) {
        PC.clear(t.body);
        var want = filter.value;
        var list = (jobs || []).filter(function (j) { return !want || j.status === want; });
        list = list.slice().sort(function (a, b) { return (b.created || 0) - (a.created || 0); });
        if (!list.length) { t.body.appendChild(emptyRow(6, 'No jobs' + (want ? ' with status "' + want + '"' : '') + '.')); return; }
        list.forEach(function (j) {
          var open = function () { location.hash = '#/jobs/' + encodeURIComponent(j.id); };
          t.body.appendChild(h('tr', { class: 'clickable', tabindex: '0', onclick: open, onkeydown: function (e) { if (e.key === 'Enter') open(); } },
            h('td', {}, h('a', { class: 'link', href: '#/jobs/' + encodeURIComponent(j.id) }, j.name || (j.type + ' ' + PC.shortId(j.id)))),
            h('td', {}, j.type || '-'),
            h('td', {}, PC.badge(j.status)),
            h('td', {}, progressBar(j)),
            h('td', {}, PC.timeEl(j.created)),
            h('td', { class: 'right' }, jobRowActions(j, load))));
        });
      }

      function load() {
        PC.api.jobs(filter.value || null, 200).then(function (jobs) {
          PC.store.setJobs(jobs);
        }).catch(PC.err);
      }

      draw(PC.store.jobs);
      load();
      var off = PC.bus.on('jobs', draw);
      return function () { off(); };
    }
  };

  function jobRowActions(j, reload) {
    var wrap = h('div', { class: 'row gap end' });
    var active = j.status === 'running' || j.status === 'pending' || j.status === 'waiting';
    if (active) {
      wrap.appendChild(h('button', {
        class: 'btn btn-sm', title: 'Cancel', onclick: function (e) {
          e.stopPropagation();
          PC.api.cancelJob(j.id).then(function () { PC.ok('Cancelled'); reload && reload(); }).catch(PC.err);
        }
      }, 'Cancel'));
    }
    if (j.status === 'failed') {
      wrap.appendChild(h('button', {
        class: 'btn btn-sm', title: 'Retry failed tasks', onclick: function (e) {
          e.stopPropagation();
          PC.api.retryJob(j.id).then(function () { PC.ok('Retrying'); reload && reload(); }).catch(PC.err);
        }
      }, 'Retry'));
    }
    wrap.appendChild(h('button', {
      class: 'btn btn-sm btn-danger', title: 'Delete', onclick: function (e) {
        e.stopPropagation();
        PC.confirm('Delete job "' + (j.name || j.id) + '"?', { title: 'Delete job', confirmLabel: 'Delete' }).then(function (yes) {
          if (!yes) return;
          PC.api.deleteJob(j.id).then(function () {
            PC.ok('Deleted');
            PC.store.jobs = PC.store.jobs.filter(function (x) { return x.id !== j.id; });
            PC.bus.emit('jobs', PC.store.jobs);
          }).catch(PC.err);
        });
      }
    }, 'Delete'));
    return wrap;
  }

  /* ---------------- New job dialog ---------------- */
  PC.newJobDialog = function (initialSpec) {
    var box = h('div', { class: 'dialog-body' }, h('p', { class: 'muted' }, 'Loading plugins…'));
    var m = PC.modal({ title: 'New job', body: box, actions: [] });
    Promise.all([PC.loadPlugins(), PC.loadDevices()]).then(function (r) {
      var form = new PC.JobForm({ plugins: r[0], devices: r[1] });
      PC.clear(box);
      var bar = h('div', { class: 'row gap wrap dialog-bar' },
        h('button', { class: 'btn btn-primary', onclick: submit }, 'Submit job'),
        h('button', { class: 'btn', onclick: saveTemplate }, 'Save as template'),
        h('button', { class: 'btn', onclick: loadTemplate }, 'Load template'),
        h('button', { class: 'btn', onclick: function () { m.close(); } }, 'Cancel'));
      box.appendChild(form.el);
      box.appendChild(bar);
      if (initialSpec) form.setSpec(initialSpec);

      function submit() {
        var spec;
        try { spec = form.getSpec(); } catch (e) { PC.err(e); return; }
        PC.api.submitJob(spec).then(function (job) {
          m.close();
          PC.ok('Job submitted');
          if (job && job.id) {
            PC.upsert(PC.store.jobs, job);
            PC.bus.emit('jobs', PC.store.jobs);
            location.hash = '#/jobs/' + encodeURIComponent(job.id);
          }
        }).catch(PC.err);
      }
      function saveTemplate() {
        var spec;
        try { spec = form.getSpec(); } catch (e) { PC.err(e); return; }
        PC.prompt('Template name', spec.name || spec.type, { title: 'Save as template' }).then(function (name) {
          if (!name) return;
          PC.api.createTemplate(name, spec).then(function () { PC.ok('Template saved'); }).catch(PC.err);
        });
      }
      function loadTemplate() {
        PC.api.templates().then(function (list) {
          if (!list.length) { PC.toast('No templates saved yet', 'info'); return; }
          var sel = h('select', { class: 'input', autofocus: true },
            list.map(function (t) { return h('option', { value: t.id }, t.name || t.id); }));
          PC.modal({
            title: 'Load template',
            body: h('div', {}, PC.field('Template', sel)),
            actions: [
              { label: 'Cancel', onClick: function (c) { c(); } },
              {
                label: 'Delete', kind: 'danger', onClick: function (c) {
                  var id = sel.value;
                  PC.api.deleteTemplate(id).then(function () { c(); PC.ok('Template deleted'); }).catch(PC.err);
                }
              },
              {
                label: 'Load', kind: 'primary', onClick: function (c) {
                  var t = list.filter(function (x) { return x.id === sel.value; })[0];
                  if (t) form.setSpec(t.job_spec || {});
                  c();
                }
              }]
          });
        }).catch(PC.err);
      }
    }).catch(function (e) {
      PC.clear(box);
      box.appendChild(h('p', { class: 'error-text' }, 'Could not load plugins: ' + (e.message || e)));
    });
  };

  /* ---------------- Pipeline builder ---------------- */
  PC.pipelineDialog = function () {
    var box = h('div', { class: 'dialog-body' }, h('p', { class: 'muted' }, 'Loading plugins…'));
    var m = PC.modal({ title: 'Pipeline builder', body: box, actions: [] });
    Promise.all([PC.loadPlugins(), PC.loadDevices()]).then(function (r) {
      var plugins = r[0], devices = r[1];
      var steps = [];
      PC.clear(box);
      var nameInput = h('input', { class: 'input', placeholder: 'my pipeline' });
      var stepsBox = h('div', { class: 'steps' });
      box.appendChild(PC.field('Pipeline name', nameInput));
      box.appendChild(stepsBox);
      box.appendChild(h('div', { class: 'row gap wrap dialog-bar' },
        h('button', { class: 'btn', onclick: function () { addStep(); } }, '+ Add step'),
        h('button', { class: 'btn btn-primary', onclick: submit }, 'Submit pipeline'),
        h('button', { class: 'btn', onclick: function () { m.close(); } }, 'Cancel')));

      function keyOptions(exclude) {
        return steps.filter(function (s) { return s !== exclude; }).map(function (s) { return s.keyInput.value; }).filter(Boolean);
      }

      function refreshDeps() {
        steps.forEach(function (s) {
          var chosen = PC.qsa('option', s.depSel).filter(function (o) { return o.selected; }).map(function (o) { return o.value; });
          PC.clear(s.depSel);
          keyOptions(s).forEach(function (k) {
            var o = h('option', { value: k }, k);
            if (chosen.indexOf(k) >= 0) o.selected = true;
            s.depSel.appendChild(o);
          });
          if (!keyOptions(s).length) s.depSel.appendChild(h('option', { value: '', disabled: true }, 'no other steps'));
        });
      }

      function addStep() {
        var idx = steps.length;
        var keyInput = h('input', { class: 'input compact', value: String.fromCharCode(97 + (idx % 26)), oninput: refreshDeps });
        var depSel = h('select', { class: 'input multi', multiple: true, size: '3' });
        var form = new PC.JobForm({ plugins: plugins, devices: devices });
        var wrap = h('div', { class: 'card step-card' },
          h('div', { class: 'row gap wrap step-head' },
            h('label', { class: 'inline-field' }, h('span', {}, 'key'), keyInput),
            h('label', { class: 'inline-field' }, h('span', {}, 'depends on'), depSel),
            h('button', { class: 'btn btn-sm btn-danger', onclick: function () { removeStep(step); } }, 'Remove step')),
          form.el);
        var step = { keyInput: keyInput, depSel: depSel, form: form, wrap: wrap };
        steps.push(step);
        stepsBox.appendChild(wrap);
        refreshDeps();
      }

      function removeStep(step) {
        steps = steps.filter(function (s) { return s !== step; });
        if (step.wrap.parentNode) step.wrap.parentNode.removeChild(step.wrap);
        refreshDeps();
      }

      function submit() {
        if (!steps.length) { PC.toast('Add at least one step', 'error'); return; }
        var jobs = [], seen = {};
        try {
          steps.forEach(function (s) {
            var key = s.keyInput.value.trim();
            if (!key) throw new Error('every step needs a key');
            if (seen[key]) throw new Error('duplicate step key "' + key + '"');
            seen[key] = true;
            var spec = s.form.getSpec();
            spec.key = key;
            var deps = PC.qsa('option', s.depSel).filter(function (o) { return o.selected && o.value; }).map(function (o) { return o.value; });
            if (deps.length) spec.depends_on = deps;
            jobs.push(spec);
          });
        } catch (e) { PC.err(e); return; }
        PC.api.pipeline({ name: nameInput.value.trim() || 'pipeline', jobs: jobs }).then(function (res) {
          m.close();
          PC.ok('Pipeline submitted');
          location.hash = '#/jobs';
          if (res && res.jobs) {
            var first = res.jobs[Object.keys(res.jobs)[0]];
            if (first) location.hash = '#/jobs/' + encodeURIComponent(first);
          }
        }).catch(PC.err);
      }

      addStep();
    }).catch(function (e) {
      PC.clear(box);
      box.appendChild(h('p', { class: 'error-text' }, 'Could not load plugins: ' + (e.message || e)));
    });
  };

  /* =========================================================================
     Job detail
     ========================================================================= */
  views.job = {
    title: 'Job',
    render: function (root, params) {
      var id = params.id;
      var subscribedTask = null;
      var header = h('div', {});
      var body = h('div', {});
      root.appendChild(header);
      root.appendChild(body);
      header.appendChild(page('Job ' + PC.shortId(id), [h('a', { class: 'btn', href: '#/jobs' }, '← All jobs')]));

      var summary = h('div', { class: 'card' }, h('p', { class: 'muted' }, 'Loading…'));
      var tasksCard = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'Tasks'));
      var tasksTable = table(['#', 'Status', 'Device', 'Attempts', 'Duration', 'Outputs']);
      tasksCard.appendChild(tasksTable.el);
      var logCard = h('div', { class: 'card' },
        h('h2', { class: 'card-title' }, 'Task log'),
        h('p', { class: 'muted', id: 'log-hint' }, 'Select a task above to stream its log.'));
      var logPre = h('pre', { class: 'logpane', tabindex: '0' });
      logCard.appendChild(logPre);
      var resultCard = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'Result'));
      var resultBox = h('div', {}, h('p', { class: 'muted' }, 'No result yet.'));
      resultCard.appendChild(resultBox);
      var outputsCard = h('div', { class: 'card' }, h('h2', { class: 'card-title' }, 'Outputs'));
      var outputsBox = h('div', {}, h('p', { class: 'muted' }, 'No outputs.'));
      outputsCard.appendChild(outputsBox);

      body.appendChild(summary);
      body.appendChild(tasksCard);
      body.appendChild(logCard);
      body.appendChild(h('div', { class: 'grid-2 stretch' }, resultCard, outputsCard));

      var tasks = [];
      var job = null;

      function drawSummary(j) {
        job = j;
        PC.clear(summary);
        summary.appendChild(h('div', { class: 'row gap wrap between' },
          h('div', {},
            h('h2', { class: 'card-title' }, j.name || (j.type + ' ' + PC.shortId(j.id))),
            h('div', { class: 'muted' },
              'type ', h('code', {}, j.type || '-'), ' · id ', h('code', {}, j.id),
              j.pipeline_id ? ' · pipeline ' + PC.shortId(j.pipeline_id) : '',
              ' · created ', PC.timeEl(j.created),
              j.finished ? h('span', {}, ' · finished ', PC.timeEl(j.finished)) : '')),
          PC.badge(j.status)));
        summary.appendChild(progressBar(j));
        if (j.expired) {
          // SPEC 9.10: an old job keeps only its final result (Settings: clean up finished jobs after)
          summary.appendChild(h('p', { class: 'muted small' }, 'An old job: its tasks, logs and files were removed ',
            PC.timeEl(j.expired), '; only the final result is kept.'));
        } else if (j.cleaned) {
          // SPEC 9.10: an hour after a job succeeded only its final result is kept
          summary.appendChild(h('p', { class: 'muted small' }, 'The tasks\' own results and in-between files were cleaned ',
            PC.timeEl(j.cleaned), '; the final result and its files are kept.'));
        }
        var dyn = j.progress_units || (j.options || {})._dynamic;
        summary.appendChild(h('div', { class: 'muted small' },
          (j.done_tasks || 0) + ' done · ' + (j.failed_tasks || 0) + ' failed · ' + (j.total_tasks || 0) +
          (dyn ? ' pieces so far, each sized to its device\'s speed (' + (dyn.done || 0) + ' of ' + dyn.units + ' units done)'
               : ' total') +
          (j.priority ? ' · priority ' + j.priority : '')));
        if (j.error) summary.appendChild(h('p', { class: 'error-text' }, String(j.error)));
        if (j.report) summary.appendChild(speedLine(j.report));
        if (j.rechecked) {
          summary.appendChild(h('div', { class: 'speed-line warn' },
            'Computed again after a device that worked on it gave a wrong answer elsewhere'));
        }
        if (Array.isArray(j.depends_on) && j.depends_on.length) {
          summary.appendChild(h('div', { class: 'muted small' }, 'depends on: ',
            j.depends_on.map(function (dep) {
              return h('a', { class: 'link', href: '#/jobs/' + encodeURIComponent(dep) }, PC.shortId(dep) + ' ');
            })));
        }
        var actions = h('div', { class: 'row gap wrap card-actions' });
        var active = j.status === 'running' || j.status === 'pending' || j.status === 'waiting';
        if (active) actions.appendChild(h('button', { class: 'btn', onclick: function () { PC.api.cancelJob(id).then(function () { PC.ok('Cancelled'); reload(); }).catch(PC.err); } }, 'Cancel'));
        actions.appendChild(h('button', { class: 'btn', onclick: function () { PC.api.retryJob(id).then(function () { PC.ok('Retrying failed tasks'); reload(); }).catch(PC.err); } }, 'Retry'));
        if (!active) {
          // a new job with the same spec; identical work is answered from stored results (SPEC 9.8)
          actions.appendChild(h('button', {
            class: 'btn', title: 'Submit the same job again (results of identical work are reused)',
            onclick: function () { PC.api.rerunJob(id, false).then(function (nj) { location.hash = '#/jobs/' + encodeURIComponent(nj.id); }).catch(PC.err); }
          }, 'Run again'));
          actions.appendChild(h('button', {
            class: 'btn', title: 'Compute everything again and replace the stored results',
            onclick: function () { PC.api.rerunJob(id, true).then(function (nj) { location.hash = '#/jobs/' + encodeURIComponent(nj.id); }).catch(PC.err); }
          }, 'Run again fresh'));
        }
        ['json', 'csv', 'zip'].forEach(function (fmt) {
          actions.appendChild(h('button', {
            class: 'btn btn-sm', title: 'Save the export into data\\downloads',
            onclick: function () { PC.api.save('/jobs/' + encodeURIComponent(id) + '/export/save?format=' + fmt); }
          }, 'Export ' + fmt));
        });
        actions.appendChild(h('button', {
          class: 'btn btn-sm btn-danger', onclick: function () {
            PC.confirm('Delete this job?', { title: 'Delete job', confirmLabel: 'Delete' }).then(function (yes) {
              if (!yes) return;
              PC.api.deleteJob(id).then(function () { PC.ok('Deleted'); location.hash = '#/jobs'; }).catch(PC.err);
            });
          }
        }, 'Delete'));
        summary.appendChild(actions);

        if (j.result !== undefined && j.result !== null) drawResult(j.result);
      }

      function drawResult(res) {
        PC.clear(resultBox);
        var pre = h('pre', { class: 'json' });
        pre.innerHTML = PC.jsonHtml(res);
        resultBox.appendChild(h('div', { class: 'row gap end' },
          h('button', { class: 'btn btn-sm', onclick: function () { PC.copy(PC.pretty(res)); } }, 'Copy JSON')));
        resultBox.appendChild(pre);
      }

      function drawTasks() {
        PC.clear(tasksTable.body);
        if (!tasks.length) { tasksTable.body.appendChild(emptyRow(6, 'No tasks yet.')); return; }
        tasks.slice().sort(function (a, b) { return (a.idx || 0) - (b.idx || 0); }).forEach(function (t) {
          var dev = PC.store.devices.filter(function (d) { return d.id === t.device_id; })[0];
          var outs = t.outputs || [];
          var sel = function () { selectTask(t); };
          var tr = h('tr', {
            class: 'clickable' + (subscribedTask === t.id ? ' selected' : ''), tabindex: '0',
            onclick: sel, onkeydown: function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sel(); } }
          },
            h('td', {}, String(t.idx === undefined ? '-' : t.idx)),
            h('td', {}, PC.badge(t.status), taskTag(t)),
            h('td', {}, dev ? (dev.name || PC.shortId(t.device_id)) : (t.device_id ? PC.shortId(t.device_id) : (t.reused ? 'earlier run' : '-'))),
            h('td', {}, (t.attempts === undefined ? '-' : t.attempts) + (t.max_attempts ? ' / ' + t.max_attempts : '')),
            h('td', {}, PC.fmtDur(t.duration)),
            h('td', {}, outs.length ? String(outs.length) : '-'));
          if (t.error) tr.title = String(t.error);
          tasksTable.body.appendChild(tr);
        });
        drawOutputs();
      }

      function drawOutputs() {
        PC.clear(outputsBox);
        var all = [];
        tasks.forEach(function (t) { (t.outputs || []).forEach(function (o) { all.push({ task: t, out: o }); }); });
        if (!all.length) { outputsBox.appendChild(h('p', { class: 'muted' }, 'No outputs.')); return; }
        var list = h('ul', { class: 'plain-list' });
        all.forEach(function (x) {
          var o = x.out;
          list.appendChild(h('li', { class: 'row gap between' },
            h('span', {}, h('code', {}, '#' + x.task.idx), ' ', o.name || o.file_id, ' ', h('span', { class: 'muted' }, PC.fmtBytes(o.size))),
            h('button', {
              class: 'btn btn-sm', title: 'Save into data\\downloads',
              onclick: function () { PC.api.save('/files/' + encodeURIComponent(o.file_id) + '/save', o.name); }
            }, 'Save')));
        });
        outputsBox.appendChild(list);
      }

      /* --- log pane --- */
      var logLines = [];
      function setLogHint(txt) {
        var hint = PC.qs('#log-hint', logCard);
        if (hint) hint.textContent = txt;
      }
      function appendLog(lines) {
        var atBottom = logPre.scrollTop + logPre.clientHeight >= logPre.scrollHeight - 30;
        lines.forEach(function (l) {
          logLines.push(l);
          logPre.appendChild(document.createTextNode(String(l) + '\n'));
        });
        while (logLines.length > 5000 && logPre.firstChild) { logLines.shift(); logPre.removeChild(logPre.firstChild); }
        if (atBottom) logPre.scrollTop = logPre.scrollHeight;
      }
      function selectTask(t) {
        if (subscribedTask) PC.ws.unsubLogs(subscribedTask);
        subscribedTask = t.id;
        logLines = [];
        PC.clear(logPre);
        setLogHint('Task #' + t.idx + ' · ' + t.status + ' · ' + PC.shortId(t.id) +
          (PC.ws.state === 'open' ? ' · streaming' : ' · offline (socket down)'));
        drawTasks();
        PC.api.taskLog(t.id, 500).then(function (lines) {
          appendLog(lines.filter(function (l) { return l !== ''; }));
          if (!lines.length) appendLog(['(no log lines yet)']);
        }).catch(function (e) { appendLog(['(could not load log: ' + (e.message || e) + ')']); });
        PC.ws.subLogs(t.id);
        if (t.result !== undefined && t.result !== null) {
          PC.clear(resultBox);
          resultBox.appendChild(h('div', { class: 'muted small' }, 'Task #' + t.idx + ' result'));
          var pre = h('pre', { class: 'json' });
          pre.innerHTML = PC.jsonHtml(t.result);
          resultBox.appendChild(pre);
          resultBox.appendChild(h('button', {
            class: 'btn btn-sm', onclick: function () { if (job) drawResult(job.result); }
          }, 'Show job result'));
        }
        if (t.error) {
          resultBox.appendChild(h('p', { class: 'error-text' }, String(t.error)));
        }
      }

      function reload() {
        PC.api.job(id).then(drawSummary).catch(function (e) {
          PC.clear(summary);
          summary.appendChild(h('p', { class: 'error-text' }, 'Could not load job: ' + (e.message || e)));
        });
        PC.api.jobTasks(id).then(function (list) { tasks = list; drawTasks(); }).catch(function () { });
      }
      reload();
      PC.loadDevices().then(function () { drawTasks(); }).catch(function () { });

      var offs = [
        PC.bus.on('job', function (j) { if (j.id === id) { PC.api.job(id).then(drawSummary).catch(function () { }); } }),
        PC.bus.on('task', function (t) {
          if (t.job_id !== id) return;
          PC.upsert(tasks, t);
          drawTasks();
        }),
        PC.bus.on('log', function (m) { if (m.task_id === subscribedTask) appendLog(m.lines || []); })
      ];
      var poll = setInterval(function () { if (PC.ws.state !== 'open') reload(); }, 5000);

      return function () {
        offs.forEach(function (f) { f(); });
        clearInterval(poll);
        if (subscribedTask) PC.ws.unsubLogs(subscribedTask);
      };
    }
  };
})();
