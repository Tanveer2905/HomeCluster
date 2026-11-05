/* PhoneCluster dashboard - boot, login, hash router, connection indicator. */
(function () {
  'use strict';
  var PC = (window.PC = window.PC || {});
  var h = PC.h;

  /* 24x24 stroke icons, drawn for this dashboard */
  var ICONS = {
    overview: '<rect x="3.5" y="3.5" width="7" height="8" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="5" rx="1.5"/><rect x="13.5" y="11.5" width="7" height="9" rx="1.5"/><rect x="3.5" y="14.5" width="7" height="6" rx="1.5"/>',
    devices: '<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M11 18h2"/>',
    jobs: '<path d="m3.5 6.5 1.8 1.8 3.2-3.3"/><path d="m3.5 15.5 1.8 1.8 3.2-3.3"/><path d="M12 7h8.5M12 16h8.5"/>',
    files: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    storage: '<ellipse cx="12" cy="5.5" rx="8" ry="2.8"/><path d="M4 5.5v6.3c0 1.5 3.6 2.8 8 2.8s8-1.3 8-2.8V5.5"/><path d="M4 11.8v6.4c0 1.5 3.6 2.8 8 2.8s8-1.3 8-2.8v-6.4"/>',
    services: '<path d="M13 2.5 4.5 13.5H11l-1 8 8.5-11H12z"/>',
    models: '<rect x="5.5" y="5.5" width="13" height="13" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx=".8"/><path d="M9.5 2.5v3M14.5 2.5v3M9.5 18.5v3M14.5 18.5v3M2.5 9.5h3M2.5 14.5h3M18.5 9.5h3M18.5 14.5h3"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="m7.5 9.5 2.5 2.5-2.5 2.5"/><path d="M12.5 15h4"/>',
    schedules: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    events: '<path d="M3 12h3.5l2.5-7 5 14 2.5-7H21"/>',
    settings: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    join: '<rect x="3.5" y="3.5" width="6.5" height="6.5" rx="1"/><rect x="14" y="3.5" width="6.5" height="6.5" rx="1"/><rect x="3.5" y="14" width="6.5" height="6.5" rx="1"/><path d="M14 14h2.5v2.5M20.5 14v.01M14 20.5h6.5v-3.5M17.5 17.5v3"/>',
    theme_system: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8.5 20h7M12 16v4"/>',
    theme_light: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
    theme_dark: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/>'
  };
  function icon(name) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || '') + '</svg>';
  }

  var NAV = [
    { hash: '#/overview', label: 'Overview', view: 'overview' },
    { hash: '#/devices', label: 'Devices', view: 'devices' },
    { hash: '#/jobs', label: 'Jobs', view: 'jobs' },
    { hash: '#/files', label: 'Files', view: 'files', group: 'Data' },
    { hash: '#/storage', label: 'Storage', view: 'storage' },
    { hash: '#/services', label: 'Services', view: 'services', group: 'Compute' },
    { hash: '#/models', label: 'Models', view: 'models' },
    { hash: '#/schedules', label: 'Schedules', view: 'schedules' },
    { hash: '#/terminal', label: 'Terminal', view: 'terminal' },
    { hash: '#/events', label: 'Events', view: 'events', group: 'System' },
    { hash: '#/settings', label: 'Settings', view: 'settings' },
    { hash: '#/join', label: 'Join', view: 'join' }
  ];

  /* ---------------- theme: follow Windows, or pinned light / dark ---------------- */
  var THEME_KEY = 'pc_theme';
  var THEMES = ['system', 'light', 'dark'];
  var THEME_LABEL = { system: 'follows Windows', light: 'light', dark: 'dark' };

  function savedTheme() {
    try { var t = localStorage.getItem(THEME_KEY); return THEMES.indexOf(t) >= 0 ? t : 'system'; } catch (e) { return 'system'; }
  }

  function applyTheme(t) {
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
    else document.documentElement.removeAttribute('data-theme');
    var btn = document.getElementById('theme-btn');
    if (!btn) return;
    btn.innerHTML = icon('theme_' + t);
    var label = 'Theme: ' + THEME_LABEL[t] + ' (click to change)';
    btn.title = label;
    btn.setAttribute('aria-label', label);
  }

  function nextTheme() {
    var t = THEMES[(THEMES.indexOf(savedTheme()) + 1) % THEMES.length];
    try { if (t === 'system') localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, t); } catch (e) { /* private mode */ }
    applyTheme(t);
  }

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

    // nav links, in groups
    NAV.forEach(function (n) {
      if (n.group) els.nav.appendChild(h('div', { class: 'nav-group' }, n.group));
      els.nav.appendChild(h('a', { href: n.hash, class: 'nav-link' },
        h('span', { class: 'nav-icon', html: icon(n.view) }), h('span', {}, n.label)));
    });

    applyTheme(savedTheme());
    document.getElementById('theme-btn').addEventListener('click', nextTheme);

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
