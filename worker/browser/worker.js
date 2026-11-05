/* PhoneCluster - browser worker (SPEC 15).
 *
 * Registers with platform "browser" and caps ["js","browser"], long-polls
 * /api/w/poll and runs js impls inside runner.js.
 *
 * ES2017 only: no modules, no optional chaining, no nullish coalescing, no
 * top-level await - the Samsung J2 runs an old Chrome.
 */
(function () {
'use strict';

var STORE_KEY = 'phonecluster.device';
var RUNNER_URL = '/w/runner.js';
var MAX_LOG_LINES = 10000;
var UI_LOG_LINES = 200;

var state = {
  base: location.origin,
  device_id: null,
  token: null,
  name: '',
  slots: 1,
  paused: false,
  stopped: false,
  polling: false,
  replaying: false,
  rejoinTried: false,
  svcTimer: null,
  battery: null,
  charging: null,
  tasksDone: 0,
  score: null,
  pollTimeout: 25,
  logBatchMs: 500,
  current: null,
  /* Python via Pyodide (SPEC 15.1): served by the coordinator at /w/pyodide/ */
  python: { wanted: false, version: null, ready: false, worker: null, loading: null, run: null, failed: null }
};

/* ------------------------------------------------------------------ dom */

function $(id) { return document.getElementById(id); }

var ui = {};
var logBuffer = [];

function setState(text, cls) {
  ui.state.textContent = text;
  ui.dot.className = 'dot ' + (cls ? cls : '');
  if (ui.statusValue) { ui.statusValue.textContent = text; }
}

function show(card) {
  ui.joinCard.hidden = card !== 'join';
  ui.mainCard.hidden = card !== 'main';
  ui.goneCard.hidden = card !== 'gone';
}

function uiLog(line) {
  logBuffer.push(line);
  if (logBuffer.length > UI_LOG_LINES) {
    logBuffer = logBuffer.slice(logBuffer.length - UI_LOG_LINES);
  }
  ui.logView.textContent = logBuffer.join('\n');
  ui.logView.scrollTop = ui.logView.scrollHeight;
}

function stamp(line) {
  var d = new Date();
  var hh = ('0' + d.getHours()).slice(-2);
  var mm = ('0' + d.getMinutes()).slice(-2);
  var ss = ('0' + d.getSeconds()).slice(-2);
  uiLog(hh + ':' + mm + ':' + ss + ' ' + line);
}

function toast(title, body) {
  ui.toast.innerHTML = '';
  var strong = document.createElement('strong');
  strong.textContent = title;
  ui.toast.appendChild(strong);
  ui.toast.appendChild(document.createTextNode(body ? body : ''));
  ui.toast.hidden = false;
  if (toast._timer) { clearTimeout(toast._timer); }
  toast._timer = setTimeout(function () { ui.toast.hidden = true; }, 6000);
}

function renderStatus() {
  ui.deviceName.textContent = state.name;
  ui.doneValue.textContent = String(state.tasksDone);
  ui.scoreValue.textContent = state.score === null ? '-' : state.score.toFixed(2);
  ui.pauseBtn.textContent = state.paused ? 'Resume' : 'Pause';
  if (state.battery === null) {
    ui.batteryValue.textContent = '-';
  } else {
    ui.batteryValue.textContent = state.battery + '%' + (state.charging ? ' +' : '');
  }
  if (state.current) {
    ui.taskValue.textContent = state.current.id;
  } else {
    ui.taskValue.textContent = 'idle';
  }
}

function setProgress(fraction) {
  var pct = Math.max(0, Math.min(1, Number(fraction) || 0)) * 100;
  ui.progressBar.style.width = pct + '%';
}

/* ----------------------------------------------------------------- util */

function sleep(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

function loadDevice() {
  try {
    var raw = localStorage.getItem(STORE_KEY);
    if (!raw) { return null; }
    return JSON.parse(raw);
  } catch (err) {
    return null;
  }
}

function saveDevice() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      device_id: state.device_id, token: state.token, name: state.name
    }));
  } catch (err) { /* private mode: keep running in memory */ }
}

function clearDevice() {
  try { localStorage.removeItem(STORE_KEY); } catch (err) { /* ignore */ }
}

/* --------------------------------------------------------------- outbox */
/* Results that could not be delivered because the coordinator was
   unreachable (SPEC 18).  Kept in localStorage and replayed after the next
   successful poll.  Output files are kept as base64 while they fit in
   OUTBOX_MAX_BYTES; bigger ones are dropped with a note in the task log. */

var OUTBOX_KEY = 'pc_outbox';
var OUTBOX_MAX_BYTES = 1024 * 1024;
var OUTBOX_MAX_AGE = 7 * 24 * 3600 * 1000;

function isTransportError(err) {
  return !err || err.status === undefined || err.status === null;
}

function outboxLoad() {
  try {
    var raw = localStorage.getItem(OUTBOX_KEY);
    var list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list : [];
  } catch (err) {
    return [];
  }
}

function outboxSave(list) {
  try {
    if (list.length) { localStorage.setItem(OUTBOX_KEY, JSON.stringify(list)); }
    else { localStorage.removeItem(OUTBOX_KEY); }
    return true;
  } catch (err) {
    stamp('outbox: could not save (' + err.message + ')');
    return false;
  }
}

function outboxCount() { return outboxLoad().length; }

function bufferToBase64(buffer) {
  var bytes = new Uint8Array(buffer);
  var chunks = [];
  for (var i = 0; i < bytes.length; i += 0x8000) {
    chunks.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000)));
  }
  return btoa(chunks.join(''));
}

function base64ToBuffer(text) {
  var bin = atob(text);
  var bytes = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) { bytes[i] = bin.charCodeAt(i); }
  return bytes.buffer;
}

function outboxAdd(taskId, result, pendingOutputs) {
  var list = outboxLoad().filter(function (e) { return e.task_id !== taskId; });
  var kept = [];
  var budget = OUTBOX_MAX_BYTES;
  (pendingOutputs || []).forEach(function (item) {
    var size = item.data ? item.data.byteLength : 0;
    if (size > budget) {
      stamp('outbox: output ' + item.name + ' (' + size + ' bytes) too large to keep');
      return;
    }
    budget -= size;
    kept.push({ name: item.name, b64: bufferToBase64(item.data) });
  });
  list.push({ task_id: taskId, result: result, pending: kept, created: Date.now() });
  if (outboxSave(list)) {
    stamp('outbox: kept result of task ' + taskId + ' (' + kept.length + ' pending file'
          + (kept.length === 1 ? '' : 's') + ')');
  }
}

function outboxRemove(taskId) {
  outboxSave(outboxLoad().filter(function (e) { return e.task_id !== taskId; }));
}

