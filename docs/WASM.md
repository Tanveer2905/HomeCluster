# WebAssembly: one program, every device

Compile a program once to WebAssembly and the **same `.wasm` file** runs on the
PC, on Termux phones and in a browser tab, such as the Samsung J2's. You don't
need a Python version and a JavaScript version of the same work any more.

| device | runs WebAssembly with | set up |
|---|---|---|
| PC | wasmtime in `tools\wasmtime` | `scripts\get-wasmtime.ps1` once (13 MB, pinned release, checksum checked) |
| Termux phone (Android 7+) | wasmtime from Termux | nothing: the worker runs `pkg install wasmtime` on its first WebAssembly task (needs internet on the phone once) |
| Browser tab (J2, any Chrome 85+) | the browser's own engine | nothing |

WebAssembly is sandboxed: a task sees only its own `/in` and `/out` folders, has
no network, and gets at most `memory_mb` (256 MB by default). That makes it the
safest way to run code you did not write yourself.

## Two ways to run a module

### 1. Call a function with numbers

The module exports a function that takes and returns numbers. Give the cluster
a range and it cuts it into pieces sized to each device's speed, calls
`f(lo, hi)` on every piece and adds the results up:

```powershell
scripts\pc.ps1 wasm examples\wasm\primes.wasm --call count_primes --range 2 100000000 --wait
```

```python
job = c.wasm("examples/wasm/primes.wasm", call="count_primes", range=(2, 100_000_000), wait=True)
print(job.result())          # {'sum': 5761455, 'tasks': 42}
```

Or one task per argument list: `args_list=[[2, 1000], [1000, 2000]]`
(`--args "[2, 1000]" --args "[1000, 2000]"`). Results come back as a list.

### 2. Run a WASI program

A program with a `main` (compiled for `wasm32-wasi`) reads stdin and its
arguments and prints its answer. Whatever it prints is the task's result, as JSON
when it prints JSON. Files it writes to `/out` come back as outputs. With
`inputs`, every file becomes one task that reads it on stdin and at
`/in/<name>`:

```powershell
scripts\pc.ps1 wasm examples\wasm\wordcount.wasm --input notes.txt --input book.txt --wait
scripts\pc.ps1 wasm examples\wasm\upper.wasm --input notes.txt --wait     # writes /out/upper.txt
```

```python
job = c.wasm("examples/wasm/wordcount.wasm", inputs=["notes.txt", "book.txt"], wait=True)
for row in job.result():
    print(row["input"], row["result"])          # notes.txt {'bytes': 1234, 'lines': 40, 'words': 210}
job.download_outputs("out/")
```

Other options: `args_list` (one command line per task), `stdin` (a text for every
task), `env`, `memory_mb`, `combine` (`list`, `sum` or `text` to join outputs).
In the dashboard, choose **New job → wasm**.

## Making a .wasm file

Any language that compiles to `wasm32-wasi` (also called `wasm32-wasip1`) works:

| language | command |
|---|---|
| Rust | `rustup target add wasm32-wasip1` then `cargo build --release --target wasm32-wasip1` |
| C / C++ | [wasi-sdk](https://github.com/WebAssembly/wasi-sdk): `clang --target=wasm32-wasi -O2 prog.c -o prog.wasm` |
| Zig | `zig build-exe prog.zig -target wasm32-wasi -O ReleaseFast` |
| Go | `GOOS=wasip1 GOARCH=wasm go build -o prog.wasm` (Go 1.21+) |
| TinyGo | `tinygo build -target=wasi -o prog.wasm` |
| AssemblyScript | `asc prog.ts --target release -o prog.wasm` (exports only: use the function mode) |

To export a function for the function mode, build a library (Rust:
`crate-type = ["cdylib"]` with `#[no_mangle] pub extern "C" fn`; C:
`__attribute__((export_name("f")))`). Functions take and return `i32`, `i64`,
`f32` or `f64`.

The three examples in `examples/wasm` were made without a compiler by
`examples/wasm/build_examples.py` (a tiny assembler); `primes.wat` shows what
`primes.wasm` does in WebAssembly's text form.

## How it behaves

- Before anything runs, the coordinator reads the module: a function name that
  is not exported, the wrong number of arguments, a `.wat` text file or a module
  without `main` fails the job with a clear message.
- The module is an input like any other: each device downloads it once and keeps
  it in its cache. A big module or data file another phone already has comes
  from that phone.
- Results of identical runs are reused (same module, function and arguments), and
  about 5 % of tasks are checked on a second device, like any repeatable job.
  The PC and a browser give exactly the same answers, floats included, so
  they can check each other. A program that reads the clock or random numbers
  is noticed (three different answers) and simply not checked any more; give it
  `cache=False` too.
- What the browser cannot do: subfolders in `/in` and `/out`, sockets, sleeping,
  threads. A program that needs them runs on the PC and phones only (the browser
  answers "not supported" and the task moves on).
- Speed: wasmtime compiles the module to machine code; browsers do too. Expect
  roughly native speed for number crunching, far faster than Python.
