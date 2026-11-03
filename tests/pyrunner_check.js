/* Node harness for worker/browser/pyrunner.js against tools/pyodide.
 * Usage: node tests/pyrunner_check.js  -> prints one JSON line per check.
 * Used by tests/test_pyrunner.py; skipped when Pyodide is not installed. */
var path = require('path');
var root = path.resolve(__dirname, '..');
var indexURL = path.join(root, 'tools', 'pyodide') + path.sep;
var runner = require(path.join(root, 'worker', 'browser', 'pyrunner.js'));

function kinds(messages, type) { return messages.filter(function (m) { return m.type === type; }); }

var MAP_CODE = [
  'def fn(item, ctx):',
  '    ctx.log("item %s" % item)',
  '    return item * item',
  ''
].join('\n');

// the exact runner the coordinator appends for python_map (kept in sync by test_pyrunner.py)
var WORKER_PY = process.env.PC_WORKER_PY || '';

runner.createRunner(indexURL).then(function (r) {
  var out = { version: r.version, checks: {} };
  return r.run({
    task_id: 't1', device_name: 'node',
    code: 'def run(args, ctx):\n    return {"sum": sum(args["xs"]), "name": ctx.device_name, "n_inputs": len(ctx.inputs)}\n',
    entry: 'run', args: { xs: [1, 2, 3] }, inputs: [], input_names: []
  }).then(function (m) {
    out.checks.basic = kinds(m, 'result')[0];
    // inputs are files in the virtual FS; outputs come back as bytes
    var enc = new TextEncoder();
    return r.run({
      task_id: 't2', device_name: 'node',
      code: [
        'import os',
        'def run(args, ctx):',
        '    text = open(ctx.input("abc")).read()',
        '    p = os.path.join(ctx.workdir, "out.txt")',
        '    open(p, "w").write(text.upper())',
        '    ctx.output(p, "shout.txt")',
        '    print("printed line")',
        '    ctx.progress(0.5)',
        '    return len(text)',
        ''
      ].join('\n'),
      entry: 'run', args: {}, inputs: [enc.encode('hello').buffer], input_names: ['abc']
    });
  }).then(function (m) {
    out.checks.files = {
      result: kinds(m, 'result')[0],
      error: kinds(m, 'error')[0],
      output: kinds(m, 'output').map(function (o) { return { name: o.name, text: new TextDecoder().decode(o.data) }; }),
      logs: kinds(m, 'log').map(function (l) { return l.line; }),
      progress: kinds(m, 'progress').map(function (p) { return p.value; })
    };
    // errors carry a traceback
    return r.run({ task_id: 't3', code: 'def run(args, ctx):\n    raise ValueError("nope")\n', entry: 'run', args: {}, inputs: [] });
  }).then(function (m) {
    out.checks.error = kinds(m, 'error')[0];
    // checkpoint + resume
    return r.run({
      task_id: 't4',
      code: 'def run(args, ctx):\n    s = ctx.resume({"i": 0})\n    ctx.checkpoint({"i": s["i"] + 1})\n    return s["i"]\n',
      entry: 'run', args: {}, inputs: [], checkpoint: { i: 41 }
    });
  }).then(function (m) {
    out.checks.checkpoint = { result: kinds(m, 'result')[0], error: kinds(m, 'error')[0], posted: kinds(m, 'checkpoint').map(function (c) { return c.state; }) };
    if (!WORKER_PY) { return null; }
    // the real python_map runner, resuming half-way through a chunk
    return r.run({
      task_id: 't5', code: MAP_CODE + WORKER_PY, entry: 'run',
      args: { items: [1, 2, 3, 4], fn_name: 'fn', start: 0 }, inputs: [],
      checkpoint: { n: 4, out: [1, 4] }
    });
  }).then(function (m) {
    if (m) {
      out.checks.python_map = { result: kinds(m, 'result')[0], logs: kinds(m, 'log').map(function (l) { return l.line; }) };
    }
    // globals do not leak between tasks
    return r.run({ task_id: 't6', code: 'def run(args, ctx):\n    return "fn" in globals()\n', entry: 'run', args: {}, inputs: [] });
  }).then(function (m) {
    out.checks.isolated = kinds(m, 'result')[0];
    process.stdout.write(JSON.stringify(out) + '\n');
  });
}).catch(function (err) {
  process.stdout.write(JSON.stringify({ fatal: String(err && err.stack ? err.stack : err) }) + '\n');
  process.exit(1);
});
