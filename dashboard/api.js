/* PhoneCluster dashboard - REST client (SPEC §3, §11). */
(function () {
  'use strict';
  var PC = (window.PC = window.PC || {});

  var PIN_KEY = 'pc_pin';

  PC.auth = {
    get pin() {
      try { return sessionStorage.getItem(PIN_KEY) || ''; } catch (e) { return PC._memPin || ''; }
    },
    set: function (pin) {
      PC._memPin = pin;
      try { sessionStorage.setItem(PIN_KEY, pin); } catch (e) { /* private mode */ }
    },
    clear: function () {
      PC._memPin = '';
      try { sessionStorage.removeItem(PIN_KEY); } catch (e) { /* ignore */ }
    }
  };

  /** Called on any 401. app.js replaces this to show the login screen. */
  PC.onUnauthorized = function () { };

  function headers(extra) {
    var h = Object.assign({}, extra || {});
    var pin = PC.auth.pin;
    if (pin) h['X-Pin'] = pin;
    return h;
  }

  function parseError(res, text) {
    var msg = 'HTTP ' + res.status;
    try {
      var j = JSON.parse(text);
      if (j && j.error) msg = j.error;
      else if (j && j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch (e) {
      if (text && text.length < 300) msg += ': ' + text;
    }
    var err = new Error(msg);
    err.status = res.status;
    return err;
  }

  /**
   * request('/devices', {method, json, body, raw, query})
   * Returns parsed JSON (or the Response when opts.raw).
   */
  PC.request = function (path, opts) {
    opts = opts || {};
    var url = '/api' + path;
    if (opts.query) {
      var qs = Object.keys(opts.query)
        .filter(function (k) { return opts.query[k] !== undefined && opts.query[k] !== null && opts.query[k] !== ''; })
        .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(opts.query[k]); })
        .join('&');
      if (qs) url += (url.indexOf('?') >= 0 ? '&' : '?') + qs;
    }
    var init = {
      method: opts.method || 'GET',
      credentials: 'same-origin',
      headers: headers(opts.headers)
    };
    if (opts.json !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.json);
    } else if (opts.body !== undefined) {
      init.body = opts.body; // FormData: let the browser set the boundary
    }
    return fetch(url, init).then(function (res) {
      if (res.status === 401) {
        PC.onUnauthorized();
        var e = new Error('unauthorized'); e.status = 401;
        throw e;
      }
      if (opts.raw) {
        if (!res.ok) return res.text().then(function (t) { throw parseError(res, t); });
        return res;
      }
      return res.text().then(function (text) {
        if (!res.ok) throw parseError(res, text);
        if (!text) return null;
        try { return JSON.parse(text); } catch (e) { return text; }
      });
    });
  };

  var api = {};
  ['get', 'post', 'patch', 'put', 'delete'].forEach(function (m) {
    api[m === 'delete' ? 'del' : m] = function (path, json, extra) {
      var o = Object.assign({ method: m.toUpperCase() }, extra || {});
      if (json !== undefined) o.json = json;
      return PC.request(path, o);
    };
  });
  api.raw = function (path, opts) { return PC.request(path, Object.assign({ raw: true }, opts || {})); };

  /* ---------- typed endpoints ---------- */
  api.login = function (pin) {
    return fetch('/api/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: pin })
    }).then(function (res) {
      return res.text().then(function (t) {
        if (!res.ok) throw parseError(res, t);
        return t ? JSON.parse(t) : { ok: true };
      });
    });
  };
  api.info = function () { return api.get('/info'); };
  /* a fresh one-time join code with its QR and commands (SPEC 3) */
  api.join = function () { return api.post('/join/code', {}); };
  api.stats = function () { return api.get('/stats'); };

  api.devices = function () { return api.get('/devices').then(function (r) { return PC.asList(r, 'devices'); }); };
  api.device = function (id) { return api.get('/devices/' + encodeURIComponent(id)); };
  api.patchDevice = function (id, body) { return api.patch('/devices/' + encodeURIComponent(id), body); };
  api.removeDevice = function (id) { return api.del('/devices/' + encodeURIComponent(id)); };
  api.benchDevice = function (id) { return api.post('/devices/' + encodeURIComponent(id) + '/benchmark', {}); };
  api.deviceScreen = function (id) { return api.post('/devices/' + encodeURIComponent(id) + '/screen'); };
  api.notifyDevice = function (id, title, body) {
    return api.post('/devices/' + encodeURIComponent(id) + '/notify', { title: title, body: body });
  };

  api.jobs = function (status, limit) {
    return PC.request('/jobs', { query: { status: status, limit: limit } }).then(function (r) { return PC.asList(r, 'jobs'); });
  };
  api.job = function (id) { return api.get('/jobs/' + encodeURIComponent(id)); };
  api.jobTasks = function (id) {
    return api.get('/jobs/' + encodeURIComponent(id) + '/tasks').then(function (r) { return PC.asList(r, 'tasks'); });
  };
  api.jobResult = function (id) { return api.get('/jobs/' + encodeURIComponent(id) + '/result'); };
  api.submitJob = function (spec) { return api.post('/jobs', spec); };
  api.cancelJob = function (id) { return api.post('/jobs/' + encodeURIComponent(id) + '/cancel', {}); };
  api.retryJob = function (id) { return api.post('/jobs/' + encodeURIComponent(id) + '/retry', {}); };
  /* same spec again; fresh = ignore and replace stored results (SPEC 9.8) */
  api.rerunJob = function (id, fresh) { return api.post('/jobs/' + encodeURIComponent(id) + '/rerun', { fresh: !!fresh }); };
  api.cacheInfo = function () { return api.get('/cache'); };
  api.clearCache = function () { return api.del('/cache'); };
  /* catching wrong answers (SPEC 9.9) */
  api.trustAgain = function (id) { return api.post('/devices/' + encodeURIComponent(id) + '/trust', { reset: true }); };
  api.deleteJob = function (id) { return api.del('/jobs/' + encodeURIComponent(id)); };
  api.pipeline = function (body) { return api.post('/pipelines', body); };

  api.task = function (id) { return api.get('/tasks/' + encodeURIComponent(id)); };
  api.cancelTask = function (id) { return api.post('/tasks/' + encodeURIComponent(id) + '/cancel', {}); };
  /** Log endpoint shape is not pinned by the SPEC - accept text, [..] or {lines:[..]}. */
  api.taskLog = function (id, tail) {
    return PC.request('/tasks/' + encodeURIComponent(id) + '/log', { query: { tail: tail } }).then(function (r) {
      if (r === null || r === undefined) return [];
      if (typeof r === 'string') return r.split(/\r?\n/);
      if (Array.isArray(r)) return r;
      if (Array.isArray(r.lines)) return r.lines;
      if (typeof r.log === 'string') return r.log.split(/\r?\n/);
      if (typeof r.text === 'string') return r.text.split(/\r?\n/);
      return [];
    });
  };

  api.files = function () { return api.get('/files').then(function (r) { return PC.asList(r, 'files'); }); };
  api.uploadFile = function (file, onProgress) { return upload('/api/files', file, {}, onProgress); };
  api.deleteFile = function (id) { return api.del('/files/' + encodeURIComponent(id)); };

  api.storage = function () { return api.get('/storage').then(function (r) { return PC.asList(r, 'blobs'); }); };
  api.storageStatus = function (id) { return api.get('/storage/' + encodeURIComponent(id) + '/status'); };
  api.deleteBlob = function (id) { return api.del('/storage/' + encodeURIComponent(id)); };
  api.uploadBlob = function (file, replication, onProgress, scheme) {
    var fields = {};
    if (replication) fields.replication = String(replication);
    if (scheme) fields.scheme = scheme;
    return upload('/api/storage', file, fields, onProgress);
  };

  api.uploadPackage = function (file, onProgress) { return upload('/api/packages', file, {}, onProgress); };

  /* distributed LLMs (SPEC 6.8) */
  api.llmRuntime = function () { return api.get('/llm/runtime'); };
  api.llmModels = function () { return api.get('/llm/models').then(function (r) { return PC.asList(r, 'models'); }); };
  api.llmDevices = function () { return api.get('/llm/devices').then(function (r) { return PC.asList(r, 'devices'); }); };
  api.llmPlan = function (body) { return api.post('/llm/plan', body); };
  api.llmDeployments = function () { return api.get('/llm/deployments').then(function (r) { return PC.asList(r, 'deployments'); }); };
  api.llmDeploy = function (body) { return api.post('/llm/deployments', body); };
  api.llmStop = function (id) { return api.post('/llm/deployments/' + encodeURIComponent(id) + '/stop', {}); };
  api.llmRestart = function (id) { return api.post('/llm/deployments/' + encodeURIComponent(id) + '/restart', {}); };
  api.llmDelete = function (id) { return api.del('/llm/deployments/' + encodeURIComponent(id)); };
  api.llmKeep = function (id, keep) { return api.post('/llm/deployments/' + encodeURIComponent(id) + '/keep', { keep: !!keep }); };
  api.llmDefault = function () { return api.get('/llm/default'); };
  api.llmSetDefault = function (model) { return PC.request('/llm/default', { method: 'PUT', json: { model: model } }); };
  api.speeds = function () { return api.get('/speeds').then(function (r) { return PC.asList(r, 'speeds'); }); };
  api.llmDeleteModel = function (name) { return api.del('/llm/models/' + encodeURIComponent(name)); };
  api.llmFetchModel = function (url, name) { return api.post('/llm/models/fetch', { url: url, name: name || null }); };
  /** Raw PUT of a .gguf file with upload progress (models are too big for multipart). */
  api.llmUploadModel = function (file, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('PUT', '/api/llm/models?name=' + encodeURIComponent(file.name), true);
      xhr.withCredentials = true;
      if (PC.auth.pin) xhr.setRequestHeader('X-Pin', PC.auth.pin);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      if (onProgress && xhr.upload) {
        xhr.upload.onprogress = function (e) { if (e.lengthComputable) onProgress(e.loaded / e.total); };
      }
      xhr.onload = function () {
        var body = null;
        try { body = JSON.parse(xhr.responseText); } catch (e) { body = null; }
        if (xhr.status >= 200 && xhr.status < 300) { resolve(body); return; }
        reject(new Error((body && (body.error || body.detail)) || ('HTTP ' + xhr.status)));
      };
      xhr.onerror = function () { reject(new Error('network error while uploading ' + file.name)); };
      xhr.send(file);
    });
  };

  /* services (SPEC 6.7) */
  api.services = function () { return api.get('/services').then(function (r) { return PC.asList(r, 'services'); }); };
  api.serviceRequests = function (limit) {
    return PC.request('/requests', { query: { limit: limit || 30 } }).then(function (r) { return PC.asList(r, 'requests'); });
  };
  api.serviceRequest = function (id) { return api.get('/requests/' + encodeURIComponent(id)); };
  api.cancelRequest = function (id) { return api.post('/requests/' + encodeURIComponent(id) + '/cancel', {}); };
  /**
   * Call a service with a File (streamed multipart) or a text string.
   * Resolves with the request dict; rejects with the coordinator's error.
   */
  api.callService = function (name, input, params, requestId, onProgress) {
    var query = '?request_id=' + encodeURIComponent(requestId);
    if (params && Object.keys(params).length) { query += '&params=' + encodeURIComponent(JSON.stringify(params)); }
    var url = '/api/services/' + encodeURIComponent(name) + '/call' + query;
    if (input instanceof File || input instanceof Blob) {
      return upload(url, input, {}, onProgress);
    }
    return new Promise(function (resolve, reject) {
      var fd = new FormData();
      fd.append('text', String(input));
      var xhr = new XMLHttpRequest();
      xhr.open('POST', url, true);
      xhr.withCredentials = true;
      if (PC.auth.pin) xhr.setRequestHeader('X-Pin', PC.auth.pin);
      xhr.onload = function () {
        var text = xhr.responseText;
        var body = null;
        try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }
        if (xhr.status === 401) { PC.onUnauthorized(); reject(new Error('unauthorized')); return; }
        if (xhr.status >= 200 && xhr.status < 300) { resolve(body); return; }
        reject(new Error((body && (body.error || body.detail)) || ('HTTP ' + xhr.status)));
      };
      xhr.onerror = function () { reject(new Error('network error')); };
      xhr.send(fd);
    });
  };

  api.openTerminal = function (deviceId) { return api.post('/terminal', { device_id: deviceId }); };
  api.closeTerminal = function (sid) { return api.del('/terminal/' + encodeURIComponent(sid)); };
  api.terminalScrollback = function (sid) { return api.get('/terminal/' + encodeURIComponent(sid)); };
  api.terminals = function () { return api.get('/terminal').then(function (r) { return PC.asList(r, 'sessions'); }); };

  api.schedules = function () { return api.get('/schedules').then(function (r) { return PC.asList(r, 'schedules'); }); };
  api.createSchedule = function (body) { return api.post('/schedules', body); };
  api.patchSchedule = function (id, body) { return api.patch('/schedules/' + encodeURIComponent(id), body); };
  api.deleteSchedule = function (id) { return api.del('/schedules/' + encodeURIComponent(id)); };
  api.runSchedule = function (id) { return api.post('/schedules/' + encodeURIComponent(id) + '/run', {}); };

  api.templates = function () { return api.get('/templates').then(function (r) { return PC.asList(r, 'templates'); }); };
  api.createTemplate = function (name, spec) { return api.post('/templates', { name: name, job_spec: spec }); };
  api.deleteTemplate = function (id) { return api.del('/templates/' + encodeURIComponent(id)); };

  api.plugins = function () { return api.get('/plugins').then(function (r) { return PC.asList(r, 'plugins'); }); };
  api.events = function (limit, kind) {
    return PC.request('/events', { query: { limit: limit || 100, kind: kind } }).then(function (r) { return PC.asList(r, 'events'); });
  };
  api.settings = function () { return api.get('/settings'); };
  api.saveSettings = function (body) { return api.patch('/settings', body); };

  /**
   * Multipart upload with progress (XHR - fetch has no upload progress).
   * The PIN goes in the X-Pin header, never in the URL.
   */
  function upload(url, file, fields, onProgress) {
    return new Promise(function (resolve, reject) {
      var fd = new FormData();
      fd.append('file', file, file.name);
      Object.keys(fields || {}).forEach(function (k) { fd.append(k, fields[k]); });
      var xhr = new XMLHttpRequest();
      xhr.open('POST', url, true);
      xhr.withCredentials = true;
      var pin = PC.auth.pin;
      if (pin) xhr.setRequestHeader('X-Pin', pin);
      if (onProgress && xhr.upload) {
        xhr.upload.onprogress = function (e) {
          if (e.lengthComputable) onProgress(e.loaded / e.total);
        };
      }
      xhr.onload = function () {
        if (xhr.status === 401) { PC.onUnauthorized(); reject(new Error('unauthorized')); return; }
        var text = xhr.responseText;
        if (xhr.status >= 200 && xhr.status < 300) {
          try { resolve(text ? JSON.parse(text) : null); } catch (e) { resolve(text); }
        } else {
          var msg = 'HTTP ' + xhr.status;
          try { var j = JSON.parse(text); if (j.error) msg = j.error; else if (j.detail) msg = String(j.detail); } catch (e) { /* ignore */ }
          reject(new Error(msg));
        }
      };
      xhr.onerror = function () { reject(new Error('network error while uploading ' + file.name)); };
      xhr.send(fd);
    });
  }
  api.upload = upload;

  /**
   * Download through fetch so the X-Pin header is sent even when cookies are blocked,
   * then hand the blob to a temporary <a download>.
   */
  api.download = function (path, filename) {
    var t = PC.toast('Preparing download…', 'info', 60000);
    return PC.request(path, { raw: true }).then(function (res) {
      var name = filename;
      var cd = res.headers.get('Content-Disposition') || '';
      var m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
      if (!name && m) { try { name = decodeURIComponent(m[1]); } catch (e) { name = m[1]; } }
      return res.blob().then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = PC.h('a', { href: url, download: name || 'download' });
        document.body.appendChild(a);
        a.click();
        setTimeout(function () { document.body.removeChild(a); URL.revokeObjectURL(url); }, 1000);
        if (t && t.parentNode) t.parentNode.removeChild(t);
        PC.ok('Downloaded ' + (name || ''));
      });
    }).catch(function (e) {
      if (t && t.parentNode) t.parentNode.removeChild(t);
      PC.err(e);
    });
  };

  PC.api = api;
})();