function outboxReplay() {
  if (state.replaying) { return Promise.resolve(); }
  state.replaying = true;
  var entries = outboxLoad();

  function next(index) {
    if (index >= entries.length) { return Promise.resolve(); }
    var entry = entries[index];
    var taskId = entry.task_id;
    if (Date.now() - (entry.created || 0) > OUTBOX_MAX_AGE) {
      stamp('outbox: dropping stale result of task ' + taskId);
      outboxRemove(taskId);
      return next(index + 1);
    }
    var result = entry.result || {};
    var outputs = result.outputs ? result.outputs.slice() : [];
    var pending = entry.pending ? entry.pending.slice() : [];

    function upload(i) {
      if (i >= pending.length) { return Promise.resolve(true); }
      var item = pending[i];
      return putFile(item.name, base64ToBuffer(item.b64)).then(function (meta) {
        outputs.push({ file_id: meta.file_id, name: meta.name ? meta.name : item.name, size: meta.size });
        return upload(i + 1);
      }).catch(function (err) {
        if (isTransportError(err)) {
          // Save progress so uploaded files are not sent twice.
          result.outputs = outputs;
          entry.pending = pending.slice(i);
          outboxSave(outboxLoad().map(function (e) { return e.task_id === taskId ? entry : e; }));
          return false;
        }
        stamp('outbox: upload of ' + item.name + ' rejected, skipping it');
        return upload(i + 1);
      });
    }

    return upload(0).then(function (reachable) {
      if (!reachable) { return; }
      result.outputs = outputs;
      return api('POST', '/api/w/task/' + encodeURIComponent(taskId) + '/done', result)
        .then(function () {
          stamp('outbox: delivered result of task ' + taskId);
          outboxRemove(taskId);
          return next(index + 1);
        }).catch(function (err) {
          if (err.status === 410) { onRemoved(); return; }
          if (isTransportError(err)) {
            entry.pending = [];
            outboxSave(outboxLoad().map(function (e) { return e.task_id === taskId ? entry : e; }));
            return;
          }
          stamp('outbox: result of task ' + taskId + ' no longer wanted (HTTP ' + err.status + ')');
          outboxRemove(taskId);
          return next(index + 1);
        });
    });
  }

  return next(0).catch(function (err) {
    stamp('outbox: replay failed (' + (err && err.message ? err.message : err) + ')');
  }).then(function () { state.replaying = false; });
}

function query(key) {
  var match = new RegExp('[?&]' + key + '=([^&]*)').exec(location.search);
  return match ? decodeURIComponent(match[1].replace(/\+/g, ' ')) : null;
}

function guessName() {
  var ua = navigator.userAgent;
  var model = /\(([^)]*)\)/.exec(ua);
  var inner = model ? model[1] : '';
  var parts = inner.split(';');
  var candidate = parts.length > 2 ? parts[2] : (parts.length ? parts[0] : '');
  candidate = candidate.replace(/Build\/.*$/, '').trim();
  if (!candidate) { candidate = 'browser'; }
  if (candidate.length > 28) { candidate = candidate.slice(0, 28); }
  return candidate + ' (web)';
}

function deviceInfo() {
  var memory = null;
  if (typeof navigator.deviceMemory === 'number') {
    memory = Math.round(navigator.deviceMemory * 1024);
  }
  return {
    cpu_count: navigator.hardwareConcurrency ? navigator.hardwareConcurrency : 1,
    mem_mb: memory,
    os: navigator.platform ? String(navigator.platform) : 'browser',
    python: null,
    model: navigator.userAgent ? String(navigator.userAgent).slice(0, 180) : 'browser',
    shell: null
  };
}

/* ------------------------------------------------------------------ api */

function ApiError(status, message) {
  this.name = 'ApiError';
  this.status = status;
  this.message = message;
}
ApiError.prototype = Object.create(Error.prototype);

function headers(extra) {
  var out = {};
  if (state.token) { out['X-Device-Token'] = state.token; }
  if (extra) {
    var keys = Object.keys(extra);
    for (var i = 0; i < keys.length; i++) { out[keys[i]] = extra[keys[i]]; }
  }
  return out;
}

function api(method, path, body) {
  var init = { method: method, cache: 'no-store', credentials: 'same-origin' };
  init.headers = headers(body === undefined || body === null
    ? null : { 'Content-Type': 'application/json' });
  if (body !== undefined && body !== null) { init.body = JSON.stringify(body); }
  return fetch(state.base + path, init).then(function (res) {
    if (res.ok) {
      return res.text().then(function (text) {
        if (!text) { return {}; }
        try { return JSON.parse(text); } catch (err) { return {}; }
      });
    }
    return res.text().then(function (text) {
      throw new ApiError(res.status, text ? text.slice(0, 300) : ('HTTP ' + res.status));
    });
  });
}

function getBuffer(path, range) {
  var extra = null;
  if (range) { extra = { Range: 'bytes=' + range[0] + '-' + range[1] }; }
  return fetch(state.base + path, { headers: headers(extra), cache: 'no-store' })
    .then(function (res) {
      if (!res.ok) { throw new ApiError(res.status, 'download failed'); }
      return res.arrayBuffer().then(function (buf) {
        if (range && res.status !== 206) {
          // coordinator ignored the range: slice locally
          return buf.slice(range[0], range[1] + 1);
        }
        return buf;
      });
    });
}

/* An input is a file id, or {file_id, offset, length} for a slice of a big file (SPEC 6.6). */
function fetchInput(entry) {
  if (entry && typeof entry === 'object') {
    var offset = Number(entry.offset) || 0;
    var length = Number(entry.length) || 0;
    return getBuffer('/api/w/files/' + encodeURIComponent(entry.file_id),
                     [offset, offset + Math.max(0, length - 1)]);
  }
  return getBuffer('/api/w/files/' + encodeURIComponent(entry));
}

function putFile(name, buffer) {
  return fetch(state.base + '/api/w/files?name=' + encodeURIComponent(name), {
    method: 'PUT',
    headers: headers({ 'Content-Type': 'application/octet-stream' }),
    body: buffer
  }).then(function (res) {
    if (!res.ok) { throw new ApiError(res.status, 'upload failed'); }
    return res.json();
  });
}

/* --------------------------------------------------------- registration */

/* A new device joins with a one-time code from the PC's Join page (SPEC 3);
   afterwards it re-attaches with its saved device id + token. */
