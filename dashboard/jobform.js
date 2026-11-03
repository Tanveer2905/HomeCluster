/* PhoneCluster dashboard - job spec form generated from plugin PARAMS (SPEC §5, §7). */
(function () {
  'use strict';
  var PC = (window.PC = window.PC || {});

  /* ---------------- small caches for form data ---------------- */
  var cache = PC.cache = { plugins: null, files: null, devices: null };

  PC.loadPlugins = function (force) {
    if (cache.plugins && !force) return Promise.resolve(cache.plugins);
    return PC.api.plugins().then(function (p) { cache.plugins = p || []; return cache.plugins; });
  };
  PC.loadFiles = function (force) {
    if (cache.files && !force) return Promise.resolve(cache.files);
    return PC.api.files().then(function (f) { cache.files = f || []; return cache.files; });
  };
  PC.loadDevices = function (force) {
    if (cache.devices && !force && PC.store.devices.length) return Promise.resolve(PC.store.devices);
    return PC.api.devices().then(function (d) {
      cache.devices = d || [];
      PC.store.setDevices(cache.devices);
      return cache.devices;
    });
  };

  function field(labelText, control, help) {
    var f = PC.h('div', { class: 'field' });
    if (labelText) {
      var lab = PC.h('label', { class: 'field-label' }, labelText);
      if (control && control.id) lab.setAttribute('for', control.id);
      f.appendChild(lab);
    }
    f.appendChild(control);
    if (help) f.appendChild(PC.h('div', { class: 'field-help' }, help));
    return f;
  }
  PC.field = field;

  var uid = 0;
  function nextId() { return 'f' + (++uid); }

  /* ---------------- JobForm ---------------- */
  /**
   * new PC.JobForm({plugins, devices, showMeta})
   * .el          root element to append somewhere
   * .setSpec(s)  fill from a jobspec (§5)
   * .getSpec()   read a jobspec back (throws Error on invalid input)
   */
  function JobForm(opts) {
    opts = opts || {};
    // internal types (LLM shards/heads) are managed from the Models page
    this.plugins = (opts.plugins || cache.plugins || []).filter(function (p) { return !p.hidden; });
    this.devices = opts.devices || PC.store.devices || [];
    this.showMeta = opts.showMeta !== false;
    this.fields = {};
    this.build();
  }

  JobForm.prototype.build = function () {
    var self = this;
    var root = PC.h('div', { class: 'jobform' });
    this.el = root;

    // ---- type ----
    this.typeSel = PC.h('select', { class: 'input', id: nextId(), onchange: function () { self.renderParams(); } });
    this.plugins.forEach(function (p) {
      self.typeSel.appendChild(PC.h('option', { value: p.type }, p.type));
    });
    if (!this.plugins.length) this.typeSel.appendChild(PC.h('option', { value: '' }, '(no plugins available)'));
    this.typeDesc = PC.h('div', { class: 'field-help' }, '');
    root.appendChild(field('Job type', this.typeSel));
    root.appendChild(this.typeDesc);

    // ---- name ----
    this.nameInput = PC.h('input', { class: 'input', type: 'text', id: nextId(), placeholder: 'optional - defaults to "<type> <id>"' });
    if (this.showMeta) root.appendChild(field('Name', this.nameInput));

    // ---- plugin params ----
    this.paramsBox = PC.h('div', { class: 'params-box' });
    root.appendChild(PC.h('div', { class: 'form-section' },
      PC.h('h3', { class: 'form-section-title' }, 'Parameters'), this.paramsBox));

    // ---- options ----
    this.priorityInput = PC.h('input', { class: 'input', type: 'number', step: '1', value: '0', id: nextId() });
    this.timeoutInput = PC.h('input', { class: 'input', type: 'number', step: '1', min: '1', placeholder: 'plugin default', id: nextId() });
    this.attemptsInput = PC.h('input', { class: 'input', type: 'number', step: '1', min: '1', placeholder: 'plugin default', id: nextId() });

    this.targetsMode = PC.h('select', { class: 'input', id: nextId(), onchange: function () { self.syncTargets(); } },
      PC.h('option', { value: 'any' }, 'any - one task to any eligible device'),
      PC.h('option', { value: 'all' }, 'all - one task per online device'),
      PC.h('option', { value: 'specific' }, 'specific devices…'));
    this.targetsList = PC.h('select', { class: 'input multi', multiple: true, size: '4', id: nextId() });
    this.targetsWrap = PC.h('div', { class: 'hidden-block' }, this.targetsList);

    this.requiresInput = PC.h('input', { class: 'input', type: 'text', placeholder: 'ffmpeg, termux_api', id: nextId() });
    this.chunkInput = PC.h('input', { class: 'input', type: 'number', step: '1', min: '1', placeholder: 'plugin default', id: nextId() });
    this.failFast = PC.h('input', { type: 'checkbox', id: nextId() });
    // never compute twice (SPEC 9.8): unchecked = compute again even if identical work ran before
    this.reuse = PC.h('input', { type: 'checkbox', id: nextId() });
    this.reuse.checked = true;

    var opts1 = PC.h('div', { class: 'grid-2' },
      field('Priority', this.priorityInput, 'higher runs first'),
      field('Timeout (s / task)', this.timeoutInput),
      field('Max attempts', this.attemptsInput),
      field('Targets', PC.h('div', {}, this.targetsMode, this.targetsWrap)));

    var adv = PC.h('div', { class: 'grid-2' },
      field('Requires (caps, comma separated)', this.requiresInput),
      field('Chunk size', this.chunkInput),
      PC.h('label', { class: 'check' }, this.failFast, PC.h('span', {}, 'Fail fast (cancel the rest on first permanent failure)')),
      PC.h('label', { class: 'check', title: 'Repeatable job types answer identical work from stored results' },
        this.reuse, PC.h('span', {}, 'Reuse results of identical earlier work')));

    var advDetails = PC.h('details', { class: 'advanced' }, PC.h('summary', {}, 'Advanced options'), adv);

    root.appendChild(PC.h('div', { class: 'form-section' },
      PC.h('h3', { class: 'form-section-title' }, 'Options'), opts1, advDetails));

    this.refreshDevices(this.devices);
    this.renderParams();
  };

  JobForm.prototype.refreshDevices = function (devices) {
    var self = this;
    this.devices = devices || [];
    var selected = this.selectedTargets();
    PC.clear(this.targetsList);
    this.devices.forEach(function (d) {
      var o = PC.h('option', { value: d.id },
        (d.name || d.id) + ' (' + (d.platform || '?') + (d.status ? ', ' + d.status : '') + ')');
      if (selected.indexOf(d.id) >= 0) o.selected = true;
      self.targetsList.appendChild(o);
    });
    if (!this.devices.length) this.targetsList.appendChild(PC.h('option', { value: '', disabled: true }, 'no devices yet'));
  };

  JobForm.prototype.selectedTargets = function () {
    return PC.qsa('option', this.targetsList).filter(function (o) { return o.selected && o.value; }).map(function (o) { return o.value; });
  };

  JobForm.prototype.syncTargets = function () {
    this.targetsWrap.classList.toggle('open', this.targetsMode.value === 'specific');
  };

  JobForm.prototype.plugin = function (type) {
    var t = type || this.typeSel.value;
    for (var i = 0; i < this.plugins.length; i++) if (this.plugins[i].type === t) return this.plugins[i];
    return null;
  };

  JobForm.prototype.renderParams = function (values) {
    var self = this;
    var p = this.plugin();
    PC.clear(this.paramsBox);
    this.fields = {};
    this.typeDesc.textContent = p && p.description ? p.description : '';
    if (!p) {
      this.paramsBox.appendChild(PC.h('p', { class: 'muted' }, 'No plugin selected.'));
      return;
    }
    var defs = p.params || [];
    if (!defs.length) this.paramsBox.appendChild(PC.h('p', { class: 'muted' }, 'This plugin takes no parameters.'));
    defs.forEach(function (def) {
      var ctl = self.buildParam(def, values && Object.prototype.hasOwnProperty.call(values, def.name) ? values[def.name] : undefined);
      self.paramsBox.appendChild(ctl.node);
      self.fields[def.name] = ctl;
    });
    // Apply plugin default options as placeholders
    var d = p.default_options || {};
    this.timeoutInput.placeholder = d.timeout !== undefined ? String(d.timeout) : 'default';
    this.attemptsInput.placeholder = d.max_attempts !== undefined ? String(d.max_attempts) : 'default';
    if (d.targets && !values) {
      this.targetsMode.value = (d.targets === 'all' || d.targets === 'any') ? d.targets : 'specific';
      this.syncTargets();
    }
    if (Array.isArray(d.requires) && d.requires.length && !this.requiresInput.value) {
      this.requiresInput.placeholder = d.requires.join(', ');
    }
  };

  /** Build one control for a PARAMS entry. Returns {node, get(), set(v)}. */
  JobForm.prototype.buildParam = function (def, value) {
    var self = this;
    var type = (def.type || 'str').toLowerCase();
    var dflt = def.default;
    var v = value !== undefined ? value : dflt;
    var id = nextId();
    var node, get, set;
    var label = def.name + (def.required ? ' *' : '');

    function textIn(attrs) { return PC.h('input', Object.assign({ class: 'input', id: id }, attrs)); }

    if (type === 'bool') {
      var cb = PC.h('input', { type: 'checkbox', id: id });
      cb.checked = !!v;
      node = PC.h('div', { class: 'field' }, PC.h('label', { class: 'check' }, cb, PC.h('span', {}, label)),
        def.help ? PC.h('div', { class: 'field-help' }, def.help) : null);
      get = function () { return cb.checked; };
      set = function (x) { cb.checked = !!x; };

    } else if (type === 'int' || type === 'float') {
      var num = textIn({ type: 'number', step: type === 'int' ? '1' : 'any' });
      if (v !== undefined && v !== null) num.value = String(v);
      node = field(label + ' (' + type + ')', num, def.help);
      get = function () {
        if (num.value === '') return undefined;
        var n = type === 'int' ? parseInt(num.value, 10) : parseFloat(num.value);
        if (isNaN(n)) throw new Error(def.name + ': not a number');
        return n;
      };
      set = function (x) { num.value = (x === undefined || x === null) ? '' : String(x); };

    } else if (type === 'select') {
      var sel = PC.h('select', { class: 'input', id: id });
      (def.choices || []).forEach(function (c) {
        var val = (c && typeof c === 'object') ? (c.value !== undefined ? c.value : c.name) : c;
        var lbl = (c && typeof c === 'object') ? (c.label || c.name || String(val)) : String(c);
        sel.appendChild(PC.h('option', { value: String(val) }, lbl));
      });
      if (v !== undefined && v !== null) sel.value = String(v);
      node = field(label, sel, def.help);
      get = function () { return sel.value === '' ? undefined : sel.value; };
      set = function (x) { if (x !== undefined && x !== null) sel.value = String(x); };

    } else if (type === 'json') {
      var ta = PC.h('textarea', { class: 'input mono', rows: '4', id: id, spellcheck: 'false' });
      ta.value = v === undefined || v === null ? '' : (typeof v === 'string' ? v : PC.pretty(v));
      node = field(label + ' (JSON)', ta, def.help || 'JSON value');
      get = function () {
        var s = ta.value.trim();
        if (!s) return undefined;
        try { return JSON.parse(s); } catch (e) { throw new Error(def.name + ': invalid JSON - ' + e.message); }
      };
      set = function (x) { ta.value = x === undefined || x === null ? '' : (typeof x === 'string' ? x : PC.pretty(x)); };

    } else if (type === 'code') {
      var code = PC.h('textarea', { class: 'input mono code-area', rows: '10', id: id, spellcheck: 'false' });
      code.value = v === undefined || v === null ? '' : String(v);
      node = field(label + ' (source)', code, def.help || 'Python/JS source; see the plugin docs for the expected entry point');
      get = function () { return code.value === '' ? undefined : code.value; };
      set = function (x) { code.value = x === undefined || x === null ? '' : String(x); };

    } else if (type === 'file' || type === 'files') {
      var multi = type === 'files';
      var sel2 = PC.h('select', multi ? { class: 'input multi', multiple: true, size: '4', id: id } : { class: 'input', id: id });
      var status = PC.h('span', { class: 'field-help' }, '');
      var upBtn = PC.h('button', { class: 'btn btn-sm', type: 'button' }, 'Upload…');
      var picker = PC.h('input', { type: 'file', class: 'offscreen', multiple: multi });
      var wanted = multi ? (Array.isArray(v) ? v.slice() : []) : (v ? [v] : []);

      function fill(files) {
        PC.clear(sel2);
        if (!multi) sel2.appendChild(PC.h('option', { value: '' }, '- none -'));
        (files || []).forEach(function (f) {
          var o = PC.h('option', { value: f.id }, (f.name || f.id) + ' · ' + PC.fmtBytes(f.size));
          if (wanted.indexOf(f.id) >= 0) o.selected = true;
          sel2.appendChild(o);
        });
        // keep ids that are not in the list any more (deleted files / manual ids)
        wanted.forEach(function (id2) {
          if (!PC.qsa('option', sel2).some(function (o) { return o.value === id2; })) {
            var o = PC.h('option', { value: id2 }, id2 + ' (not in file store)');
            o.selected = true;
            sel2.appendChild(o);
          }
        });
        if (!multi && wanted.length) sel2.value = wanted[0];
        status.textContent = (files || []).length + ' file(s) in the store';
      }
      fill(cache.files || []);
      PC.loadFiles().then(fill).catch(function () { status.textContent = 'could not load the file store'; });

      upBtn.addEventListener('click', function () { picker.click(); });
      picker.addEventListener('change', function () {
        var list = Array.prototype.slice.call(picker.files || []);
        if (!list.length) return;
        status.textContent = 'uploading…';
        var seq = Promise.resolve();
        list.forEach(function (f) {
          seq = seq.then(function () {
            return PC.api.uploadFile(f).then(function (meta) {
              var fid = meta && (meta.id || meta.file_id);
              if (fid) { if (multi) wanted.push(fid); else wanted = [fid]; }
            });
          });
        });
        seq.then(function () { return PC.loadFiles(true); })
          .then(function (files) { fill(files); PC.ok('Uploaded'); })
          .catch(function (e) { PC.err(e); status.textContent = 'upload failed'; });
        picker.value = '';
      });

      node = field(label + (multi ? ' (files)' : ' (file)'),
        PC.h('div', {}, sel2, PC.h('div', { class: 'row gap' }, upBtn, status), picker),
        def.help);
      get = function () {
        var chosen = PC.qsa('option', sel2).filter(function (o) { return o.selected && o.value; }).map(function (o) { return o.value; });
        if (multi) return chosen.length ? chosen : undefined;
        return sel2.value || undefined;
      };
      set = function (x) {
        wanted = multi ? (Array.isArray(x) ? x.slice() : []) : (x ? [x] : []);
        fill(cache.files || []);
      };

    } else { // str and anything unknown
      var inp = textIn({ type: 'text' });
      if (v !== undefined && v !== null) inp.value = String(v);
      if (dflt !== undefined && dflt !== null) inp.placeholder = String(dflt);
      node = field(label, inp, def.help);
      get = function () { return inp.value === '' ? undefined : inp.value; };
      set = function (x) { inp.value = x === undefined || x === null ? '' : String(x); };
    }
    return { node: node, get: get, set: set, def: def };
  };

  JobForm.prototype.getSpec = function () {
    var self = this;
    if (!this.typeSel.value) throw new Error('pick a job type');
    var params = {};
    Object.keys(this.fields).forEach(function (k) {
      var v = self.fields[k].get();
      if (v !== undefined) params[k] = v;
    });
    var options = {};
    if (this.timeoutInput.value !== '') options.timeout = parseInt(this.timeoutInput.value, 10);
    if (this.attemptsInput.value !== '') options.max_attempts = parseInt(this.attemptsInput.value, 10);
    if (this.chunkInput.value !== '') options.chunk_size = parseInt(this.chunkInput.value, 10);
    var req = this.requiresInput.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    if (req.length) options.requires = req;
    if (this.failFast.checked) options.fail_fast = true;
    if (!this.reuse.checked) options.cache = false;
    var mode = this.targetsMode.value;
    if (mode === 'all') options.targets = 'all';
    else if (mode === 'specific') {
      var ids = this.selectedTargets();
      if (!ids.length) throw new Error('pick at least one target device (or choose "any")');
      options.targets = ids;
    } else options.targets = 'any';

    var spec = { type: this.typeSel.value, params: params, options: options };
    if (this.nameInput.value.trim()) spec.name = this.nameInput.value.trim();
    var prio = parseInt(this.priorityInput.value, 10);
    if (!isNaN(prio) && prio !== 0) spec.priority = prio;
    return spec;
  };

  JobForm.prototype.setSpec = function (spec) {
    spec = spec || {};
    if (spec.type) {
      var has = PC.qsa('option', this.typeSel).some(function (o) { return o.value === spec.type; });
      if (!has) this.typeSel.appendChild(PC.h('option', { value: spec.type }, spec.type + ' (unknown plugin)'));
      this.typeSel.value = spec.type;
    }
    this.nameInput.value = spec.name || '';
    this.priorityInput.value = spec.priority !== undefined ? String(spec.priority) : '0';
    var o = spec.options || {};
    this.timeoutInput.value = o.timeout !== undefined && o.timeout !== null ? String(o.timeout) : '';
    this.attemptsInput.value = o.max_attempts !== undefined && o.max_attempts !== null ? String(o.max_attempts) : '';
    this.chunkInput.value = o.chunk_size !== undefined && o.chunk_size !== null ? String(o.chunk_size) : '';
    this.requiresInput.value = Array.isArray(o.requires) ? o.requires.join(', ') : '';
    this.failFast.checked = !!o.fail_fast;
    this.reuse.checked = o.cache !== false;
    var t = o.targets;
    if (Array.isArray(t)) {
      this.targetsMode.value = 'specific';
      PC.qsa('option', this.targetsList).forEach(function (op) { op.selected = t.indexOf(op.value) >= 0; });
    } else {
      this.targetsMode.value = t === 'all' ? 'all' : 'any';
    }
    this.syncTargets();
    this.renderParams(spec.params || {});
  };

  PC.JobForm = JobForm;
})();
