/* PhoneCluster - js task runner (Web Worker).
 *
 * Receives {type:'run', code, entry, args, inputs, timeout, task_id, device_name}
 * from worker.js, evaluates `code`, awaits `entry(args, ctx)` (SPEC 6.3) and
 * posts back log / progress / output / result / error messages.
 *
 * ES2017 only: no modules, no optional chaining, no nullish coalescing - the
 * Samsung J2 runs an old Chrome.
 */
'use strict';

var SHA_K = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
];

/* SHA-256 over a Uint8Array -> lowercase hex string.
 * Implemented here because crypto.subtle is unavailable over plain http on a
 * LAN address (non-secure context), and the benchmark must run offline. */
function sha256Hex(bytes) {
  var h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
  var h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
  var len = bytes.length;
  var withPad = len + 9;
  var total = withPad + ((64 - (withPad % 64)) % 64);
  var block = new Uint8Array(64);
  var w = new Int32Array(64);
  var bitsHi = Math.floor(len / 536870912);
  var bitsLo = (len * 8) >>> 0;
  var offset = 0;

  while (offset < total) {
    var i;
    for (i = 0; i < 64; i++) {
      var pos = offset + i;
      if (pos < len) {
        block[i] = bytes[pos];
      } else if (pos === len) {
        block[i] = 0x80;
      } else if (pos >= total - 8) {
        var tail = pos - (total - 8);
        if (tail < 4) {
          block[i] = (bitsHi >>> (8 * (3 - tail))) & 0xff;
        } else {
          block[i] = (bitsLo >>> (8 * (7 - tail))) & 0xff;
        }
      } else {
        block[i] = 0;
      }
    }
    for (i = 0; i < 16; i++) {
      w[i] = (block[i * 4] << 24) | (block[i * 4 + 1] << 16) |
             (block[i * 4 + 2] << 8) | block[i * 4 + 3];
    }
    for (i = 16; i < 64; i++) {
      var w15 = w[i - 15];
      var w2 = w[i - 2];
      var s0 = ((w15 >>> 7) | (w15 << 25)) ^ ((w15 >>> 18) | (w15 << 14)) ^ (w15 >>> 3);
      var s1 = ((w2 >>> 17) | (w2 << 15)) ^ ((w2 >>> 19) | (w2 << 13)) ^ (w2 >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    var a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, hh = h7;
    for (i = 0; i < 64; i++) {
      var S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      var ch = (e & f) ^ (~e & g);
      var t1 = (hh + S1 + ch + SHA_K[i] + w[i]) | 0;
      var S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      var maj = (a & b) ^ (a & c) ^ (b & c);
      var t2 = (S0 + maj) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0;
      d = c; c = b; b = a; a = (t1 + t2) | 0;
    }
    h0 = (h0 + a) | 0; h1 = (h1 + b) | 0; h2 = (h2 + c) | 0; h3 = (h3 + d) | 0;
    h4 = (h4 + e) | 0; h5 = (h5 + f) | 0; h6 = (h6 + g) | 0; h7 = (h7 + hh) | 0;
    offset += 64;
  }
  var out = [h0, h1, h2, h3, h4, h5, h6, h7];
  var hex = '';
  for (var k = 0; k < 8; k++) {
    hex += ('00000000' + (out[k] >>> 0).toString(16)).slice(-8);
  }
  return hex;
}

/* SPEC 7 benchmark. Identical loop to worker.py; the iteration count is
 * divided by 25 for the score (JIT calibration). */
function benchmark() {
  var x = 42;
  var iters = 0;
  var batch = 200000;
  var start = Date.now();
  var end = start + 2000;
  var i;
  while (true) {
    for (i = 0; i < batch; i++) {
      x = (x * 1103515245 + 12345) & 0x7fffffff;
    }
    iters += batch;
    var left = end - Date.now();
    if (left <= 0) { break; }
    var rate = iters / Math.max(1, Date.now() - start);
    batch = Math.max(100000, Math.min(4000000, Math.floor(rate * Math.min(left, 250))));
  }
  var buf = new Uint8Array(1024 * 1024);
  for (i = 0; i < buf.length; i++) { buf[i] = 0xa5; }
  var rounds = 0;
  var hstart = Date.now();
  var hend = hstart + 1000;
  var sink = '';
  while (Date.now() < hend) {
    sink = sha256Hex(buf);
    rounds += 1;
  }
  var helapsed = Math.max(0.001, (Date.now() - hstart) / 1000);
  return {
    score: iters / 25 / 1e6,
    loop_iters: iters,
    hash_mb_s: Math.round((rounds / helapsed) * 1000) / 1000,
    kind: 'js',
    _sink: (x & 1) + sink.length
  };
}

function toArrayBuffer(data) {
  if (data === null || data === undefined) { return new ArrayBuffer(0); }
  if (data instanceof ArrayBuffer) { return data.slice(0); }
  if (typeof data === 'string') {
    var enc = new TextEncoder();
    return enc.encode(data).buffer;
  }
  if (data.buffer instanceof ArrayBuffer) {
    return data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
  }
  if (typeof Blob !== 'undefined' && data instanceof Blob) {
    if (typeof FileReaderSync !== 'undefined') {
      return new FileReaderSync().readAsArrayBuffer(data);
    }
    throw new Error('Blob outputs are not supported in this browser');
  }
  var enc2 = new TextEncoder();
  return enc2.encode(JSON.stringify(data)).buffer;
}

function post(msg, transfer) {
  if (transfer) { self.postMessage(msg, transfer); } else { self.postMessage(msg); }
}

function describe(err) {
  if (!err) { return { error: 'unknown error', traceback: '' }; }
  if (typeof err === 'string') { return { error: err, traceback: '' }; }
  var message = err.message ? String(err.message) : String(err);
  var stack = err.stack ? String(err.stack) : '';
  return { error: message, traceback: stack };
}

function runTask(msg) {
  var entry = msg.entry ? msg.entry : 'run';
  var fn = null;
  try {
    var globalEval = eval;
    globalEval(msg.code);
    if (typeof self[entry] === 'function') {
      fn = self[entry];
    } else {
      var candidate = globalEval(entry);
      if (typeof candidate === 'function') { fn = candidate; }
    }
  } catch (err) {
    post({ type: 'error', payload: describe(err) });
    return;
  }
  if (!fn) {
    post({ type: 'error', payload: { error: 'entry point ' + entry + ' not found in task code', traceback: '' } });
    return;
  }

  var inputs = msg.inputs ? msg.inputs : [];
  var saved = msg.checkpoint === undefined ? null : msg.checkpoint;
  var ctx = {
    task_id: msg.task_id,
    device_name: msg.device_name,
    workdir: null,
    inputs: inputs,
    input: function (index) { return inputs[index]; },
    log: function (line) { post({ type: 'log', line: String(line) }); },
    progress: function (fraction) { post({ type: 'progress', value: Number(fraction) }); },
    output: function (name, data) {
      var buf = toArrayBuffer(data);
      post({ type: 'output', name: String(name), data: buf }, [buf]);
    },
    /* SPEC 6.5: resumable progress */
    checkpoint_state: saved,
    resume: function (fallback) { return saved === null ? (fallback === undefined ? null : fallback) : saved; },
    checkpoint: function (state) {
      var copy = JSON.parse(JSON.stringify(state === undefined ? null : state));
      post({ type: 'checkpoint', state: copy });
    }
  };

  Promise.resolve().then(function () {
    return fn(msg.args ? msg.args : {}, ctx);
  }).then(function (result) {
    try {
      post({ type: 'result', result: result === undefined ? null : result });
    } catch (err) {
      post({ type: 'result', result: JSON.parse(JSON.stringify(result === undefined ? null : result)) });
    }
  }).catch(function (err) {
    post({ type: 'error', payload: describe(err) });
  });
}

/* ------------------------------------------------------------ WebAssembly */
/* SPEC 6.9: the same .wasm file the PC runs with wasmtime.  A `call` runs an
 * exported function with numbers; a program runs _start on a small WASI
 * preview1 layer: arguments, environment, stdin, stdout/stderr, clocks,
 * random numbers and an in-memory /in (inputs) and /out (outputs).  No
 * network, no other files.  BigInt is used through BigInt() calls only, never
 * literals, so this file still parses on an old Chrome. */

var WASI = {
  SUCCESS: 0, ACCES: 2, BADF: 8, EXIST: 20, INVAL: 28, ISDIR: 31, NOENT: 44,
  NOSYS: 52, NOTDIR: 54, SPIPE: 70
};

function WasiExit(code) { this.code = code; this.message = 'exit ' + code; }

function f32Number(x) {
  /* the shortest decimal that is this f32, like wasmtime prints it */
  var f = Math.fround(x);
  if (!isFinite(f)) { return f; }
  for (var p = 1; p <= 9; p++) {
    var s = f.toPrecision(p);
    if (Math.fround(parseFloat(s)) === f) { return parseFloat(s); }
  }
  return f;
}

function wasmValue(value, type) {
  if (typeof value === 'bigint') {
    var n = Number(value);
    return Number.isSafeInteger(n) ? n : value.toString();
  }
  if (typeof value === 'number') {
    if (isNaN(value)) { return 'NaN'; }
    if (!isFinite(value)) { return value > 0 ? 'inf' : '-inf'; }
    if (type === 'f32') { return f32Number(value); }
  }
  return value === undefined ? null : value;
}

function parseProgramOutput(text) {
  /* mirrors worker.py parse_program_output: JSON when it is JSON, else the text */
  var s = text.trim();
  var first = s.charAt(0);
  var numeric = s !== '' && !isNaN(Number(s));
  if (first === '{' || first === '[' || first === '"' || s === 'true' || s === 'false' || s === 'null' || numeric) {
    try { return JSON.parse(s); } catch (err) { /* not JSON after all */ }
  }
  return text;
}

function makeWasi(opts) {
  var memory = null;
  var enc = new TextEncoder();
  var dec = new TextDecoder('utf-8');
  var args = (opts.args || []).map(function (a) { return enc.encode(String(a) + '\0'); });
  var env = Object.keys(opts.env || {}).sort().map(function (k) { return enc.encode(k + '=' + opts.env[k] + '\0'); });
  var stdin = opts.stdin || new Uint8Array(0);
  var stdinPos = 0;
  var inFiles = opts.files || {};
  var outFiles = {};
  var dirs = { 3: '/in', 4: '/out' };
  var fds = {};
  var nextFd = 5;
  var stdout = [];
  var stderrText = '';
  var log = opts.log || function () {};

  function dv() { return new DataView(memory.buffer); }
  function mem() { return new Uint8Array(memory.buffer); }
  function set64(ptr, value) {
    var v = Math.max(0, Number(value));
    dv().setUint32(ptr, v % 4294967296, true);
    dv().setUint32(ptr + 4, Math.floor(v / 4294967296), true);
  }
  function iovs(ptr, count) {
    var out = [];
    for (var i = 0; i < count; i++) {
      out.push([dv().getUint32(ptr + i * 8, true), dv().getUint32(ptr + i * 8 + 4, true)]);
    }
    return out;
  }
  function sizes(list, countPtr, sizePtr) {
    var total = 0;
    list.forEach(function (b) { total += b.length; });
    dv().setUint32(countPtr, list.length, true);
    dv().setUint32(sizePtr, total, true);
    return WASI.SUCCESS;
  }
  function strings(list, ptrsPtr, bufPtr) {
    list.forEach(function (b, i) {
      dv().setUint32(ptrsPtr + i * 4, bufPtr, true);
      mem().set(b, bufPtr);
      bufPtr += b.length;
    });
    return WASI.SUCCESS;
  }
  function outFile(name) {
    if (!outFiles[name]) { outFiles[name] = { data: new Uint8Array(0) }; }
    return outFiles[name];
  }
  function fileData(entry) {
    return entry.dir === '/in' ? inFiles[entry.name] : outFile(entry.name).data;
  }
  function writeOut(entry, chunk) {
    var file = outFile(entry.name);
    var end = entry.pos + chunk.length;
    if (end > file.data.length) {
      var grown = new Uint8Array(Math.max(end, file.data.length * 2));
      grown.set(file.data);
      file.data = grown.subarray(0, end);
    }
    file.data.set(chunk, entry.pos);
    entry.pos = end;
  }
  function stat(ptr, filetype, size) {
    for (var i = 0; i < 64; i++) { dv().setUint8(ptr + i, 0); }
    dv().setUint8(ptr + 16, filetype);
    set64(ptr + 24, 1);
    set64(ptr + 32, size);
  }
  function lookup(dirfd, pathPtr, pathLen) {
    var dir = dirs[dirfd] || (fds[dirfd] && fds[dirfd].kind === 'dir' ? fds[dirfd].dir : null);
    if (!dir) { return { error: WASI.BADF }; }
    var name = dec.decode(mem().subarray(pathPtr, pathPtr + pathLen)).replace(/^(\.\/)+/, '');
    if (name.indexOf('/') >= 0 || name === '..') { return { error: WASI.NOENT }; }
    return { dir: dir, name: name };
  }

  var api = {
    args_sizes_get: function (c, s) { return sizes(args, c, s); },
    args_get: function (p, b) { return strings(args, p, b); },
    environ_sizes_get: function (c, s) { return sizes(env, c, s); },
    environ_get: function (p, b) { return strings(env, p, b); },
    clock_res_get: function (id, ptr) { set64(ptr, 1000); return WASI.SUCCESS; },
    clock_time_get: function (id, precision, ptr) {
      var now = id === 0 ? Date.now() * 1e6 : (typeof performance !== 'undefined' ? performance.now() : Date.now()) * 1e6;
      set64(ptr, Math.round(now));
      return WASI.SUCCESS;
    },
    fd_write: function (fd, iovPtr, iovCnt, nPtr) {
      var total = 0;
      var entry = fds[fd];
      if (fd !== 1 && fd !== 2 && !(entry && entry.dir === '/out')) { return WASI.BADF; }
      iovs(iovPtr, iovCnt).forEach(function (io) {
        var chunk = mem().slice(io[0], io[0] + io[1]);
        total += chunk.length;
        if (fd === 1) {
          stdout.push(chunk);
        } else if (fd === 2) {
          stderrText += dec.decode(chunk);
          var lines = stderrText.split('\n');
          stderrText = lines.pop();
          lines.forEach(function (line) { log('[stderr] ' + line); });
        } else {
          writeOut(entry, chunk);
        }
      });
      dv().setUint32(nPtr, total, true);
      return WASI.SUCCESS;
    },
    fd_read: function (fd, iovPtr, iovCnt, nPtr) {
      var entry = fds[fd];
      var source;
      var pos;
      if (fd === 0) { source = stdin; pos = stdinPos; } else if (entry && entry.kind === 'file') {
        source = fileData(entry); pos = entry.pos;
      } else { return WASI.BADF; }
      var total = 0;
      iovs(iovPtr, iovCnt).forEach(function (io) {
        var n = Math.min(io[1], source.length - pos);
        if (n > 0) { mem().set(source.subarray(pos, pos + n), io[0]); pos += n; total += n; }
      });
      if (fd === 0) { stdinPos = pos; } else { entry.pos = pos; }
      dv().setUint32(nPtr, total, true);
      return WASI.SUCCESS;
    },
    fd_close: function (fd) { if (fds[fd]) { delete fds[fd]; return WASI.SUCCESS; } return fd < 5 ? WASI.SUCCESS : WASI.BADF; },
    fd_seek: function (fd, offset, whence, ptr) {
      var entry = fds[fd];
      if (!entry || entry.kind !== 'file') { return fd <= 2 ? WASI.SPIPE : WASI.BADF; }
      var base = whence === 0 ? 0 : whence === 1 ? entry.pos : fileData(entry).length;
      var next = base + Number(offset);
      if (next < 0) { return WASI.INVAL; }
      entry.pos = next;
      set64(ptr, next);
      return WASI.SUCCESS;
    },
    fd_tell: function (fd, ptr) {
      var entry = fds[fd];
      if (!entry || entry.kind !== 'file') { return WASI.BADF; }
      set64(ptr, entry.pos);
      return WASI.SUCCESS;
    },
    fd_fdstat_get: function (fd, ptr) {
      var entry = fds[fd];
      var type = fd <= 2 ? 2 : dirs[fd] || (entry && entry.kind === 'dir') ? 3 : entry ? 4 : -1;
      if (type < 0) { return WASI.BADF; }
      for (var i = 0; i < 24; i++) { dv().setUint8(ptr + i, 0); }
      dv().setUint8(ptr, type);
      for (var j = 8; j < 24; j++) { dv().setUint8(ptr + j, 0xff); }
      return WASI.SUCCESS;
    },
    fd_fdstat_set_flags: function () { return WASI.SUCCESS; },
    fd_prestat_get: function (fd, ptr) {
      if (!dirs[fd]) { return WASI.BADF; }
      dv().setUint8(ptr, 0);
      dv().setUint32(ptr + 4, enc.encode(dirs[fd]).length, true);
      return WASI.SUCCESS;
    },
    fd_prestat_dir_name: function (fd, ptr, len) {
      if (!dirs[fd]) { return WASI.BADF; }
      mem().set(enc.encode(dirs[fd]).subarray(0, len), ptr);
      return WASI.SUCCESS;
    },
    fd_filestat_get: function (fd, ptr) {
      var entry = fds[fd];
      if (fd <= 2) { stat(ptr, 2, 0); return WASI.SUCCESS; }
      if (dirs[fd] || (entry && entry.kind === 'dir')) { stat(ptr, 3, 0); return WASI.SUCCESS; }
      if (!entry) { return WASI.BADF; }
      stat(ptr, 4, fileData(entry).length);
      return WASI.SUCCESS;
    },
    path_filestat_get: function (dirfd, flags, pathPtr, pathLen, ptr) {
      var found = lookup(dirfd, pathPtr, pathLen);
      if (found.error) { return found.error; }
      if (found.name === '' || found.name === '.') { stat(ptr, 3, 0); return WASI.SUCCESS; }
      var data = found.dir === '/in' ? inFiles[found.name] : (outFiles[found.name] || {}).data;
      if (!data) { return WASI.NOENT; }
      stat(ptr, 4, data.length);
      return WASI.SUCCESS;
    },
    path_open: function (dirfd, dirflags, pathPtr, pathLen, oflags, rightsBase, rightsInh, fdflags, fdPtr) {
      var found = lookup(dirfd, pathPtr, pathLen);
      if (found.error) { return found.error; }
      var fd = nextFd++;
      if (found.name === '' || found.name === '.' || (oflags & 2)) {
        if (found.name !== '' && found.name !== '.') { return WASI.NOTDIR; }
        fds[fd] = { kind: 'dir', dir: found.dir };
      } else if (found.dir === '/in') {
        if (!inFiles[found.name]) { return WASI.NOENT; }
        if (oflags & 1) { return WASI.ACCES; }
        fds[fd] = { kind: 'file', dir: '/in', name: found.name, pos: 0 };
      } else {
        var exists = !!outFiles[found.name];
        if (!exists && !(oflags & 1)) { return WASI.NOENT; }
        if (exists && (oflags & 4)) { return WASI.EXIST; }
        if (oflags & 8) { outFiles[found.name] = { data: new Uint8Array(0) }; }
        outFile(found.name);
        fds[fd] = { kind: 'file', dir: '/out', name: found.name, pos: 0 };
        if (fdflags & 1) { fds[fd].pos = outFiles[found.name].data.length; }
      }
      dv().setUint32(fdPtr, fd, true);
      return WASI.SUCCESS;
    },
    path_unlink_file: function (dirfd, pathPtr, pathLen) {
      var found = lookup(dirfd, pathPtr, pathLen);
      if (found.error) { return found.error; }
      if (found.dir !== '/out' || !outFiles[found.name]) { return WASI.NOENT; }
      delete outFiles[found.name];
      return WASI.SUCCESS;
    },
    fd_readdir: function (fd, buf, len, cookie, usedPtr) { dv().setUint32(usedPtr, 0, true); return WASI.SUCCESS; },
    proc_exit: function (code) { throw new WasiExit(code); },
    random_get: function (ptr, len) {
      for (var off = 0; off < len; off += 65536) {
        crypto.getRandomValues(mem().subarray(ptr + off, ptr + Math.min(len, off + 65536)));
      }
      return WASI.SUCCESS;
    },
    sched_yield: function () { return WASI.SUCCESS; },
    fd_sync: function () { return WASI.SUCCESS; },
    fd_datasync: function () { return WASI.SUCCESS; },
    fd_advise: function () { return WASI.SUCCESS; }
  };

  return {
    imports: function (module) {
      var wasi = {};
      var other = {};
      WebAssembly.Module.imports(module).forEach(function (imp) {
        if (imp.kind !== 'function') { return; }
        if (imp.module === 'wasi_snapshot_preview1' || imp.module === 'wasi_unstable') {
          wasi[imp.name] = api[imp.name] || function () { return WASI.NOSYS; };
        } else {
          other[imp.module] = other[imp.module] || {};
          other[imp.module][imp.name] = function () { throw new Error('the module needs ' + imp.module + '.' + imp.name + ', which only its own host provides'); };
        }
      });
      other.wasi_snapshot_preview1 = wasi;
      other.wasi_unstable = wasi;
      return other;
    },
    bind: function (instance) { memory = instance.exports.memory || null; },
    stdoutText: function () {
      var total = 0;
      stdout.forEach(function (c) { total += c.length; });
      var all = new Uint8Array(total);
      var pos = 0;
      stdout.forEach(function (c) { all.set(c, pos); pos += c.length; });
      if (stderrText) { log('[stderr] ' + stderrText); stderrText = ''; }
      return dec.decode(all);
    },
    outputs: function () {
      return Object.keys(outFiles).sort().map(function (name) {
        return { name: name, data: outFiles[name].data.slice().buffer };
      });
    }
  };
}

function runWasm(msg, emit) {
  emit = emit || post;
  var impl = msg.impl || {};
  var buffers = msg.inputs || [];
  var log = function (line) { emit({ type: 'log', line: String(line) }); };
  try {
    var module = new WebAssembly.Module(buffers[impl.module || 0]);
    var files = {};
    (impl.files || []).forEach(function (idx) {
      var name = String((impl.names || {})[String(idx)] || ('input-' + idx)).split('/').pop();
      files[name] = new Uint8Array(buffers[idx]);
    });
    var stdin = impl.stdin_input !== null && impl.stdin_input !== undefined
      ? new Uint8Array(buffers[impl.stdin_input]) : new TextEncoder().encode(impl.stdin || '');
    var argv = impl.call ? [] : ['module.wasm'].concat((impl.args || []).map(String));
    var wasi = makeWasi({ args: argv, env: impl.env || {}, stdin: stdin, files: files, log: log });
    var instance = new WebAssembly.Instance(module, wasi.imports(module));
    wasi.bind(instance);
    var result;
    if (impl.call) {
      if (typeof instance.exports._initialize === 'function') { instance.exports._initialize(); }
      var fn = instance.exports[impl.call];
      if (typeof fn !== 'function') { throw new Error('the module exports no function ' + impl.call); }
      var types = impl.params || [];
      var callArgs = (impl.args || []).map(function (a, i) {
        return types[i] === 'i64' ? BigInt(Math.trunc(Number(a))) : Number(a);
      });
      var value = fn.apply(null, callArgs);
      var resultTypes = impl.results || [];
      if (Array.isArray(value)) {
        result = value.map(function (v, i) { return wasmValue(v, resultTypes[i]); });
      } else {
        result = wasmValue(value, resultTypes[0]);
      }
      var printed = wasi.stdoutText();
      if (printed) { printed.split('\n').forEach(function (line) { if (line) { log(line); } }); }
    } else {
      var code = 0;
      try {
        if (typeof instance.exports._start !== 'function') { throw new Error('the module has no _start'); }
        instance.exports._start();
      } catch (err) {
        if (!(err instanceof WasiExit)) { throw err; }
        code = err.code;
      }
      var text = wasi.stdoutText();
      if (code !== 0) {
        emit({ type: 'error', payload: { error: 'WebAssembly program exited with code ' + code, traceback: text.slice(-4000) } });
        return;
      }
      wasi.outputs().forEach(function (file) { emit({ type: 'output', name: file.name, data: file.data }, [file.data]); });
      result = parseProgramOutput(text);
    }
    emit({ type: 'result', result: result === undefined ? null : result });
  } catch (err) {
    emit({ type: 'error', payload: describe(err) });
  }
}

function onMessage(event) {
  var msg = event.data ? event.data : {};
  if (msg.type === 'wasm') {
    runWasm(msg);
    return;
  }
  if (msg.type === 'bench') {
    try {
      var result = benchmark();
      delete result._sink;
      post({ type: 'bench', result: result });
    } catch (err) {
      post({ type: 'error', payload: describe(err) });
    }
    return;
  }
  if (msg.type === 'run') {
    runTask(msg);
  }
}

if (typeof self !== 'undefined' && typeof self.addEventListener === 'function') {
  self.addEventListener('message', onMessage);
}

/* exported for node-based unit tests; ignored in the browser */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { sha256Hex: sha256Hex, benchmark: benchmark, toArrayBuffer: toArrayBuffer,
    runWasm: runWasm, f32Number: f32Number, parseProgramOutput: parseProgramOutput };
}