function register(code, renamed) {
  var body = {
    join_code: code ? code : null,
    name: state.name,
    platform: 'browser',
    caps: capsList(),
    info: deviceInfo(),
    slots: state.slots,
    rename: !!renamed  /* the person renamed it here: this name wins over the dashboard's */
  };
  if (state.device_id && state.token) {
    body.device_id = state.device_id;
    body.token = state.token;
  }
  return api('POST', '/api/w/register', body).then(function (res) {
    state.device_id = res.device_id ? res.device_id : state.device_id;
    if (res.token) { state.token = res.token; }
    var config = res.config ? res.config : {};
    if (config.poll_timeout) { state.pollTimeout = config.poll_timeout; }
    if (config.log_batch_ms) { state.logBatchMs = config.log_batch_ms; }
    if (res.name) { state.name = res.name; }  /* a name given on the dashboard wins */
    saveDevice();
    stamp('registered as ' + state.name);
    if (!res.bench_done && !register._benched) {
      register._benched = true;
      runBenchmark();
    }
    return res;
  });
}

/* --------------------------------------------------------------- python */
/* Browser Python (SPEC 15.1): when the coordinator serves Pyodide, this
   worker also advertises the `python` capability and runs python impls in a
   persistent Web Worker (pyrunner.js).  Pure stdlib Python only, and slower
   than a real phone Python, but it lets a browser-only phone join map jobs
   that have no JS twin. */

var PYRUNNER_URL = '/w/pyrunner.js';

function capsList() {
  var caps = ['js', 'browser'];
  if (state.python.wanted) { caps.push('python'); }
  if (typeof WebAssembly === 'object' && typeof Worker !== 'undefined' && query('wasm') !== '0') {
    caps.push('wasm');  /* the same .wasm the PC runs (SPEC 6.9) */
  }
  return caps;
}

function renderPython() {
  if (!ui.pythonValue) { return; }
  var p = state.python;
  var text = 'off';
  if (p.failed) { text = 'unavailable (' + p.failed + ')'; }
  else if (p.ready) { text = 'ready (Pyodide ' + (p.version || '?') + ')'; }
  else if (p.loading) { text = 'loading ...'; }
  else if (p.wanted) { text = 'available (loads on first task)'; }
  ui.pythonValue.textContent = text;
}

function detectPython() {
  /* Decide whether to offer Python: coordinator must serve Pyodide, the
     browser must have WebAssembly, and ?python=0 turns it off. */
  if (query('python') === '0' || typeof WebAssembly === 'undefined' || typeof Worker === 'undefined') {
    state.python.wanted = false;
    return Promise.resolve(false);
  }
  return api('GET', '/api/info').then(function (info) {
    state.python.wanted = !!(info && info.pyodide);
    state.python.version = info && info.pyodide ? String(info.pyodide) : null;
    renderPython();
    return state.python.wanted;
  }).catch(function () {
    state.python.wanted = false;
    return false;
  });
}

function killPython(reason) {
  var p = state.python;
  if (p.worker) {
    try { p.worker.terminate(); } catch (err) { /* ignore */ }
  }
  p.worker = null;
  p.ready = false;
  p.loading = null;
  p.run = null;
  if (reason) { stamp('python runtime stopped (' + reason + ')'); }
  renderPython();
}

function pythonUnavailable(message) {
  /* Loading failed (old browser, out of memory, ...): stop offering Python. */
  var p = state.python;
  p.failed = message;
  p.wanted = false;
  killPython(null);
  stamp('python unavailable: ' + message);
  renderPython();
  if (state.token && !state.stopped) {
    register(null).catch(function () { /* the next poll re-registers */ });
  }
}

function ensurePython() {
  var p = state.python;
  if (p.ready && p.worker) { return Promise.resolve(p.worker); }
  if (p.loading) { return p.loading; }
  if (!p.wanted) { return Promise.reject(new Error('python runtime not offered')); }
  renderPython();
  p.loading = new Promise(function (resolve, reject) {
    var worker;
    try {
      worker = new Worker(PYRUNNER_URL);
    } catch (err) {
      reject(err);
      return;
    }
    var started = Date.now();
    stamp('loading python runtime (Pyodide) ...');
    worker.onerror = function (event) {
      reject(new Error(event.message ? String(event.message) : 'worker error'));
    };
    worker.onmessage = function (event) {
      var msg = event.data ? event.data : {};
      if (msg.type === 'ready') {
        p.worker = worker;
        p.ready = true;
        p.loading = null;
        p.version = msg.version || p.version;
        stamp('python ready in ' + Math.round((Date.now() - started) / 100) / 10 + 's (Pyodide ' + p.version + ')');
        renderPython();
        worker.onmessage = function (ev) {
          if (p.run) { onRunnerMessage(p.run, ev.data); }
        };
        worker.onerror = function (ev) {
          if (p.run) {
            finishTask(p.run, { ok: false, error: ev.message ? String(ev.message) : 'python worker error', traceback: '' });
          }
          killPython('worker error');
        };
        resolve(worker);
        return;
      }
      if (msg.type === 'init_error') {
        reject(new Error(msg.error || 'init failed'));
        return;
      }
      if (msg.type === 'log') { uiLog('[python] ' + msg.line); }
    };
    worker.postMessage({ type: 'init', indexURL: state.base + '/w/pyodide/' });
  }).catch(function (err) {
    p.loading = null;
    pythonUnavailable(err && err.message ? err.message : String(err));
    throw err;
  });
  return p.loading;
}

function warmPython() {
  if (!state.python.wanted || state.python.ready || state.python.loading) { return; }
  ensurePython().catch(function () { /* already reported */ });
}

function inputKey(entry) {
  /* mirrors worker.py input_key() so ctx.input(<id>) works the same */
  if (entry && typeof entry === 'object') {
    return String(entry.file_id) + ':' + String(entry.offset || 0) + '+' + String(entry.length || 0);
  }
  return String(entry);
}

function startPythonTask(run, payload) {
  var impl = payload.impls.python;
  run.python = true;
  api('POST', '/api/w/task/' + encodeURIComponent(run.id) + '/start', {})
    .catch(function () { /* best effort */ });
  var inputIds = payload.inputs ? payload.inputs : [];
  Promise.all(inputIds.map(fetchInput)).then(function (buffers) {
    if (run.cancelled || run.finished) { return null; }
    return ensurePython().then(function (worker) {
      if (run.cancelled || run.finished) { return; }
      var timeout = Number(payload.timeout) > 0 ? Number(payload.timeout) : 600;
      run.timer = setTimeout(function () {
        finishTask(run, { ok: false, error: 'timeout after ' + timeout + 's', traceback: '' });
      }, timeout * 1000);
      state.python.run = run;
      worker.postMessage({
        type: 'run',
        code: impl.code,
        entry: impl.entry ? impl.entry : 'run',
        args: impl.args ? impl.args : {},
        inputs: buffers,
        input_names: inputIds.map(inputKey),
        task_id: run.id,
        device_name: state.name,
        checkpoint: run.task && run.task.checkpoint !== undefined ? run.task.checkpoint : null
      }, buffers);
    });
  }).catch(function (err) {
    finishTask(run, {
      ok: false,
      error: 'python task could not start: ' + (err && err.message ? err.message : String(err)),
      traceback: ''
    });
  });
}

