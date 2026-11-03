/* PhoneCluster dashboard - WebSocket live feed + shared store (SPEC §11). */
(function () {
  'use strict';
  var PC = (window.PC = window.PC || {});

  /* ---------------- shared store ---------------- */
  var store = PC.store = {
    devices: [],          // list
    jobs: [],             // most recent jobs (from snapshot / polling / live updates)
    stats: null,
    events: [],           // newest first, capped
    connected: false,
    lastSnapshot: 0
  };

  function upsert(list, item, key) {
    if (!item || !item[key || 'id']) return list;
    var k = key || 'id';
    for (var i = 0; i < list.length; i++) {
      if (list[i][k] === item[k]) { list[i] = Object.assign({}, list[i], item); return list; }
    }
    list.unshift(item);
    return list;
  }
  PC.upsert = upsert;

  /* Fingerprint used to skip pointless re-renders while polling: ignores fields
     that tick on their own (last_seen) but are not worth a DOM rebuild. */
  function fingerprint(list, skip) {
    try {
      return JSON.stringify((list || []).map(function (x) {
        var c = {};
        Object.keys(x).forEach(function (k) { if (skip.indexOf(k) < 0) c[k] = x[k]; });
        return c;
      }));
    } catch (e) { return String(Math.random()); }
  }
  var fpDevices = null, fpJobs = null;

  store.setDevices = function (list, force) {
    store.devices = (list || []).filter(function (d) { return !d.removed; });
    var fp = fingerprint(store.devices, ['last_seen']);
    if (!force && fp === fpDevices) return;
    fpDevices = fp;
    PC.bus.emit('devices', store.devices);
  };
  store.setJobs = function (list, force) {
    store.jobs = list || [];
    var fp = fingerprint(store.jobs, []);
    if (!force && fp === fpJobs) return;
    fpJobs = fp;
    PC.bus.emit('jobs', store.jobs);
  };
  store.invalidate = function () { fpDevices = null; fpJobs = null; };

  /* ---------------- WebSocket ---------------- */
  var ws = PC.ws = {
    sock: null,
    state: 'idle',            // idle | connecting | open | closed
    backoff: 1000,
    maxBackoff: 30000,
    timer: null,
    stopped: false,
    logSubs: {},              // task_id -> true (re-subscribed after reconnect)

    url: function () {
      var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      return proto + '//' + location.host + '/ws/dashboard?pin=' + encodeURIComponent(PC.auth.pin || '');
    },

    setState: function (s, detail) {
      ws.state = s;
      store.connected = s === 'open';
      PC.bus.emit('conn', { state: s, detail: detail || '' });
    },

    connect: function () {
      ws.stopped = false;
      if (ws.sock && (ws.sock.readyState === 0 || ws.sock.readyState === 1)) return;
      clearTimeout(ws.timer);
      ws.setState('connecting');
      var sock;
      try {
        sock = new WebSocket(ws.url());
      } catch (e) {
        ws.setState('closed', String(e && e.message));
        ws.scheduleReconnect();
        return;
      }
      ws.sock = sock;
      sock.onopen = function () {
        ws.backoff = 1000;
        ws.setState('open');
        Object.keys(ws.logSubs).forEach(function (tid) { ws.send({ op: 'sub_logs', task_id: tid }); });
      };
      sock.onclose = function () {
        if (ws.sock === sock) { ws.sock = null; ws.setState('closed'); ws.scheduleReconnect(); }
      };
      sock.onerror = function () { /* onclose follows; nothing to log */ };
      sock.onmessage = function (e) {
        var msg;
        try { msg = JSON.parse(e.data); } catch (err) { return; }
        handle(msg);
      };
    },

    disconnect: function () {
      ws.stopped = true;
      clearTimeout(ws.timer);
      var s = ws.sock;
      ws.sock = null;
      if (s) { try { s.close(); } catch (e) { /* ignore */ } }
      ws.setState('idle');
    },

    scheduleReconnect: function () {
      if (ws.stopped) return;
      clearTimeout(ws.timer);
      var wait = ws.backoff;
      ws.backoff = Math.min(ws.maxBackoff, Math.round(ws.backoff * 1.8));
      ws.timer = setTimeout(function () { ws.connect(); }, wait);
      PC.bus.emit('conn', { state: 'closed', detail: 'retry in ' + Math.round(wait / 1000) + 's' });
    },

    send: function (obj) {
      if (ws.sock && ws.sock.readyState === 1) {
        try { ws.sock.send(JSON.stringify(obj)); return true; } catch (e) { return false; }
      }
      return false;
    },

    subLogs: function (taskId) {
      if (!taskId) return;
      ws.logSubs[taskId] = true;
      ws.send({ op: 'sub_logs', task_id: taskId });
    },
    unsubLogs: function (taskId) {
      if (!taskId) return;
      delete ws.logSubs[taskId];
      ws.send({ op: 'unsub_logs', task_id: taskId });
    },
    termInput: function (sessionId, data) {
      return ws.send({ op: 'term_input', session_id: sessionId, data: data });
    }
  };

  /* ---------------- inbound events (SPEC §11) ---------------- */
  function handle(msg) {
    var ev = msg && msg.ev;
    if (!ev) return;
    switch (ev) {
      case 'snapshot':
        store.lastSnapshot = Date.now();
        if (msg.devices) store.setDevices(PC.asList(msg.devices, 'devices'));
        if (msg.jobs) store.setJobs(PC.asList(msg.jobs, 'jobs'));
        if (msg.stats) { store.stats = msg.stats; PC.bus.emit('stats', msg.stats); }
        if (msg.events) { store.events = PC.asList(msg.events, 'events'); PC.bus.emit('events', store.events); }
        PC.bus.emit('snapshot', msg);
        break;
      case 'device': {
        var d = msg.device || msg.data || msg;
        if (d && d.id) {
          if (d.removed) store.devices = store.devices.filter(function (x) { return x.id !== d.id; });
          else upsert(store.devices, d);
          PC.bus.emit('devices', store.devices);
          PC.bus.emit('device', d);
        }
        break;
      }
      case 'devices':
        store.setDevices(PC.asList(msg.devices || msg.data || [], 'devices'));
        break;
      case 'job': {
        var j = msg.job || msg.data || msg;
        if (j && j.id) {
          if (msg.deleted) store.jobs = store.jobs.filter(function (x) { return x.id !== j.id; });
          else upsert(store.jobs, j);
          PC.bus.emit('jobs', store.jobs);
          PC.bus.emit('job', j);
        }
        break;
      }
      case 'task': {
        var t = msg.task || msg.data || msg;
        if (t && t.id) PC.bus.emit('task', t);
        break;
      }
      case 'log':
        PC.bus.emit('log', { task_id: msg.task_id, lines: msg.lines || [] });
        break;
      case 'event': {
        var e = msg.event || msg.data || msg;
        store.events.unshift(e);
        if (store.events.length > 500) store.events.length = 500;
        PC.bus.emit('event', e);
        PC.bus.emit('events', store.events);
        break;
      }
      case 'services':
        PC.bus.emit('services', { services: msg.services || [] });
        break;
      case 'request':
        PC.bus.emit('request', { request: msg.request || {}, partial: msg.partial });
        break;
      case 'llm':
        PC.bus.emit('llm', msg);
        break;
      case 'term':
        PC.bus.emit('term', { session_id: msg.session_id, data: msg.data || '', pos: msg.pos });
        break;
      case 'term_closed':
        PC.bus.emit('term_closed', { session_id: msg.session_id, exit_code: msg.exit_code });
        break;
      case 'stats':
        store.stats = msg.stats || msg.data || msg;
        PC.bus.emit('stats', store.stats);
        break;
      default:
        // Unknown event kinds are ignored on purpose (forward compatible).
        break;
    }
  }
  PC.ws._handle = handle;

  /* ---------------- polling fallback while the socket is down ---------------- */
  var pollTimer = null, polling = false;
  function pollOnce() {
    if (polling || ws.state === 'open' || !PC.auth.pin) return;
    polling = true;
    PC.api.jobs(null, 50)
      .then(function (jobs) { store.setJobs(jobs); })
      .catch(function () { /* indicator already shows the problem */ })
      .then(function () {
        if (ws.state === 'open') return null;
        return PC.api.devices().then(function (d) { store.setDevices(d); }).catch(function () { });
      })
      .then(function () {
        if (ws.state === 'open') return null;
        return PC.api.stats().then(function (s) { store.stats = s; PC.bus.emit('stats', s); }).catch(function () { });
      })
      .then(function () { polling = false; }, function () { polling = false; });
  }
  PC.startPollingFallback = function () {
    if (pollTimer) return;
    pollTimer = setInterval(pollOnce, 5000);
  };
  PC.stopPollingFallback = function () { clearInterval(pollTimer); pollTimer = null; };
  PC.pollOnce = pollOnce;
})();
