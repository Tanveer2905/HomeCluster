/* PhoneCluster browser worker: Python runner (SPEC 15.1).
 *
 * A persistent Web Worker that loads Pyodide (CPython on WebAssembly) once
 * and then runs `python` impls with the same ctx API as worker.py's harness
 * (SPEC 6.1): log, input/inputs, output, workdir, progress, checkpoint/resume.
 *
 * Messages in:  {type:'init', indexURL}
 *               {type:'run', code, entry, args, inputs:[ArrayBuffer], input_names,
 *                task_id, device_name, checkpoint}
 * Messages out: {type:'ready', version} | {type:'init_error', error}
 *               {type:'log', line} | {type:'progress', value} | {type:'checkpoint', state}
 *               {type:'output', name, data:ArrayBuffer} | {type:'result', result}
 *               {type:'error', payload:{error, traceback}}
 *
 * ES2017 only (old Android Chrome). Also loadable from Node for tests:
 * `require('./pyrunner.js').createRunner(indexURL)`.
 */

var IS_WORKER = typeof importScripts === 'function';
var pyodide = null;
var loading = null;

var HARNESS = [
  'import json, os, sys, traceback, shutil',
  'import js',
  '',
  'class Ctx(object):',
  '    def __init__(self, spec):',
  '        self.task_id = spec.get("task_id")',
  '        self.device_name = spec.get("device_name")',
  '        self.workdir = spec.get("workdir")',
  '        self._map = spec.get("inputs") or {}',
  '        order = spec.get("input_order") or list(self._map.keys())',
  '        self.inputs = [self._map[k] for k in order if k in self._map]',
  '        self._outputs = []',
  '        self.checkpoint_state = spec.get("checkpoint")',
  '',
  '    def log(self, *parts):',
  '        msg = " ".join([str(p) for p in parts])',
  '        for line in msg.split("\\n"):',
  '            js._pc_log(line.rstrip("\\r"))',
  '',
  '    def input(self, file_id):',
  '        if file_id in self._map:',
  '            return self._map[file_id]',
  '        try:',
  '            return self.inputs[int(file_id)]',
  '        except (TypeError, ValueError):',
  '            raise KeyError(file_id)',
  '',
  '    def output(self, path, name=None):',
  '        p = os.path.abspath(path)',
  '        self._outputs.append({"path": p, "name": name or os.path.basename(p)})',
  '',
  '    def progress(self, fraction):',
  '        try:',
  '            js._pc_progress(float(fraction))',
  '        except (TypeError, ValueError):',
  '            pass',
  '',
  '    def resume(self, default=None):',
  '        return default if self.checkpoint_state is None else self.checkpoint_state',
  '',
  '    def checkpoint(self, state):',
  '        try:',
  '            text = json.dumps(state)',
  '        except (TypeError, ValueError) as exc:',
  '            raise TypeError("checkpoint state must be JSON-serialisable: %s" % exc)',
  '        js._pc_checkpoint(text)',
  '',
  '',
  'def __pc_run(spec_json):',
  '    spec = json.loads(spec_json)',
  '    ctx = Ctx(spec)',
  '    wd = ctx.workdir',
  '    try:',
  '        os.makedirs(wd, exist_ok=True)',
  '        os.chdir(wd)',
  '        ns = {"__name__": "pc_task"}',
  '        exec(compile(spec.get("code") or "", "<pc_task>", "exec"), ns)',
  '        entry = spec.get("entry") or "run"',
  '        fn = ns.get(entry)',
  '        if not callable(fn):',
  '            raise NameError("entry point %r not found in task code" % entry)',
  '        result = fn(spec.get("args") or {}, ctx)',
  '        outputs = []',
  '        for item in ctx._outputs:',
  '            if os.path.isfile(item["path"]):',
  '                outputs.append(item)',
  '        return json.dumps({"ok": True, "result": result, "outputs": outputs}, default=repr)',
  '    except BaseException:',
  '        et, ev, _tb = sys.exc_info()',
  '        return json.dumps({"ok": False, "error": (str(ev) or et.__name__),',
  '                           "traceback": traceback.format_exc()})',
  '',
  '',
  'def __pc_cleanup(wd):',
  '    try:',
  '        os.chdir("/")',
  '        shutil.rmtree(wd, ignore_errors=True)',
  '    except Exception:',
  '        pass',
  ''
].join('\n');

function post(msg, transfer) {
  if (IS_WORKER) { self.postMessage(msg, transfer || []); }
  else if (typeof module !== 'undefined' && module.exports && module.exports._sink) { module.exports._sink(msg); }
}

function toUint8(data) {
  if (data instanceof Uint8Array) { return data; }
  if (data instanceof ArrayBuffer) { return new Uint8Array(data); }
  if (data && data.buffer instanceof ArrayBuffer) { return new Uint8Array(data.buffer); }
  return new Uint8Array(0);
}