/* ---------------------------------------------------------- WebAssembly */
/* SPEC 6.9: wasm impls run in a fresh runner Web Worker (runner.js runWasm):
   the module and its inputs are downloaded, the worker is terminated on
   timeout or cancel like a js task. */

function startWasmTask(run, payload) {
  var impl = payload.impls.wasm;
  run.impl = 'wasm';
  api('POST', '/api/w/task/' + encodeURIComponent(run.id) + '/start', {})
    .catch(function () { /* best effort */ });
  var inputIds = payload.inputs ? payload.inputs : [];
  Promise.all(inputIds.map(fetchInput)).then(function (buffers) {
    if (run.cancelled || run.finished) { return; }
    var runner = new Worker(RUNNER_URL);
    run.worker = runner;
    runner.onmessage = function (event) { onRunnerMessage(run, event.data); };
    runner.onerror = function (event) {
      finishTask(run, { ok: false, error: event.message ? String(event.message) : 'worker error', traceback: '' });
    };
    var timeout = Number(payload.timeout) > 0 ? Number(payload.timeout) : 600;
    run.timer = setTimeout(function () {
      finishTask(run, { ok: false, error: 'timeout after ' + timeout + 's', traceback: '' });
    }, timeout * 1000);
    runner.postMessage({ type: 'wasm', impl: impl, inputs: buffers, task_id: run.id }, buffers);
  }).catch(function (err) {
    finishTask(run, {
      ok: false,
      error: 'input download failed: ' + (err && err.message ? err.message : String(err)),
      traceback: ''
    });
  });
}

/* ---------------------------------------------------------------- tasks */

function LogStream(taskId) {
  this.taskId = taskId;
  this.lines = [];
  this.count = 0;
  this.truncated = false;
  var self_ = this;
  this.timer = setInterval(function () { self_.flush(); }, state.logBatchMs);
}

LogStream.prototype.add = function (line) {
  if (this.truncated) { return; }
  if (this.count >= MAX_LOG_LINES) {
    this.truncated = true;
    this.lines.push('... truncated (log line cap reached)');
    return;
  }
  this.count += 1;
  this.lines.push(line);
  if (this.lines.length > 400) { this.flush(); }
};

LogStream.prototype.flush = function () {
  if (!this.lines.length) { return Promise.resolve(); }
  var batch = this.lines;
  this.lines = [];
  return api('POST', '/api/w/task/' + encodeURIComponent(this.taskId) + '/log',
    { lines: batch, ts: Date.now() / 1000 }).catch(function () { /* best effort */ });
};

LogStream.prototype.close = function () {
  clearInterval(this.timer);
  return this.flush();
};

function startTask(task) {
  if (state.current) {
    stamp('busy, ignoring task ' + task.id);
    return;
  }
  var payload = task.payload ? task.payload : {};
  var impls = payload.impls ? payload.impls : {};
  var run = {
    id: String(task.id),
    task: task,
    python: false,
    attempt: task.attempt !== undefined ? task.attempt : null,
    started: Date.now(),
    worker: null,
    logs: new LogStream(String(task.id)),
    uploads: [],
    outputs: [],
    pendingOutputs: [],
    pendingCheckpoint: undefined,
    checkpointTimer: null,
    checkpointSentAt: 0,
    finished: false,
    cancelled: false,
    timer: null,
    impl: impls.js ? 'js' : (impls.python ? 'python' : null)
  };
  state.current = run;
  setProgress(0);
  renderStatus();
  setState('running', 'busy');
  stamp('task ' + run.id + ' started');

  // jobs that offer JavaScript too keep it here: the browser compiles it to native code anyway
  if (impls.wasm && !impls.js && capsList().indexOf('wasm') >= 0) {
    startWasmTask(run, payload);
    return;
  }
  if (!impls.js) {
    if (impls.python && state.python.wanted) {
      startPythonTask(run, payload);
      return;
    }
    finishTask(run, { ok: false, error: 'no supported impl for this device (have ' + capsList().join(',') + ')' });
    return;
  }

  api('POST', '/api/w/task/' + encodeURIComponent(run.id) + '/start', {})
    .catch(function () { /* best effort */ });

  var inputIds = payload.inputs ? payload.inputs : [];
  var downloads = inputIds.map(fetchInput);

  Promise.all(downloads).then(function (buffers) {
    if (run.cancelled) { return; }
    var runner = new Worker(RUNNER_URL);
    run.worker = runner;
    runner.onmessage = function (event) { onRunnerMessage(run, event.data); };
    runner.onerror = function (event) {
      finishTask(run, {
        ok: false,
        error: event.message ? String(event.message) : 'worker error',
        traceback: ''
      });
    };
    var timeout = Number(payload.timeout) > 0 ? Number(payload.timeout) : 600;
    run.timer = setTimeout(function () {
      finishTask(run, { ok: false, error: 'timeout after ' + timeout + 's', traceback: '' });
    }, timeout * 1000);
    runner.postMessage({
      type: 'run',
      code: impls.js.code,
      entry: impls.js.entry ? impls.js.entry : 'run',
      args: impls.js.args ? impls.js.args : {},
      inputs: buffers,
      task_id: run.id,
      device_name: state.name,
      checkpoint: task.checkpoint === undefined ? null : task.checkpoint
    }, buffers);
  }).catch(function (err) {
    finishTask(run, {
      ok: false,
      error: 'input download failed: ' + (err && err.message ? err.message : String(err)),
      traceback: ''
    });
  });
}

function onRunnerMessage(run, msg) {
  if (!msg || run.finished) { return; }
  if (msg.type === 'log') {
    run.logs.add(msg.line);
    uiLog(msg.line);
    return;
  }
  if (msg.type === 'progress') {
    run.logs.add('__PC_PROGRESS__ ' + msg.value);
    setProgress(msg.value);
    return;
  }
  if (msg.type === 'checkpoint') {
    queueCheckpoint(run, msg.state);
    return;
  }
  if (msg.type === 'output') {
    var upload = putFile(msg.name, msg.data).then(function (meta) {
      run.outputs.push({
        file_id: meta.file_id,
        name: meta.name ? meta.name : msg.name,
        size: meta.size
      });
    }).catch(function (err) {
      if (isTransportError(err)) {
        // Coordinator unreachable: keep the bytes for the outbox.
        run.pendingOutputs.push({ name: msg.name, data: msg.data });
        return;
      }
      run.logs.add('[worker] output upload rejected (' + msg.name + '): ' + err.message);
    });
    run.uploads.push(upload);
    return;
  }
  if (msg.type === 'result') {
    finishTask(run, { ok: true, result: msg.result });
    return;
  }
  if (msg.type === 'error') {
    var payload = msg.payload ? msg.payload : {};
    finishTask(run, {
      ok: false,
      error: payload.error ? payload.error : 'task failed',
      traceback: payload.traceback ? payload.traceback : ''
    });
  }
}

