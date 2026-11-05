/* PhoneCluster dashboard - Services view (SPEC 6.7): list running model
   services, call one with a file or text, watch partial results live. */
(function () {
  'use strict';
  var PC = (window.PC = window.PC || {});
  var views = PC.views;
  var h = PC.h;
  var page = PC.page, table = PC.table;

  function fmtMs(ms) { return ms === null || ms === undefined ? '' : (ms >= 1000 ? (ms / 1000).toFixed(1) + ' s' : ms + ' ms'); }

  function newRequestId() {
    var s = '';
    for (var i = 0; i < 12; i++) { s += Math.floor(Math.random() * 16).toString(16); }
    return 'd' + s;
  }

  function previewFor(output) {
    var name = String(output.name || '').toLowerCase();
    var url = '/api/files/' + encodeURIComponent(output.file_id) + '/download';
    if (/\.(wav|mp3|ogg|m4a|flac)$/.test(name)) {
      // audio needs the PIN cookie (set at login) since <audio> cannot send headers
      return h('audio', { controls: true, src: url, class: 'preview-audio' });
    }
    if (/\.(png|jpe?g|gif|webp|bmp)$/.test(name)) {
      return h('img', { src: url, class: 'preview-image', alt: output.name });
    }
    return null;
  }

  views.services = {
    title: 'Services',
    render: function (root) {
      var services = [];
      var activeId = null;          // request currently shown in the call panel
      var partialsEl, resultEl, outputsEl, statusEl, progressEl, callBtn, cancelBtn;

      root.appendChild(page('Services', [
        h('button', { class: 'btn btn-primary', onclick: function () { PC.newJobDialog({ type: 'service' }); } }, 'Start service'),
        h('button', { class: 'btn', onclick: load }, 'Refresh')
      ]));
      root.appendChild(h('p', { class: 'muted' },
        'A service is a job that keeps a model loaded on one device (speech-to-text, vision, translation, TTS, ...). ',
        'Any device or this page can send it requests; the input streams to the model and the answer comes back here.'));

      /* ---- running services ---- */
      var listCard = h('div', { class: 'card' });
      root.appendChild(listCard);

      /* ---- call panel ---- */
      var svcSel = h('select', { class: 'input' });
      var modeSel = h('select', { class: 'input compact', onchange: syncMode },
        h('option', { value: 'file' }, 'file'), h('option', { value: 'text' }, 'text'));
      var picker = h('input', { type: 'file', class: 'input' });
      var textIn = h('textarea', { class: 'input mono', rows: '4', placeholder: 'text to send' });
      var paramsIn = h('input', { class: 'input mono', placeholder: '{"lang": "en"}  (optional params JSON)' });
      callBtn = h('button', { class: 'btn btn-primary', onclick: call }, 'Call');
      cancelBtn = h('button', { class: 'btn', onclick: cancelActive, disabled: true }, 'Cancel');
      statusEl = h('div', { class: 'muted small' }, 'Pick a service, choose a file or type text, then Call.');
      progressEl = h('div', { class: 'progress-bar' }, h('span', { style: 'width:0%' }));
      partialsEl = h('pre', { class: 'log partials', hidden: true });
      resultEl = h('pre', { class: 'json-view', hidden: true });
      outputsEl = h('div', { class: 'outputs' });
      textIn.hidden = true;
      root.appendChild(h('div', { class: 'card' },
        h('h3', {}, 'Call a service'),
        h('div', { class: 'form-grid' },
          h('label', {}, 'service', svcSel),
          h('label', {}, 'input', h('div', { class: 'row gap' }, modeSel, picker, textIn)),
          h('label', {}, 'params', paramsIn)),
        h('div', { class: 'row gap' }, callBtn, cancelBtn, statusEl),
        progressEl, partialsEl, resultEl, outputsEl));

      /* ---- recent requests ---- */
      var recent = table(['Request', 'Service', 'Status', 'Device', 'Bytes', 'Time', 'Error']);
      root.appendChild(h('div', { class: 'card' }, h('h3', {}, 'Recent requests'), recent.el));

      function syncMode() {
        var file = modeSel.value === 'file';
        picker.hidden = !file;
        textIn.hidden = file;
      }

      function load() {
        PC.api.services().then(function (list) {
          services = list;
          drawServices();
        }).catch(PC.err);
        PC.api.serviceRequests(30).then(drawRecent).catch(function () { });
      }

      function drawServices() {
        PC.clear(listCard);
        var current = svcSel.value;
        PC.clear(svcSel);
        if (!services.length) {
          listCard.appendChild(h('p', { class: 'muted' }, 'No service is running. Start one (job type "service") or run a package from examples/packages/services.'));
          svcSel.appendChild(h('option', { value: '' }, 'no services'));
          return;
        }
        services.forEach(function (s) {
          svcSel.appendChild(h('option', { value: s.name }, s.name + (s.description ? ' - ' + s.description : '')));
          if (s.on_demand) {
            // a model that is not loaded: the first call starts it (SPEC 6.4d)
            listCard.appendChild(h('div', { class: 'service-block' },
              h('div', { class: 'row gap wrap' },
                h('strong', {}, s.name), h('span', { class: 'chip' }, 'loads on first call'),
                h('span', { class: 'muted' }, s.description || ''))));
            return;
          }
          var t = table(['Device', 'Status', 'Requests', 'Avg', 'Since']);
          (s.instances || []).forEach(function (inst) {
            t.body.appendChild(h('tr', {},
              h('td', {}, inst.device_name || PC.shortId(inst.device_id)),
              h('td', {}, h('span', { class: 'badge badge-' + (inst.status === 'ready' ? 'done' : 'running') }, inst.status)),
              h('td', {}, String(inst.requests_done || 0)),
              h('td', {}, fmtMs(inst.avg_ms)),
              h('td', {}, PC.relTime ? PC.relTime(inst.since) : '')));
          });
          listCard.appendChild(h('div', { class: 'service-block' },
            h('div', { class: 'row gap wrap' },
              h('strong', {}, s.name),
              (s.instances || []).length > 1 ? h('span', { class: 'chip', title: 'extra copies start when callers wait and stop when it is quiet' },
                s.instances.length + ' copies') : null,
              h('span', { class: 'muted' }, s.description || ''),
              h('span', { class: 'muted small' }, [s.accepts, s.produces].filter(Boolean).join(' -> ')),
              h('span', { class: 'spacer' }),
              h('button', { class: 'btn btn-danger btn-sm', onclick: function () { stopService(s); } }, 'Stop')),
            t.el));
        });
        if (current) svcSel.value = current;
      }

      function stopService(s) {
        PC.confirm('Stop service ' + s.name + ' on every device?').then(function (ok) {
          if (!ok) return;
          var jobs = {};
          (s.instances || []).forEach(function (i) { if (i.job_id) jobs[i.job_id] = true; });
          Promise.all(Object.keys(jobs).map(function (id) { return PC.api.cancelJob(id); }))
            .then(function () { PC.toast('Stopped ' + s.name, 'ok'); load(); }).catch(PC.err);
        });
      }

      function drawRecent(items) {
        PC.clear(recent.body);
        (items || []).forEach(function (r) {
          recent.body.appendChild(h('tr', { 'data-id': r.id },
            h('td', { class: 'mono' }, PC.shortId(r.id)),
            h('td', {}, r.service || ''),
            h('td', {}, h('span', { class: 'badge badge-' + (r.status || 'queued') }, r.status || '')),
            h('td', {}, r.device_id ? PC.shortId(r.device_id) : ''),
            h('td', {}, String(r.bytes_in || 0)),
            h('td', {}, fmtMs(r.ms)),
            h('td', { class: 'muted small' }, r.error || '')));
        });
      }

      function setProgress(fraction) {
        progressEl.firstChild.style.width = Math.round(Math.max(0, Math.min(1, fraction)) * 100) + '%';
      }

      function call() {
        var name = svcSel.value;
        if (!name) { PC.toast('No service selected', 'error'); return; }
        var params = {};
        if (paramsIn.value.trim()) {
          try { params = JSON.parse(paramsIn.value); } catch (e) { PC.toast('params must be JSON', 'error'); return; }
        }
        var input;
        if (modeSel.value === 'file') {
          input = picker.files && picker.files[0];
          if (!input) { PC.toast('Choose a file first', 'error'); return; }
        } else {
          input = textIn.value;
          if (!input) { PC.toast('Type something first', 'error'); return; }
        }
        activeId = newRequestId();
        PC.clear(partialsEl); partialsEl.hidden = false;
        PC.clear(resultEl); resultEl.hidden = true;
        PC.clear(outputsEl);
        setProgress(0);
        statusEl.textContent = 'uploading to ' + name + ' ...';
        callBtn.disabled = true;
        cancelBtn.disabled = false;
        PC.api.callService(name, input, params, activeId, function (f) {
          setProgress(f);
          if (f >= 1) statusEl.textContent = 'waiting for ' + name + ' ...';
        }).then(function (res) {
          showResult(res);
        }).catch(function (err) {
          statusEl.textContent = 'failed: ' + err.message;
          PC.toast(err.message, 'error');
        }).then(function () {
          callBtn.disabled = false;
          cancelBtn.disabled = true;
          load();
        });
      }

      function cancelActive() {
        if (!activeId) return;
        PC.api.cancelRequest(activeId).catch(PC.err);
      }

      function appendPartial(value) {
        partialsEl.hidden = false;
        partialsEl.appendChild(document.createTextNode((typeof value === 'string' ? value : JSON.stringify(value)) + '\n'));
        partialsEl.scrollTop = partialsEl.scrollHeight;
      }

      function showResult(res) {
        if (!res) return;
        statusEl.textContent = (res.status === 'done' ? 'done' : (res.status || '')) + (res.ms ? ' in ' + fmtMs(res.ms) : '')
          + (res.device_id ? ' on ' + PC.shortId(res.device_id) : '');
        if (res.partials && !partialsEl.childNodes.length) { res.partials.forEach(appendPartial); }
        resultEl.hidden = false;
        resultEl.textContent = typeof res.result === 'string' ? res.result : JSON.stringify(res.result, null, 2);
        PC.clear(outputsEl);
        (res.outputs || []).forEach(function (o) {
          var row = h('div', { class: 'output-row' },
            h('button', { class: 'btn btn-sm', title: 'Save into data\\downloads', onclick: function () { PC.api.save('/files/' + encodeURIComponent(o.file_id) + '/save', o.name); } }, 'Save'),
            h('span', {}, o.name + (o.size ? ' (' + PC.fmtBytes(o.size) + ')' : '')));
          var preview = previewFor(o);
          if (preview) row.appendChild(preview);
          outputsEl.appendChild(row);
        });
      }

      var offs = [
        PC.bus.on('services', function (m) { services = PC.asList(m.services || m, 'services'); drawServices(); }),
        PC.bus.on('request', function (m) {
          var r = m.request || {};
          if (activeId && r.id === activeId) {
            if (m.partial !== undefined) appendPartial(m.partial);
            if (r.status === 'running') statusEl.textContent = 'running on ' + PC.shortId(r.device_id || '') + ' ...';
          }
          PC.api.serviceRequests(30).then(drawRecent).catch(function () { });
        })
      ];

      load();
      return function () { offs.forEach(function (f) { f(); }); };
    }
  };
})();