function ensurePyodide(indexURL) {
  if (pyodide) { return Promise.resolve(pyodide); }
  if (loading) { return loading; }
  var loader;
  if (IS_WORKER) {
    importScripts(indexURL + 'pyodide.js');
    loader = self.loadPyodide;
  } else {
    loader = require(indexURL + 'pyodide.js').loadPyodide;
  }
  var stdoutHandler = function (line) { post({ type: 'log', line: String(line) }); };
  var stderrHandler = function (line) { post({ type: 'log', line: '[stderr] ' + String(line) }); };
  loading = loader({ indexURL: indexURL, stdout: stdoutHandler, stderr: stderrHandler }).then(function (py) {
    var g = IS_WORKER ? self : global;
    g._pc_log = function (line) { post({ type: 'log', line: String(line) }); };
    g._pc_progress = function (value) { post({ type: 'progress', value: Number(value) }); };
    g._pc_checkpoint = function (text) {
      var state = null;
      try { state = JSON.parse(text); } catch (err) { return; }
      post({ type: 'checkpoint', state: state });
    };
    py.runPython(HARNESS);
    pyodide = py;
    loading = null;
    return py;
  }).catch(function (err) {
    loading = null;
    throw err;
  });
  return loading;
}

function runTask(msg) {
  var py = pyodide;
  var taskId = String(msg.task_id || 'task');
  var workdir = '/task/' + taskId;
  var inputs = msg.inputs || [];
  var names = msg.input_names || [];
  var map = {};
  var order = [];
  try {
    py.FS.mkdirTree(workdir + '/in');
    for (var i = 0; i < inputs.length; i++) {
      var key = String(names[i] !== undefined ? names[i] : i);
      var path = workdir + '/in/' + i;
      py.FS.writeFile(path, toUint8(inputs[i]));
      map[key] = path;
      order.push(key);
    }
  } catch (err) {
    post({ type: 'error', payload: { error: 'could not stage inputs: ' + err.message, traceback: '' } });
    return;
  }
  var spec = {
    code: msg.code || '',
    entry: msg.entry || 'run',
    args: msg.args || {},
    inputs: map,
    input_order: order,
    workdir: workdir,
    task_id: taskId,
    device_name: msg.device_name || 'browser',
    checkpoint: msg.checkpoint === undefined ? null : msg.checkpoint
  };
  var text;
  try {
    var fn = py.globals.get('__pc_run');
    text = fn(JSON.stringify(spec));
    if (fn.destroy) { fn.destroy(); }
  } catch (err) {
    post({ type: 'error', payload: { error: String(err && err.message ? err.message : err), traceback: '' } });
    cleanup(py, workdir);
    return;
  }
  var out;
  try { out = JSON.parse(String(text)); } catch (err) {
    post({ type: 'error', payload: { error: 'bad result json', traceback: String(text).slice(0, 2000) } });
    cleanup(py, workdir);
    return;
  }
  if (!out.ok) {
    post({ type: 'error', payload: { error: out.error || 'task failed', traceback: out.traceback || '' } });
    cleanup(py, workdir);
    return;
  }
  var outputs = out.outputs || [];
  for (var k = 0; k < outputs.length; k++) {
    try {
      var bytes = py.FS.readFile(outputs[k].path);
      var buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      post({ type: 'output', name: String(outputs[k].name), data: buf }, [buf]);
    } catch (err) {
      post({ type: 'log', line: '[worker] could not read output ' + outputs[k].name + ': ' + err.message });
    }
  }
  post({ type: 'result', result: out.result === undefined ? null : out.result });
  cleanup(py, workdir);
}

function cleanup(py, workdir) {
  try {
    var fn = py.globals.get('__pc_cleanup');
    fn(workdir);
    if (fn.destroy) { fn.destroy(); }
  } catch (err) { /* ignore */ }
}

function onMessage(event) {
  var msg = event.data ? event.data : {};
  if (msg.type === 'init') {
    ensurePyodide(msg.indexURL || '/w/pyodide/').then(function (py) {
      post({ type: 'ready', version: py.version });
    }).catch(function (err) {
      post({ type: 'init_error', error: String(err && err.message ? err.message : err) });
    });
    return;
  }
  if (msg.type === 'run') {
    if (!pyodide) {
      post({ type: 'error', payload: { error: 'python runtime not loaded', traceback: '' } });
      return;
    }
    runTask(msg);
  }
}

if (IS_WORKER) {
  self.addEventListener('message', onMessage);
}

/* Node entry point for tests: createRunner(indexURL) -> {run(msg) -> Promise<messages[]>} */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    _sink: null,
    createRunner: function (indexURL) {
      return ensurePyodide(indexURL).then(function (py) {
        return {
          version: py.version,
          run: function (msg) {
            var messages = [];
            module.exports._sink = function (m) { messages.push(m); };
            runTask(msg);
            module.exports._sink = null;
            return Promise.resolve(messages);
          }
        };
      });
    }
  };
}