/* ------------------------------------------------------------ checkpoints */
/* Latest state wins; at most one POST per CHECKPOINT_INTERVAL_MS per task. */

var CHECKPOINT_INTERVAL_MS = 2000;
var CHECKPOINT_MAX_BYTES = 256 * 1024;

function queueCheckpoint(run, stateValue) {
  var text;
  try { text = JSON.stringify(stateValue === undefined ? null : stateValue); } catch (err) { return; }
  if (text.length > CHECKPOINT_MAX_BYTES) {
    run.logs.add('[worker] checkpoint ignored: ' + text.length + ' bytes > ' + CHECKPOINT_MAX_BYTES + ' limit');
    return;
  }
  run.pendingCheckpoint = stateValue;
  if (run.checkpointTimer) { return; }
  var wait = Math.max(0, (run.checkpointSentAt || 0) + CHECKPOINT_INTERVAL_MS - Date.now());
  run.checkpointTimer = setTimeout(function () { flushCheckpoint(run); }, wait);
}

function flushCheckpoint(run) {
  if (run.checkpointTimer) { clearTimeout(run.checkpointTimer); run.checkpointTimer = null; }
  if (run.pendingCheckpoint === undefined) { return Promise.resolve(); }
  var body = { state: run.pendingCheckpoint };
  run.pendingCheckpoint = undefined;
  if (run.attempt !== null) { body.attempt = run.attempt; }
  run.checkpointSentAt = Date.now();
  return api('POST', '/api/w/task/' + encodeURIComponent(run.id) + '/checkpoint', body)
    .catch(function (err) {
      if (err.status === 410) { onRemoved(); }
    });
}

function finishTask(run, result) {
  if (run.finished) { return; }
  run.finished = true;
  if (run.timer) { clearTimeout(run.timer); }
  if (run.worker) {
    try { run.worker.terminate(); } catch (err) { /* ignore */ }
    run.worker = null;
  }
  if (run.python) {
    if (state.python.run === run) { state.python.run = null; }
    if (!result.ok && /^(timeout|cancelled)/.test(String(result.error || ''))) {
      // Python cannot be interrupted: drop the interpreter, it reloads next time.
      killPython(result.error);
    }
  }
  var settle = Promise.all(run.uploads).catch(function () { return null; });
  if (!result.ok && run.pendingCheckpoint !== undefined) {
    // let the next attempt resume from the latest progress
    settle = settle.then(function () { return flushCheckpoint(run); });
  } else if (run.checkpointTimer) {
    clearTimeout(run.checkpointTimer); run.checkpointTimer = null; run.pendingCheckpoint = undefined;
  }
  settle.then(function () {
    result.duration = Math.round((Date.now() - run.started)) / 1000;
    result.log_lines = run.logs.count;
    if (run.impl) { result.impl = run.impl; }  /* checks compare like with like (SPEC 9.9) */
    if (run.attempt !== null) { result.attempt = run.attempt; }
    if (result.ok) { result.outputs = run.outputs; }
    return run.logs.close().then(function () {
      if (result.ok && run.pendingOutputs.length) {
        // Some outputs never reached the coordinator: deliver everything later.
        outboxAdd(run.id, result, run.pendingOutputs);
        return;
      }
      return api('POST', '/api/w/task/' + encodeURIComponent(run.id) + '/done', result)
        .catch(function (err) {
          if (err.status === 409) { return; }
          if (isTransportError(err) && result.ok) {
            outboxAdd(run.id, result, []);
            return;
          }
          stamp('done post failed: ' + err.message);
        });
    });
  }).then(function () {
    if (result.ok) { state.tasksDone += 1; }
    stamp('task ' + run.id + (result.ok ? ' done' : ' failed: ' + result.error));
    if (state.current === run) { state.current = null; }
    setProgress(0);
    renderStatus();
    if (!state.stopped) { setState(state.paused ? 'paused' : 'online', state.paused ? 'paused' : 'on'); }
  });
}

function cancelTask(taskId) {
  var run = state.current;
  if (!run || run.id !== String(taskId)) { return; }
  run.cancelled = true;
  finishTask(run, { ok: false, error: 'cancelled', traceback: '' });
}

/* ------------------------------------------------------------ benchmark */

function runBenchmark() {
  stamp('running benchmark ...');
  var bench;
  try {
    bench = new Worker(RUNNER_URL);
  } catch (err) {
    stamp('benchmark unavailable: ' + err.message);
    return;
  }
  bench.onmessage = function (event) {
    var msg = event.data ? event.data : {};
    if (msg.type === 'bench' && msg.result) {
      state.score = msg.result.score;
      renderStatus();
      api('POST', '/api/w/bench', msg.result).catch(function () { /* ignore */ });
      stamp('benchmark score ' + msg.result.score.toFixed(2));
    }
    try { bench.terminate(); } catch (err) { /* ignore */ }
  };
  bench.onerror = function () {
    stamp('benchmark failed');
    try { bench.terminate(); } catch (err) { /* ignore */ }
  };
  bench.postMessage({ type: 'bench' });
}

/* ------------------------------------------------------------- commands */

function handle(cmd) {
  if (!cmd || !cmd.cmd) { return; }
  if (cmd.cmd === 'task') {
    startTask(cmd.task ? cmd.task : {});
  } else if (cmd.cmd === 'cancel') {
    cancelTask(cmd.task_id);
  } else if (cmd.cmd === 'benchmark') {
    runBenchmark();
  } else if (cmd.cmd === 'notify') {
    notify(cmd.title, cmd.body);
  } else if (cmd.cmd === 'config') {
    if (typeof cmd.slots === 'number') { state.slots = Math.max(1, cmd.slots); }
    if (typeof cmd.paused === 'boolean') {
      state.paused = cmd.paused;
      setState(state.paused ? 'paused' : 'online', state.paused ? 'paused' : 'on');
    }
    renderStatus();
  } else if (cmd.cmd === 'hold' || cmd.cmd === 'release') {
    // browsers never host services, so nothing here answers a request to pause for
  } else if (cmd.cmd === 'removed') {
    onRemoved();
  } else if (cmd.cmd === 'shutdown') {
    onStopped('Stopped', 'The coordinator asked this worker to stop.');
  } else {
    stamp('unknown command: ' + cmd.cmd);
  }
}

