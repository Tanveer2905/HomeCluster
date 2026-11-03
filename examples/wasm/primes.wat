;; The text form of primes.wasm (examples/wasm/build_examples.py writes the
;; binary).  wasmtime runs this file directly:
;;   tools\wasmtime\wasmtime.exe run --invoke count_primes examples\wasm\primes.wat 2 1000
;; Browsers need the binary .wasm.
(module
  ;; 1 when n is prime, by trial division with odd divisors
  (func $is_prime (param $n i32) (result i32)
    (local $d i32)
    (if (i32.lt_u (local.get $n) (i32.const 2)) (then (return (i32.const 0))))
    (if (i32.lt_u (local.get $n) (i32.const 4)) (then (return (i32.const 1))))
    (if (i32.eqz (i32.rem_u (local.get $n) (i32.const 2))) (then (return (i32.const 0))))
    (local.set $d (i32.const 3))
    (loop $next
      (if (i32.gt_u (i32.mul (local.get $d) (local.get $d)) (local.get $n))
        (then (return (i32.const 1))))
      (if (i32.eqz (i32.rem_u (local.get $n) (local.get $d)))
        (then (return (i32.const 0))))
      (local.set $d (i32.add (local.get $d) (i32.const 2)))
      (br $next))
    (i32.const 1))

  ;; how many primes are in [lo, hi)
  (func (export "count_primes") (param $lo i32) (param $hi i32) (result i32)
    (local $n i32) (local $count i32)
    (local.set $n (local.get $lo))
    (block $done
      (loop $each
        (br_if $done (i32.ge_u (local.get $n) (local.get $hi)))
        (local.set $count (i32.add (local.get $count) (call $is_prime (local.get $n))))
        (local.set $n (i32.add (local.get $n) (i32.const 1)))
        (br $each)))
    (local.get $count)))
