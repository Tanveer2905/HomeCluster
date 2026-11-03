/* PhoneCluster dashboard - Models view (SPEC 6.8): GGUF models, running one
   model across the devices you pick, and chatting with it. */
(function () {
  'use strict';
  var PC = (window.PC = window.PC || {});
  var views = PC.views;
  var h = PC.h;
  var page = PC.page, table = PC.table;

  function mb(n) { return n === null || n === undefined ? '-' : (n >= 1024 ? (n / 1024).toFixed(1) + ' GB' : Math.round(n) + ' MB'); }
  function bytesMb(b) { return b ? mb(b / 1048576) : '-'; }
  function slug(file) {
    return String(file || '').replace(/\.gguf$/i, '').toLowerCase().replace(/[^a-z0-9._-]+/g, '-').slice(0, 40) || 'model';
  }
  function buildLabel(key) {
    return { 'win-x64': 'prebuilt (Windows)', 'android-arm64': 'prebuilt (Android 9+)', source: 'compiles on first start' }[key] || key;
  }
  function badge(status) {
    var cls = { ready: 'done', loading: 'running', starting: 'running', degraded: 'assigned', failed: 'failed', stopped: 'cancelled', idle: 'cancelled' }[status] || 'queued';
    return h('span', { class: 'badge badge-' + cls, title: status === 'idle' ? 'unloaded after sitting unused; the next message loads it again' : '' },
      status === 'idle' ? 'idle (unloaded)' : status);
  }
  function newId() {
    var s = 'm';
    for (var i = 0; i < 12; i++) { s += Math.floor(Math.random() * 16).toString(16); }
    return s;
  }

  views.models = {
    title: 'Models',
    render: function (root) {
      var state = { models: [], devices: [], deployments: [], runtime: null, planTimer: null, chats: {},
                    services: [], defaultModel: null };

      root.appendChild(page('Models', [
        h('button', { class: 'btn', onclick: loadAll }, 'Refresh')
      ]));
      root.appendChild(h('p', { class: 'muted' },
        'Run a language model on the cluster. A model that fits on one device runs there alone, on the fastest one ',
        '(the PC\'s graphics card first); a model too big for any single device is split over several, each holding part of the layers. ',
        'The finished model appears on the Services page, so every device (a browser-only phone too) can use it.'));

      var runtimeEl = h('div', { class: 'muted small' });
      root.appendChild(runtimeEl);

      /* ---------------- model files ---------------- */
      var modelsTable = table(['Model', 'Size', 'Architecture', 'Layers', 'Context', 'Used by', '']);
      var picker = h('input', { type: 'file', accept: '.gguf', class: 'offscreen' });
      var uploadBar = h('div', { class: 'progress-bar', hidden: true }, h('span', { style: 'width:0%' }));
      var urlIn = h('input', { class: 'input mono', placeholder: 'https://huggingface.co/.../resolve/main/model-Q4_K_M.gguf' });
      picker.addEventListener('change', function () {
        var file = picker.files && picker.files[0];
        picker.value = '';
        if (!file) return;
        uploadBar.hidden = false;
        PC.api.llmUploadModel(file, function (f) { uploadBar.firstChild.style.width = Math.round(f * 100) + '%'; })
          .then(function () { PC.toast('Uploaded ' + file.name, 'ok'); loadModels(); })
          .catch(PC.err).then(function () { uploadBar.hidden = true; });
      });
      root.appendChild(h('div', { class: 'card' },
        h('h3', {}, 'Model files'),
        h('p', { class: 'muted small' }, 'GGUF files in data\\models on the PC. Q4_K_M quantizations are a good size/quality balance for phones.'),
        modelsTable.el,
        h('div', { class: 'row gap wrap', style: 'margin-top:10px' },
          h('button', { class: 'btn', onclick: function () { picker.click(); } }, 'Upload .gguf'), picker,
          urlIn,
          h('button', { class: 'btn', onclick: fetchUrl }, 'Download from URL')),
        uploadBar));

      function fetchUrl() {
        var url = urlIn.value.trim();
        if (!url) { PC.toast('Paste a .gguf URL first', 'error'); return; }
        PC.api.llmFetchModel(url).then(function () { urlIn.value = ''; PC.toast('Download started', 'ok'); loadModels(); })
          .catch(PC.err);
      }

      /* ---------------- deploy ---------------- */
      var modelSel = h('select', { class: 'input', onchange: function () { nameIn.value = slug(modelSel.value); schedulePlan(); } });
      var nameIn = h('input', { class: 'input mono', placeholder: 'service name, e.g. qwen-small' });
      var ctxSel = h('select', { class: 'input compact', onchange: schedulePlan },
        [1024, 2048, 4096, 8192].map(function (n) { return h('option', { value: String(n), selected: n === 2048 }, n + ' tokens'); }));
      var threadsIn = h('input', { class: 'input compact', type: 'number', min: '0', value: '0', title: '0 = every core the device allows' });
      var autoRadio = h('input', { type: 'radio', name: 'llm-mode', value: 'auto', checked: true, onchange: modeChanged });
      var manualRadio = h('input', { type: 'radio', name: 'llm-mode', value: 'manual', onchange: modeChanged });
      var modeHelp = h('p', { class: 'muted small' });
      var devTable = table(['Use', 'Host', 'Device', 'Platform', 'llama.cpp', 'Free', 'For models', 'GPU', 'Share weight', '']);
      var planEl = h('div', { class: 'plan' });
      var deployBtn = h('button', { class: 'btn btn-primary', onclick: function () { deploy(false); } }, 'Run model');
      var forceBtn = h('button', { class: 'btn btn-danger', hidden: true, onclick: function () { deploy(true); } }, 'Run anyway');
      root.appendChild(h('div', { class: 'card' },
        h('h3', {}, 'Run a model'),
        h('div', { class: 'form-grid' },
          h('label', {}, 'model', modelSel),
          h('label', {}, 'name', nameIn),
          h('label', {}, 'context', h('div', { class: 'row gap' }, ctxSel, h('span', { class: 'muted small' }, 'threads'), threadsIn)),
          h('div', { class: 'row gap' }, h('span', { class: 'muted small' }, 'placement'),
            h('label', { class: 'inline-choice' }, autoRadio, ' Automatic (recommended)'),
            h('label', { class: 'inline-choice' }, manualRadio, ' I choose'))),
        modeHelp,
        devTable.el, planEl,
        h('div', { class: 'row gap' }, deployBtn, forceBtn)));

      function mode() { return manualRadio.checked ? 'manual' : 'auto'; }
      function modeChanged() {
        modeHelp.textContent = mode() === 'auto'
          ? 'Untick a device to keep it out. The cluster runs the model alone on the fastest device that holds it ' +
            'without strain, or splits it over the fewest fast devices when it is too big. If a device gets too hot, ' +
            'switched off or disconnected, the model moves to the others.'
          : 'Tick the devices that should hold the model and pick one host (it keeps the model file and serves chats). ' +
            'Leave share weights empty to split by usable memory, or type numbers (e.g. 3 and 1) to set the proportions yourself. ' +
            'A device on its own (also the host) runs the model alone.';
        PC.qsa('input[name=llm-host], input[data-weight]', devTable.body).forEach(function (el) {
          el.disabled = mode() === 'auto' || el.getAttribute('data-eligible') !== '1';
        });
        schedulePlan();
      }

      /* ---------------- deployments ---------------- */
      var depsEl = h('div', {});
      root.appendChild(h('div', { class: 'card' }, h('h3', {}, 'Running models'), depsEl));

      /* ---------------- chat ---------------- */
      var chatSel = h('select', { class: 'input' });
      var chatLog = h('div', { class: 'chat-log' });
      var chatIn = h('textarea', { class: 'input', rows: '3', placeholder: 'Ask something... (Ctrl+Enter to send)' });
      var maxTok = h('input', { class: 'input compact', type: 'number', min: '1', value: '256' });
      var temp = h('input', { class: 'input compact', type: 'number', min: '0', max: '2', step: '0.1', value: '0.7' });
      var sendBtn = h('button', { class: 'btn btn-primary', onclick: send }, 'Send');
      var clearBtn = h('button', { class: 'btn', onclick: function () { state.chats[chatSel.value] = []; drawChat(); } }, 'New chat');
      var chatStatus = h('span', { class: 'muted small' });
      chatIn.addEventListener('keydown', function (e) { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } });
      root.appendChild(h('div', { class: 'card' },
        h('h3', {}, 'Chat'),
        h('div', { class: 'row gap wrap' }, chatSel, h('span', { class: 'muted small' }, 'max tokens'), maxTok,
          h('span', { class: 'muted small' }, 'temperature'), temp, clearBtn),
        chatLog, chatIn, h('div', { class: 'row gap' }, sendBtn, chatStatus)));
      chatSel.addEventListener('change', drawChat);

      /* ---------------- loading ---------------- */
      function loadModels() {
        return Promise.all([PC.api.llmModels(), PC.api.llmDefault().catch(function () { return {}; })])
          .then(function (r) { state.models = r[0]; state.defaultModel = (r[1] || {}).model || null; drawModels(); });
      }
      function loadDevices() {
        return PC.api.llmDevices().then(function (list) { state.devices = list; drawDevices(); });
      }
      function loadDeployments() {
        return Promise.all([PC.api.llmDeployments(), PC.api.services().catch(function () { return []; })])
          .then(function (r) {
            state.deployments = r[0];
            state.services = PC.asList(r[1], 'services');
            drawDeployments();
            drawChatSelect();
          });
      }
      function loadAll() {
        PC.api.llmRuntime().then(function (rt) {
          state.runtime = rt;
          var have = Object.keys(rt.assets || {});
          runtimeEl.textContent = rt.tag
            ? 'llama.cpp ' + rt.tag + ' on the coordinator (' + (have.length ? have.join(', ') : 'no builds') +
              (rt.gpu_build ? '; GPU build for the PC' : '; no GPU build - scripts\\get-llama.ps1 -Vulkan adds one') +
              '). Every device runs this exact version.'
            : 'llama.cpp is not installed on the coordinator: run scripts\\get-llama.ps1 on the PC.';
        }).catch(function () { });
        Promise.all([loadModels(), loadDevices(), loadDeployments()]).then(schedulePlan).catch(PC.err);
      }

      /* ---------------- drawing ---------------- */
      function drawModels() {
        PC.clear(modelsTable.body);
        var current = modelSel.value;
        PC.clear(modelSel);
        var files = state.models.filter(function (m) { return !m.status || m.status === 'done'; });
        files.forEach(function (m) {
          modelSel.appendChild(h('option', { value: m.name }, m.name + ' (' + bytesMb(m.size) + ')'));
          modelsTable.body.appendChild(h('tr', {},
            h('td', { class: 'mono' }, m.name, m.title ? h('div', { class: 'muted small' }, m.title + (m.size_label ? ' - ' + m.size_label : '')) : null),
            h('td', {}, bytesMb(m.size)),
            h('td', {}, m.architecture || (m.error ? 'unreadable' : '-')),
            h('td', {}, m.layers ? String(m.layers) : '-'),
            h('td', {}, m.context_length ? String(m.context_length) : '-'),
            h('td', {}, (m.used_by || []).join(', ')),
            h('td', { class: 'row gap' },
              state.defaultModel === m.name
                ? h('span', { class: 'chip good', title: 'the name "chat" reaches this model' }, 'default for chat')
                : h('button', { class: 'btn btn-sm', title: 'Make the name "chat" reach this model', onclick: function () {
                  PC.api.llmSetDefault(m.name).then(function () { PC.toast('"chat" now reaches ' + m.name, 'ok'); loadModels(); loadDeployments(); }).catch(PC.err);
                } }, 'Use for chat'),
              h('button', { class: 'btn btn-sm btn-danger', disabled: (m.used_by || []).length > 0, onclick: function () {
                PC.confirm('Delete ' + m.name + ' from the PC?').then(function (ok) {
                  if (ok) PC.api.llmDeleteModel(m.name).then(loadModels).catch(PC.err);
                });
              } }, 'Delete'))));
        });
        state.models.filter(function (m) { return m.status && m.status !== 'done'; }).forEach(function (f) {
          var pct = f.total ? Math.round(100 * f.bytes / f.total) : null;
          modelsTable.body.appendChild(h('tr', {},
            h('td', { class: 'mono' }, f.name),
            h('td', {}, bytesMb(f.bytes) + (f.total ? ' / ' + bytesMb(f.total) : '')),
            h('td', { colspan: '5' }, f.status === 'failed' ? 'download failed: ' + f.error : (f.status + (pct !== null ? ' ' + pct + '%' : '')))));
        });
        if (!files.length && !state.models.length) {
          modelsTable.body.appendChild(h('tr', {}, h('td', { colspan: '7', class: 'empty' },
            'No models yet. Upload a .gguf, paste a download link, or run scripts\\get-model.ps1 on the PC.')));
          modelSel.appendChild(h('option', { value: '' }, 'no models'));
        }
        if (current && files.some(function (m) { return m.name === current; })) modelSel.value = current;
        if (!nameIn.value && modelSel.value) nameIn.value = slug(modelSel.value);
        var downloading = state.models.some(function (m) { return m.status === 'downloading'; });
        clearTimeout(state.dlTimer);
        if (downloading) state.dlTimer = setTimeout(loadModels, 2000);
      }

      function drawDevices() {
        var checked = {};
        var weights = {};
        var host = null;
        PC.qsa('input[data-hold]', devTable.body).forEach(function (el) { checked[el.getAttribute('data-hold')] = el.checked; });
        PC.qsa('input[data-weight]', devTable.body).forEach(function (el) { weights[el.getAttribute('data-weight')] = el.value; });
        var hostEl = PC.qs('input[name=llm-host]:checked', devTable.body);
        if (hostEl) host = hostEl.value;
        var first = !Object.keys(checked).length;
        PC.clear(devTable.body);
        var manual = mode() === 'manual';
        state.devices.forEach(function (d) {
          var hold = h('input', { type: 'checkbox', 'data-hold': d.id, disabled: !d.eligible, onchange: schedulePlan });
          hold.checked = first ? d.eligible : (checked[d.id] === undefined ? d.eligible : !!checked[d.id]);
          var hostRadio = h('input', { type: 'radio', name: 'llm-host', value: d.id, 'data-eligible': d.eligible ? '1' : '0',
            disabled: !d.eligible || !manual, onchange: schedulePlan });
          var weight = h('input', { class: 'input compact', type: 'number', min: '0', step: 'any', placeholder: 'auto',
            'data-weight': d.id, 'data-eligible': d.eligible ? '1' : '0', disabled: !d.eligible || !manual, oninput: schedulePlan });
          if (weights[d.id]) weight.value = weights[d.id];
          var gpu = '-';
          if (d.gpu) {
            gpu = h('span', { title: (d.gpu.name || 'GPU') + ': ' + d.gpu_pct + '% of its memory may be used (change it on the Devices page)' },
              (d.gpu_budget_mb ? mb(d.gpu_budget_mb) : 'not used') +
              (d.gpu.temp !== null && d.gpu.temp !== undefined ? ' · ' + d.gpu.temp + ' °C' : ''));
          }
          devTable.body.appendChild(h('tr', { class: d.eligible ? '' : 'muted' },
            h('td', {}, hold), h('td', {}, hostRadio),
            h('td', {}, d.name, h('div', { class: 'muted small' }, d.status)),
            h('td', {}, d.platform + (d.android_api ? ' (API ' + d.android_api + ')' : '')),
            h('td', { class: 'small' }, d.platform === 'browser' ? '-' : buildLabel(d.llama_build) + (d.gpu_build ? ' + GPU build' : '')),
            h('td', {}, mb(d.mem_free_mb)), h('td', {}, mb(d.usable_mb)),
            h('td', { class: 'small' }, gpu),
            h('td', {}, weight),
            h('td', { class: 'muted small' }, d.reason || '')));
          if (host === d.id) hostRadio.checked = true;
        });
        if (!PC.qs('input[name=llm-host]:checked', devTable.body)) {
          // default host: the strongest eligible device (usually the PC)
          var best = state.devices.filter(function (d) { return d.eligible; })
            .sort(function (a, b) { return (b.usable_mb || 0) - (a.usable_mb || 0); })[0];
          var radio = best && PC.qs('input[name=llm-host][value="' + best.id + '"]', devTable.body);
          if (radio) radio.checked = true;
        }
        if (!state.devices.length) {
          devTable.body.appendChild(h('tr', {}, h('td', { colspan: '10', class: 'empty' }, 'No devices connected.')));
        }
      }

      function selection() {
        var devices = PC.qsa('input[data-hold]', devTable.body).filter(function (el) { return el.checked; })
          .map(function (el) { return el.getAttribute('data-hold'); });
        var sel = { model: modelSel.value, devices: devices, mode: mode(), ctx: parseInt(ctxSel.value, 10) || 2048 };
        if (sel.mode === 'manual') {
          var weights = {};
          PC.qsa('input[data-weight]', devTable.body).forEach(function (el) {
            var v = parseFloat(el.value);
            if (!isNaN(v) && v > 0) weights[el.getAttribute('data-weight')] = v;
          });
          var hostEl = PC.qs('input[name=llm-host]:checked', devTable.body);
          sel.host = hostEl ? hostEl.value : null;
          sel.weights = weights;
        }
        return sel;
      }

      function partsText(parts) {
        return (parts || []).filter(function (p) { return p.layers; }).map(function (p) {
          return (p.kind === 'gpu' ? 'GPU ' : 'CPU ') + p.layers;
        }).join(' + ') || '-';
      }

      function schedulePlan() {
        clearTimeout(state.planTimer);
        state.planTimer = setTimeout(plan, 250);
      }

      function plan() {
        var sel = selection();
        PC.clear(planEl);
        forceBtn.hidden = true;
        if (!sel.model) { planEl.appendChild(h('p', { class: 'muted' }, 'Choose a model first.')); deployBtn.disabled = true; return; }
        if (!sel.devices.length) { planEl.appendChild(h('p', { class: 'muted' }, 'Tick at least one device.')); deployBtn.disabled = true; return; }
        PC.api.llmPlan(sel).then(function (p) {
          PC.clear(planEl);
          var t = table(['Order', 'Device', 'Layers', 'On', 'Needs', 'Usable', '']);
          p.devices.forEach(function (d, i) {
            var usable = (d.parts || []).reduce(function (s, x) { return s + (x.budget_mb || 0); }, 0);
            var pct = usable ? Math.min(100, Math.round(100 * d.need_mb / usable)) : 100;
            var role = d.role === 'host' ? ' (alone)' : d.role === 'shard + host' ? ' (host)' : '';
            t.body.appendChild(h('tr', {},
              h('td', {}, String(i + 1) + (p.placement === 'split' && i === p.devices.length - 1 ? ' (output layer)' : '')),
              h('td', {}, d.name + role),
              h('td', {}, d.layers === null || d.layers === undefined ? '-' : String(d.layers)),
              h('td', { class: 'small' }, partsText(d.parts)),
              h('td', {}, mb(d.need_mb)),
              h('td', {}, h('div', { class: 'progress-bar', title: pct + '% of what this device may use' },
                h('span', { style: 'width:' + pct + '%;' + (d.fits ? '' : 'background:var(--bad)') })), mb(usable)),
              h('td', {}, d.fits ? 'fits' : h('strong', { style: 'color:var(--bad)' }, 'too big'))));
          });
          if (p.summary) {
            planEl.appendChild(h('p', {}, h('strong', {}, p.summary.charAt(0).toUpperCase() + p.summary.slice(1)),
              p.tokens_per_second ? h('span', { class: 'muted small' }, ' · roughly ' + p.tokens_per_second + ' tokens/s') : null));
          }
          planEl.appendChild(h('p', { class: 'small muted' },
            'Model ' + mb(p.model_mb) + ', context ' + mb(p.kv_mb) + ', work buffer ' + mb(p.compute_mb) + ' per device' +
            (p.model.layers ? '; ' + p.model.layers + ' layers.' : '.')));
          if (p.devices.length) planEl.appendChild(t.el);
          (p.excluded || []).forEach(function (x) {
            planEl.appendChild(h('p', { class: 'muted small' }, x.name + ' left out: ' + x.reason));
          });
          (p.errors || []).forEach(function (e) { planEl.appendChild(h('p', { class: 'error' }, e)); });
          (p.warnings || []).forEach(function (w) { planEl.appendChild(h('p', { class: 'warn small' }, w)); });
          deployBtn.disabled = !!(p.errors && p.errors.length) || !p.ok;
          forceBtn.hidden = p.ok || !!(p.errors && p.errors.length);
        }).catch(function (err) {
          PC.clear(planEl);
          planEl.appendChild(h('p', { class: 'error' }, err.message));
          deployBtn.disabled = true;
        });
      }

      function deploy(force) {
        var sel = selection();
        sel.name = nameIn.value.trim();
        sel.threads = parseInt(threadsIn.value, 10) || 0;
        sel.force = !!force;
        deployBtn.disabled = true;
        PC.api.llmDeploy(sel).then(function (dep) {
          PC.toast('Starting ' + dep.name + ': ' + ((dep.options || {}).summary || ''), 'ok');
          loadDeployments();
        }).catch(PC.err).then(function () { deployBtn.disabled = false; });
      }

      function drawDeployments() {
        PC.clear(depsEl);
        if (!state.deployments.length) {
          depsEl.appendChild(h('p', { class: 'muted' }, 'Nothing running. Pick a model and devices above.'));
          return;
        }
        state.deployments.forEach(function (d) {
          var active = ['starting', 'loading', 'ready', 'degraded'].indexOf(d.status) >= 0;
          var opts = d.options || {};
          var keep = !d.idle_minutes;
          var keepBtn = h('button', {
            class: 'btn btn-sm', title: keep ? 'Unload it after the idle time set in Settings when nobody uses it' : 'Never unload it',
            onclick: function () { PC.api.llmKeep(d.id, !keep).then(loadDeployments).catch(PC.err); }
          }, keep ? 'Let it unload when unused' : 'Keep loaded');
          var note = keep ? 'kept loaded' : 'unloads after ' + d.idle_minutes + ' min unused, loads again on the next message';
          var t = table(['Device', 'Layers', 'On', 'Needs', 'Address']);
          (d.shards || []).forEach(function (s) {
            t.body.appendChild(h('tr', {},
              h('td', {}, (s.name || PC.shortId(s.device_id)) + (s.device_id === d.host_device ? ' (host)' : '')),
              h('td', {}, s.layers === null || s.layers === undefined ? '-' : String(s.layers)),
              h('td', { class: 'small' }, partsText(s.parts)),
              h('td', {}, mb(s.need_mb)),
              h('td', { class: 'mono small' }, s.endpoint || (active ? 'starting...' : '-'))));
          });
          depsEl.appendChild(h('div', { class: 'service-block' },
            h('div', { class: 'row gap wrap' },
              h('strong', {}, d.name), badge(d.status),
              h('span', { class: 'chip' }, d.mode === 'auto' ? 'automatic' : 'manual'),
              (d.copies || []).length ? h('span', { class: 'chip', title: 'extra copies on ' + d.copies.map(function (c) { return c.host_name; }).join(', ') +
                ' while callers wait; they stop when it is quiet' }, (1 + d.copies.length) + ' copies') : null,
              h('span', { class: 'muted small' }, d.model + ' - ' + (opts.summary || ('host ' + (d.host_name || PC.shortId(d.host_device)))) +
                ' - ctx ' + (opts.ctx || 2048) + ' - ' + note),
              h('span', { class: 'spacer' }),
              keepBtn,
              active ? h('button', { class: 'btn btn-sm', onclick: function () { PC.api.llmRestart(d.id).then(loadDeployments).catch(PC.err); } }, 'Restart')
                : h('button', { class: 'btn btn-sm', title: 'Load it now instead of on the next message', onclick: function () {
                  PC.api.llmRestart(d.id).then(loadDeployments).catch(PC.err); } }, 'Load now'),
              active ? h('button', { class: 'btn btn-sm btn-danger', onclick: function () {
                PC.confirm('Stop ' + d.name + '? Its devices unload the model (a message to it loads it again).').then(function (ok) {
                  if (ok) PC.api.llmStop(d.id).then(loadDeployments).catch(PC.err);
                });
              } }, 'Stop') : h('button', { class: 'btn btn-sm', onclick: function () { PC.api.llmDelete(d.id).then(loadDeployments).catch(PC.err); } }, 'Remove')),
            d.error ? h('p', { class: d.status === 'failed' ? 'error small' : 'warn small' }, d.error) : null,
            (d.shards || []).length ? t.el : null));
        });
      }

      function drawChatSelect() {
        var current = chatSel.value;
        PC.clear(chatSel);
        var names = [];
        state.deployments.filter(function (d) { return d.status === 'ready'; }).forEach(function (d) {
          names.push(d.name);
          chatSel.appendChild(h('option', { value: d.name }, d.name + ' (' + d.model + ')'));
        });
        // models that are not loaded: the first message loads them
        state.services.filter(function (s) { return s.on_demand && names.indexOf(s.name) < 0; }).forEach(function (s) {
          names.push(s.name);
          chatSel.appendChild(h('option', { value: s.name }, s.name + ' (loads on the first message)'));
        });
        if (!names.length) chatSel.appendChild(h('option', { value: '' }, 'no model yet'));
        if (current && names.indexOf(current) >= 0) chatSel.value = current;
        sendBtn.disabled = !names.length;
        drawChat();
      }

      function drawChat() {
        PC.clear(chatLog);
        (state.chats[chatSel.value] || []).forEach(function (m) {
          chatLog.appendChild(h('div', { class: 'chat-msg chat-' + m.role },
            h('div', { class: 'chat-role' }, m.role === 'user' ? 'you' : chatSel.value),
            h('div', { class: 'chat-text' }, m.content || (m.pending ? '...' : ''))));
        });
        chatLog.scrollTop = chatLog.scrollHeight;
      }

      function send() {
        var name = chatSel.value;
        var text = chatIn.value.trim();
        if (!name || !text) return;
        var history = state.chats[name] = state.chats[name] || [];
        history.push({ role: 'user', content: text });
        var reply = { role: 'assistant', content: '', pending: true };
        history.push(reply);
        chatIn.value = '';
        drawChat();
        var rid = newId();
        state.activeChat = { id: rid, reply: reply };
        var body = {
          messages: history.filter(function (m) { return !m.pending; }).map(function (m) { return { role: m.role, content: m.content }; }),
          max_tokens: parseInt(maxTok.value, 10) || 256,
          temperature: parseFloat(temp.value) || 0
        };
        sendBtn.disabled = true;
        chatStatus.textContent = 'thinking...';
        var blob = new Blob([JSON.stringify(body)], { type: 'application/json' });
        PC.api.callService(name, blob, null, rid).then(function (res) {
          reply.pending = false;
          reply.content = (res && res.result && res.result.text) || reply.content;
          var r = (res && res.result) || {};
          chatStatus.textContent = (r.tokens ? r.tokens + ' tokens, ' : '') + (r.tokens_per_second ? r.tokens_per_second + ' tok/s, ' : '') +
            (res && res.ms ? (res.ms / 1000).toFixed(1) + ' s' : '');
        }).catch(function (err) {
          reply.pending = false;
          reply.content = reply.content || ('(error: ' + err.message + ')');
          chatStatus.textContent = '';
        }).then(function () {
          state.activeChat = null;
          sendBtn.disabled = false;
          drawChat();
        });
      }

      function refreshDevicesSoon() {
        // devices publish on every heartbeat: refresh at most every 10 s, and
        // never while the user is typing a share weight
        if (state.devTimer) return;
        state.devTimer = setTimeout(function () {
          state.devTimer = null;
          var focused = document.activeElement;
          if (focused && devTable.el.contains(focused)) return;
          loadDevices().then(schedulePlan).catch(function () { });
        }, 10000);
      }

      var offs = [
        PC.bus.on('llm', function () { loadDeployments().catch(function () { }); }),
        PC.bus.on('request', function (m) {
          var active = state.activeChat;
          if (!active || !m.request || m.request.id !== active.id || !m.partial) return;
          if (m.partial.status) chatStatus.textContent = m.partial.status + '...';   // e.g. loading the model
          active.reply.content += m.partial.token || '';
          drawChat();
        }),
        PC.bus.on('devices', refreshDevicesSoon)
      ];

      modeChanged();
      loadAll();
      return function () {
        clearTimeout(state.planTimer);
        clearTimeout(state.dlTimer);
        clearTimeout(state.devTimer);
        offs.forEach(function (f) { f(); });
      };
    }
  };
})();