function notify(title, body) {
  var text = title ? String(title) : 'PhoneCluster';
  var detail = body ? String(body) : '';
  if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
    try {
      new Notification(text, { body: detail });
      stamp('notification: ' + text);
      return;
    } catch (err) { /* fall through to the on-page toast */ }
  }
  toast(text, detail);
  stamp('notification: ' + text + ' - ' + detail);
}

/* ------------------------------------------------------ page updates */
/* The PC sends the version of this page's files with every poll (SPEC 8.4).
   When it changes, the page reloads itself once it is idle: no task, no
   service call of its own running.  The saved device keeps it joined. */

function pageChanged(version) {
  if (!version) { return false; }
  if (!state.page) { state.page = version; return false; }
  if (version === state.page || state.current || svc.active) { return false; }
  stamp('the PC has a newer worker page: reloading');
  state.reloading = true;
  state.stopped = true;
  state.polling = false;
  setTimeout(function () { location.reload(); }, 500);
  return true;
}

/* ----------------------------------------------------------- poll loop */

function statusBlock() {
  return {
    battery: state.battery,
    charging: state.charging,
    temp: null,
    free_slots: (state.paused || state.current) ? 0 : state.slots,
    running: state.current ? [state.current.id] : [],
    storage_free_mb: null
  };
}

function loop() {
  if (state.polling) { return; }
  state.polling = true;
  var backoff = 1000;

  function tick() {
    if (state.stopped) { state.polling = false; return; }
    api('POST', '/api/w/poll', { status: statusBlock() }).then(function (res) {
      backoff = 1000;
      if (!state.current && !state.paused) { setState('online', 'on'); }
      if (outboxCount()) { outboxReplay(); }
      var commands = res.commands ? res.commands : [];
      for (var i = 0; i < commands.length; i++) {
        try { handle(commands[i]); } catch (err) { stamp('command failed: ' + err.message); }
      }
      if (pageChanged(res.page)) { return; }
      tick();
    }).catch(function (err) {
      if (err.status === 410) { onRemoved(); return; }
      if (err.status === 401) {
        stamp('token rejected');
        state.device_id = null;
        state.token = null;
        clearDevice();
        // join codes work once: this device has to join again with a new one
        onStopped('Signed out', 'Open Join on the PC and scan the new code to reconnect this device.');
        return;
      }
      setState('offline', 'off');
      stamp('poll failed (' + (err.message ? err.message : err) + '), retry in '
            + Math.round(backoff / 1000) + 's');
      setTimeout(tick, backoff);
      backoff = Math.min(30000, backoff * 2);
    });
  }

  tick();
}

/* ----------------------------------------------------------- lifecycle */

function onRemoved() {
  state.stopped = true;
  state.polling = false;
  clearDevice();
  outboxSave([]);
  setState('removed', 'off');
  ui.goneTitle.textContent = 'Removed';
  ui.goneText.textContent = 'This device was removed from the cluster.';
  show('gone');
}

function onStopped(title, text) {
  state.stopped = true;
  state.polling = false;
  setState('stopped', 'off');
  ui.goneTitle.textContent = title;
  ui.goneText.textContent = text;
  show('gone');
}

function leave() {
  state.stopped = true;
  state.polling = false;
  if (state.current) { cancelTask(state.current.id); }
  api('POST', '/api/w/leave', {}).catch(function () { return null; }).then(function () {
    clearDevice();
    outboxSave([]);
    state.device_id = null;
    state.token = null;
    onStopped('Left the cluster', 'This browser is no longer a worker.');
  });
}

function rename() {
  var next = window.prompt('Device name', state.name);
  if (!next) { return; }
  state.name = String(next).slice(0, 40);
  renderStatus();
  register(null, true).then(function () {
    stamp('renamed to ' + state.name);
  }).catch(function (err) {
    stamp('rename failed: ' + (err.message ? err.message : err));
  });
}

/* -------------------------------------------------- battery & wakelock */

function watchBattery() {
  if (!navigator.getBattery) { return; }
  navigator.getBattery().then(function (battery) {
    function update() {
      state.battery = Math.round(battery.level * 100);
      state.charging = !!battery.charging;
      renderStatus();
    }
    update();
    battery.addEventListener('levelchange', update);
    battery.addEventListener('chargingchange', update);
  }).catch(function () { /* not supported */ });
}

var wakeLock = null;

function requestWakeLock() {
  if (!navigator.wakeLock || !navigator.wakeLock.request) {
    startWakeVideo();
    return;
  }
  navigator.wakeLock.request('screen').then(function (lock) {
    wakeLock = lock;
    lock.addEventListener('release', function () { wakeLock = null; });
  }).catch(function () { startWakeVideo(); });
}

function startWakeVideo() {
  var video = ui.wakeVideo;
  if (!video || video.src) {
    if (video && video.src) { video.play().catch(function () { return null; }); }
    return;
  }
  try {
    var canvas = document.createElement('canvas');
    canvas.width = 2;
    canvas.height = 2;
    var ctx2d = canvas.getContext('2d');
    ctx2d.fillStyle = '#000';
    ctx2d.fillRect(0, 0, 2, 2);
    var stream = canvas.captureStream(1);
    var chunks = [];
    var recorder = new MediaRecorder(stream);
    recorder.ondataavailable = function (event) {
      if (event.data && event.data.size) { chunks.push(event.data); }
    };
    recorder.onstop = function () {
      try {
        video.src = URL.createObjectURL(new Blob(chunks, { type: 'video/webm' }));
        video.loop = true;
        video.muted = true;
        video.play().catch(function () { return null; });
      } catch (err) { /* ignore */ }
      stream.getTracks().forEach(function (track) { track.stop(); });
    };
    recorder.start();
    setTimeout(function () {
      try { recorder.stop(); } catch (err) { /* ignore */ }
    }, 400);
  } catch (err) { /* no fallback available */ }
}

function onVisibility() {
  if (document.visibilityState === 'visible' && !state.stopped) {
    if (!wakeLock) { requestWakeLock(); }
    if (ui.wakeVideo && ui.wakeVideo.src) {
      ui.wakeVideo.play().catch(function () { return null; });
    }
  }
}

/* ------------------------------------------------------------- services */
/* Call a model hosted on another device (SPEC 6.7). This page only holds
   its device token, which the service endpoints accept, so a phone with no
   compute of its own can still use vosk / translation / TTS on the cluster.
   The upload streams through the coordinator to the model; partial results
   are long-polled while the call is in flight. */

