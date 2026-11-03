/* Drives worker/browser/runner.js runWasm() from Node with the example
 * modules (examples/wasm) and prints one JSON report; tests/test_wasm.py
 * compares it with what wasmtime prints for the same modules. */
'use strict';

var fs = require('fs');
var path = require('path');
var root = path.resolve(__dirname, '..');
var runner = require(path.join(root, 'worker', 'browser', 'runner.js'));

function load(name) {
  var data = fs.readFileSync(path.join(root, 'examples', 'wasm', name));
  return data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
}

function run(impl, inputs) {
  var messages = [];
  runner.runWasm({ impl: impl, inputs: inputs }, function (msg) { messages.push(msg); });
  var out = { logs: [], outputs: [] };
  messages.forEach(function (msg) {
    if (msg.type === 'log') { out.logs.push(msg.line); }
    if (msg.type === 'output') {
      out.outputs.push({ name: msg.name, text: Buffer.from(msg.data).toString('utf8') });
    }
    if (msg.type === 'result') { out.result = msg.result; }
    if (msg.type === 'error') { out.error = msg.payload; }
  });
  return out;
}

var text = Buffer.from('hello wasm world\nsecond line\n', 'utf8');
var report = {
  primes: run({ module: 0, call: 'count_primes', args: [2, 100000], params: ['i32', 'i32'], results: ['i32'] },
              [load('primes.wasm')]),
  wordcount: run({ module: 0, call: null, args: [], files: [], names: {}, stdin: 'one two three\nfour\n', env: {} },
                 [load('wordcount.wasm')]),
  upper: run({ module: 0, call: null, args: [], files: [1], names: { 1: 'in.txt' }, stdin_input: 1, env: {} },
             [load('upper.wasm'), text.buffer.slice(text.byteOffset, text.byteOffset + text.byteLength)]),
  missing: run({ module: 0, call: 'nope', args: [] }, [load('primes.wasm')]),
  f32: [runner.f32Number(3.140000104904175), runner.f32Number(0.1), runner.f32Number(1)],
  parse: [runner.parseProgramOutput('{"a": 1}\n'), runner.parseProgramOutput('42\n'),
          runner.parseProgramOutput('plain text\n'), runner.parseProgramOutput('Infinity')]
};
process.stdout.write(JSON.stringify(report) + '\n');
