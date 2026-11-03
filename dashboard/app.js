/* PhoneCluster dashboard - boot, login, hash router, connection indicator. */
(function () {
  'use strict';
  var PC = (window.PC = window.PC || {});
  var h = PC.h;

  var NAV = [
    { hash: '#/overview', label: 'Overview', view: 'overview' },
    { hash: '#/devices', label: 'Devices', view: 'devices' },
    { hash: '#/jobs', label: 'Jobs', view: 'jobs' },
    { hash: '#/files', label: 'Files', view: 'files' },
    { hash: '#/storage', label: 'Storage', view: 'storage' },
    { hash: '#/services', label: 'Services', view: 'services' },
    { hash: '#/models', label: 'Models', view: 'models' },
    { hash: '#/terminal', label: 'Terminal', view: 'terminal' },
    { hash: '#/schedules', label: 'Schedules', view: 'schedules' },
    { hash: '#/events', label: 'Events', view: 'events' },
    { hash: '#/settings', label: 'Settings', view: 'settings' },
    { hash: '#/join', label: 'Join', view: 'join' }
  ];

  var els = {};
  var currentCleanup = null;
  var booted = false;

  /* ---------------- login ---------------- */
  function showLogin(message) {
    if (currentCleanup) { try { currentCleanup(); } catch (e) { /* ignore */ } currentCleanup = null; }
    PC.ws.disconnect();
    PC.stopPollingFallback();
    els.app.hidden = true;
    els.login.hidden = false;
    els.loginMsg.textContent = message || '';
    els.loginMsg.classList.toggle('error-text', !!message);
    setTimeout(function () { els.pinInput.focus(); els.pinInput.select(); }, 30);
  }

  function doLogin(pin) {
    pin = String(pin || '').trim();
    if (!pin) { els.loginMsg.textContent = 'Enter the PIN shown in the coordinator console.'; return; }
    els.loginBtn.disabled = true;
    els.loginMsg.classList.remove('error-text');
    els.loginMsg.textContent = 'Checking…';
    PC.api.login(pin).then(function () {
      PC.auth.set(pin);
      els.loginBtn.disabled = false;
      els.pinInput.value = '';
      enterApp();
    }).catch(function (e) {
      els.loginBtn.disabled = false;
      els.loginMsg.classList.add('error-text');
      els.loginMsg.textContent = e && e.status === 401 ? 'Wrong PIN.' : ('Login failed: ' + ((e && e.message) || e));
      els.pinInput.select();
    });
  }

  function enterApp() {
    els.login.hidden = true;
    els.app.hidden = false;
    PC.ws.connect();
    PC.startPollingFallback();
    PC.loadDevices(true).catch(function () { });
    route();
  }

  /* ---------------- connection indicator ---------------- */
  function setConn(info) {
    var s = info.state;
    els.conn.className = 'conn conn-' + s;
    var text = s === 'open' ? 'live' : s === 'connecting' ? 'connecting…' : s === 'idle' ? 'offline' : 'disconnected';
    if (s === 'closed' && info.detail) text += ' · ' + info.detail;
    els.connText.textContent = text;
    els.conn.title = s === 'open'
      ? 'WebSocket connected - live updates'
      : 'WebSocket down - falling back to polling /api/jobs every 5 s';
  }

  /* ---------------- router ---------------- */
  function parseHash() {
    var hash = location.hash || '#/overview';
    var path = hash.replace(/^#\/?/, '').split('?')[0];
    var parts = path.split('/').filter(Boolean);
    if (!parts.length) return { view: 'overview', params: {} };
    if (parts[0] === 'jobs' && parts[1]) return { view: 'job', params: { id: decodeURIComponent(parts[1]) } };
    var known = NAV.filter(function (n) { return n.view === parts[0]; })[0];
    if (known) return { view: known.view, params: {} };
    return { view: 'overview', params: {} };
  }

  function route() {
    if (els.app.hidden) return;
    var r = parseHash();
    var view = PC.views[r.view];
    if (currentCleanup) { try { currentCleanup(); } catch (e) { console.warn('view cleanup failed', e); } currentCleanup = null; }
    PC.clear(els.view);
    els.view.scrollTop = 0;
    window.scrollTo(0, 0);
    document.title = 'PhoneCluster · ' + ((view && view.title) || 'Dashboard');
    if (!view) {
      els.view.appendChild(h('p', { class: 'muted' }, 'Unknown view.'));
      return;
    }
    try {
      currentCleanup = view.render(els.view, r.params) || null;
    } catch (e) {
      console.error('view render failed', e);
      els.view.appendChild(h('p', { class: 'error-text' }, 'This view failed to render: ' + (e.message || e)));
    }
    // highlight nav
    var activeHash = r.view === 'job' ? '#/jobs' : '#/' + r.view;
    PC.qsa('a', els.nav).forEach(function (a) {
      var on = a.getAttribute('href') === activeHash;
      a.classList.toggle('active', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    closeNav();
  }

  function closeNav() {
    if (window.innerWidth <= 860) els.nav.classList.remove('open');
  }

  /* ---------------- boot ---------------- */
  function boot() {
    els.login = document.getElementById('login-screen');
    els.loginForm = document.getElementById('login-form');
    els.pinInput = document.getElementById('pin-input');
    els.loginBtn = document.getElementById('login-btn');
    els.loginMsg = document.getElementById('login-msg');
    els.app = document.getElementById('app');
    els.nav = document.getElementById('nav');
    els.view = document.getElementById('view');
    els.conn = document.getElementById('conn');
    els.connText = document.getElementById('conn-text');

    // nav links
    NAV.forEach(function (n) {
      els.nav.appendChild(h('a', { href: n.hash, class: 'nav-link' }, n.label));
    });

    els.loginForm.addEventListener('submit', function (e) {
      e.preventDefault();
      doLogin(els.pinInput.value);
    });
    // Belt and braces for phone keyboards whose "Go" key does not always
    // trigger implicit form submission.
    els.pinInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        doLogin(els.pinInput.value);
      }
    });

    document.getElementById('nav-toggle').addEventListener('click', function () {
      els.nav.classList.toggle('open');
    });
    document.getElementById('lock-btn').addEventListener('click', function () {
      PC.auth.clear();
      showLogin('Locked. Enter the PIN to continue.');
    });

    window.addEventListener('hashchange', route);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeNav();
    });

    PC.bus.on('conn', setConn);
    setConn({ state: 'idle' });
    PC.startClock();

    PC.onUnauthorized = function () {
      if (els.login.hidden === false) return;
      PC.auth.clear();
      showLogin('Session expired - enter the PIN again.');
    };

    // Show the coordinator name/version on the login screen (public endpoint).
    PC.api.info().then(function (info) {
      if (info && info.version) {
        var v = document.getElementById('version');
        if (v) v.textContent = 'v' + info.version;
      }
    }).catch(function () { });

    if (!location.hash) location.hash = '#/overview';

    // Already have a PIN in this tab? Verify it before showing the app.
    var pin = PC.auth.pin;
    if (pin) {
      PC.api.stats().then(function (s) {
        PC.store.stats = s;
        enterApp();
      }).catch(function (e) {
        if (e && e.status === 401) showLogin('');
        else { PC.toast('Coordinator unreachable: ' + (e.message || e), 'error'); enterApp(); }
      });
    } else {
      showLogin('');
    }
    booted = true;
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  PC._boot = boot;
  PC._booted = function () { return booted; };
})();