var svc = { list: [], active: null, xhr: null };

function svcId() {
  var s = 'b';
  for (var i = 0; i < 12; i++) { s += Math.floor(Math.random() * 16).toString(16); }
  return s;
}

function svcRefresh() {
  if (!state.token) { return; }
  api('GET', '/api/services').then(function (list) {
    svc.list = Array.isArray(list) ? list : [];
    svcRender();
  }).catch(function () { /* offline: keep what we have */ });
}

function svcRender() {
  if (!ui.svcSelect) { return; }
  var current = ui.svcSelect.value;
  while (ui.svcSelect.firstChild) { ui.svcSelect.removeChild(ui.svcSelect.firstChild); }
  var any = false;
  svc.list.forEach(function (s) {
    // a model that is not loaded yet starts on the first call (SPEC 6.4d)
    if (!s.on_demand && (!s.instances || !s.instances.length)) { return; }
    any = true;
    var opt = document.createElement('option');
    opt.value = s.name;
    opt.textContent = s.name + (s.description ? ' - ' + s.description : '') +
      (s.on_demand ? '' : '  (' + s.ready + ' ready)');
    ui.svcSelect.appendChild(opt);
  });
  ui.svcEmpty.hidden = any;
  ui.svcPanel.hidden = !any;
  if (current) { ui.svcSelect.value = current; }
}

function svcSetStatus(text) { ui.svcStatus.textContent = text; }

function svcAppendPartial(value) {
  ui.svcPartials.hidden = false;
  if (value && typeof value === 'object' && typeof value.token === 'string') {
    ui.svcPartials.textContent += value.token;           // a model's words, as they come
  } else if (value && typeof value === 'object' && typeof value.status === 'string') {
    ui.svcPartials.textContent += '[' + value.status + ']\n';
  } else {
    ui.svcPartials.textContent += (typeof value === 'string' ? value : JSON.stringify(value)) + '\n';
  }
  ui.svcPartials.scrollTop = ui.svcPartials.scrollHeight;
}

function svcWatch(rid) {
  /* long-poll partials until the request ends or the call returns */
  var since = 0;
  function step() {
    if (svc.active !== rid) { return; }
    api('GET', '/api/requests/' + encodeURIComponent(rid) + '/partials?since=' + since + '&wait=10')
      .then(function (page) {
        if (svc.active !== rid) { return; }
        (page.partials || []).forEach(svcAppendPartial);
        since = page.next || since;
        if (page.status === 'done' || page.status === 'failed' || page.status === 'cancelled') { return; }
        step();
      }).catch(function (err) {
        if (err.status === 404) { setTimeout(step, 300); }
      });
  }
  step();
}

function svcCall() {
  var name = ui.svcSelect.value;
  if (!name) { return; }
  var file = ui.svcFile.files && ui.svcFile.files[0];
  var text = ui.svcText.value;
  if (!file && !text) { svcSetStatus('choose a file or type some text first'); return; }
  var params = null;
  if (ui.svcParams.value.trim()) {
    try { params = JSON.parse(ui.svcParams.value); } catch (err) { svcSetStatus('params must be JSON'); return; }
  }
  var rid = svcId();
  svc.active = rid;
  ui.svcPartials.textContent = ''; ui.svcPartials.hidden = true;
  ui.svcResult.textContent = ''; ui.svcResult.hidden = true;
  while (ui.svcOutputs.firstChild) { ui.svcOutputs.removeChild(ui.svcOutputs.firstChild); }
  ui.svcProgress.style.width = '0%';
  ui.svcCallBtn.disabled = true;
  ui.svcCancelBtn.disabled = false;
  svcSetStatus('uploading to ' + name + ' ...');

  var url = state.base + '/api/services/' + encodeURIComponent(name) + '/call?request_id=' + rid
    + (params ? '&params=' + encodeURIComponent(JSON.stringify(params)) : '');
  var xhr = new XMLHttpRequest();
  svc.xhr = xhr;
  xhr.open('POST', url, true);
  var hdrs = headers(file ? { 'Content-Type': file.type || 'application/octet-stream', 'X-File-Name': file.name }
                          : { 'Content-Type': 'text/plain; charset=utf-8' });
  Object.keys(hdrs).forEach(function (k) { xhr.setRequestHeader(k, hdrs[k]); });
  if (xhr.upload) {
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        ui.svcProgress.style.width = Math.round(100 * e.loaded / e.total) + '%';
        if (e.loaded >= e.total) { svcSetStatus('waiting for ' + name + ' ...'); }
      }
    };
  }
  xhr.onload = function () {
    var body = null;
    try { body = JSON.parse(xhr.responseText); } catch (err) { body = null; }
    svcFinish(rid, xhr.status, body);
  };
  xhr.onerror = function () { svcFinish(rid, 0, null); };
  xhr.send(file ? file : text);
  svcWatch(rid);
}

function svcFinish(rid, status, body) {
  if (svc.active !== rid) { return; }
  svc.active = null;
  svc.xhr = null;
  ui.svcCallBtn.disabled = false;
  ui.svcCancelBtn.disabled = true;
  ui.svcProgress.style.width = '100%';
  if (!body) { svcSetStatus(status ? 'failed (HTTP ' + status + ')' : 'network error'); return; }
  if (body.status !== 'done') {
    svcSetStatus((body.status || 'failed') + ': ' + (body.error || body.detail || 'unknown error'));
    return;
  }
  svcSetStatus('done in ' + (body.ms >= 1000 ? (body.ms / 1000).toFixed(1) + ' s' : body.ms + ' ms'));
  if (body.partials && !ui.svcPartials.textContent) { body.partials.forEach(svcAppendPartial); }
  ui.svcResult.hidden = false;
  ui.svcResult.textContent = typeof body.result === 'string' ? body.result : JSON.stringify(body.result, null, 2);
  (body.outputs || []).forEach(svcShowOutput);
  svcRefresh();
}

function svcShowOutput(output) {
  /* fetch through the worker file endpoint (token auth) and show/offer it */
  var row = document.createElement('div');
  row.className = 'svc-output';
  var label = document.createElement('span');
  label.textContent = output.name || output.file_id;
  row.appendChild(label);
  ui.svcOutputs.appendChild(row);
  getBuffer('/api/w/files/' + encodeURIComponent(output.file_id)).then(function (buf) {
    var name = String(output.name || '').toLowerCase();
    var type = /\.wav$/.test(name) ? 'audio/wav' : /\.mp3$/.test(name) ? 'audio/mpeg' : /\.ogg$/.test(name) ? 'audio/ogg'
      : /\.png$/.test(name) ? 'image/png' : /\.jpe?g$/.test(name) ? 'image/jpeg' : 'application/octet-stream';
    var blobUrl = URL.createObjectURL(new Blob([buf], { type: type }));
    if (type.indexOf('audio/') === 0) {
      var audio = document.createElement('audio');
      audio.controls = true; audio.src = blobUrl;
      row.appendChild(audio);
    } else if (type.indexOf('image/') === 0) {
      var img = document.createElement('img');
      img.src = blobUrl; img.alt = output.name || '';
      row.appendChild(img);
    }
    var link = document.createElement('a');
    link.href = blobUrl; link.download = output.name || 'output'; link.textContent = 'save';
    link.className = 'muted';
    row.appendChild(link);
  }).catch(function (err) {
    label.textContent += ' (could not fetch: ' + err.message + ')';
  });
}

function svcCancel() {
  if (!svc.active) { return; }
  var rid = svc.active;
  api('POST', '/api/requests/' + encodeURIComponent(rid) + '/cancel', {}).catch(function () { /* ignore */ });
  if (svc.xhr) { try { svc.xhr.abort(); } catch (err) { /* ignore */ } }
  svcFinish(rid, 0, { status: 'cancelled', error: 'cancelled' });
}

/* ---------------------------------------------------------------- boot */

function join(code, name) {
  state.name = name ? name : (state.name ? state.name : guessName());
  ui.joinError.hidden = true;
  setState('registering');
  detectPython().then(function () {
    return register(code);
  }).then(function () {
    // the code is used up: keep it out of the address bar and history
    if (query('code') && window.history && window.history.replaceState) {
      try { window.history.replaceState(null, '', '/w'); } catch (err) { /* ignore */ }
    }
    show('main');
    renderStatus();
    renderPython();
    setState('online', 'on');
    requestWakeLock();
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      try { Notification.requestPermission(); } catch (err) { /* ignore */ }
    }
    loop();
    // pre-load the interpreter so the first python task does not pay for it
    setTimeout(warmPython, 1500);
    svcRefresh();
    if (!state.svcTimer) { state.svcTimer = setInterval(svcRefresh, 30000); }
  }).catch(function (err) {
    setState('not joined', 'off');
    var message;
    if (err.status === 410) { onRemoved(); return; }
    if (err.status === 401) {
      if (state.device_id) {
        // saved credentials are stale: forget them and ask for a new code
        state.device_id = null;
        state.token = null;
        clearDevice();
        if (code && !state.rejoinTried) {
          // the link carries a (maybe fresh) join code: try it as a new device
          state.rejoinTried = true;
          stamp('saved identity rejected, joining with the code from the link');
          join(code, name);
          return;
        }
        message = 'This device is unknown to the coordinator. Make a new join code on the PC.';
      } else {
        message = 'That join code is wrong, already used or expired. Make a new one on the PC\'s Join page.';
      }
    } else if (err.status === 429) {
      message = 'Too many wrong codes from this device. Wait a few minutes, then try a new code.';
    } else {
      message = 'Could not reach the coordinator: ' + (err.message ? err.message : err);
    }
    show('join');
    ui.nameInput.value = state.name;
    ui.joinError.textContent = message;
    ui.joinError.hidden = false;
  });
}

function boot() {
  ui = {
    dot: $('dot'), state: $('state'),
    joinCard: $('joinCard'), mainCard: $('mainCard'), goneCard: $('goneCard'),
    codeInput: $('codeInput'), nameInput: $('nameInput'), joinBtn: $('joinBtn'),
    joinError: $('joinError'), deviceName: $('deviceName'),
    statusValue: $('statusValue'), batteryValue: $('batteryValue'),
    doneValue: $('doneValue'), scoreValue: $('scoreValue'),
    taskValue: $('taskValue'), progressBar: $('progressBar'), pythonValue: $('pythonValue'),
    pauseBtn: $('pauseBtn'), renameBtn: $('renameBtn'), leaveBtn: $('leaveBtn'),
    logView: $('logView'), toast: $('toast'), wakeVideo: $('wakeVideo'),
    goneTitle: $('goneTitle'), goneText: $('goneText'), againBtn: $('againBtn'),
    svcRefreshBtn: $('svcRefreshBtn'), svcEmpty: $('svcEmpty'), svcPanel: $('svcPanel'),
    svcSelect: $('svcSelect'), svcFile: $('svcFile'), svcText: $('svcText'), svcParams: $('svcParams'),
    svcCallBtn: $('svcCallBtn'), svcCancelBtn: $('svcCancelBtn'), svcProgress: $('svcProgress'),
    svcStatus: $('svcStatus'), svcPartials: $('svcPartials'), svcResult: $('svcResult'), svcOutputs: $('svcOutputs')
  };
  ui.svcRefreshBtn.addEventListener('click', svcRefresh);
  ui.svcCallBtn.addEventListener('click', svcCall);
  ui.svcCancelBtn.addEventListener('click', svcCancel);

  ui.pauseBtn.addEventListener('click', function () {
    state.paused = !state.paused;
    setState(state.paused ? 'paused' : 'online', state.paused ? 'paused' : 'on');
    renderStatus();
    stamp(state.paused ? 'paused - no new tasks' : 'resumed');
  });
  ui.renameBtn.addEventListener('click', rename);
  ui.leaveBtn.addEventListener('click', function () {
    if (window.confirm('Leave the cluster?')) { leave(); }
  });
  ui.againBtn.addEventListener('click', function () { location.href = '/w'; });
  ui.joinBtn.addEventListener('click', function () {
    var code = ui.codeInput.value.trim();
    if (!code) { return; }
    join(code, ui.nameInput.value.trim());
  });
  ui.codeInput.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') { ui.joinBtn.click(); }
  });
  document.addEventListener('visibilitychange', onVisibility);
  window.addEventListener('pagehide', function () {
    if (!state.token || state.stopped || state.reloading) { return; }
    // keepalive lets the request outlive the page; headers stay per SPEC 8
    try {
      fetch(state.base + '/api/w/leave', {
        method: 'POST', keepalive: true, body: '{}',
        headers: headers({ 'Content-Type': 'application/json' })
      }).catch(function () { return null; });
    } catch (err) { /* best effort */ }
  });

  watchBattery();

  var saved = loadDevice();
  var code = query('code');
  var name = query('name');
  if (saved && saved.device_id && saved.token) {
    state.device_id = saved.device_id;
    state.token = saved.token;
    state.name = name ? name : (saved.name ? saved.name : guessName());
    join(code, state.name);
    return;
  }
  state.name = name ? name : guessName();
  ui.nameInput.value = state.name;
  if (code) {
    ui.codeInput.value = code;
    join(code, state.name);
    return;
  }
  setState('not joined', 'off');
  show('join');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}

})();
