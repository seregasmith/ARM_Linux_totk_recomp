# Faster lifted game code on ARM64: plan

Status: written 2026-11-09. Done: step 1.2, `-O2` is the aarch64 default. The handheld has no `perf`, so
step 0.2 needs the Linux port of the profiler. It plans Phase 4's "FP/SIMD", "Register locals" and "Code level" items
of [ARM64_PORT_PLAN.md](ARM64_PORT_PLAN.md) in the order the measurements of
[arm64-performance.md](arm64-performance.md) suggest. Estimates below are guesses until step 0 replaces them.

## Why this now

With the ARM64 Linux defaults of 2026-11-09 (write tracking 256 / 256, the recorder on and pinned) the game's main
thread holds the frame, not the replay. Per frame on the Odin 3, at 20-46 s of game time on the test route (22.8 ms,
the scheduler lines of `TOTK_TRACE=sched`):

| Thread | Doing | ms |
|---|---|---|
| guest main `0x1001` | running game code | 10.3 |
| | waiting for the job workers (`0x1037`-`0x1039`, which run game code) | 8.9 |
| | waiting in a host call that lines up with the GPU fence waits | 2.3 |
| | waiting for the render thread | 0.5 |
| render `0x1004` | replay and other host calls | 11.9 |
| | waiting for the main thread | 10.7 |

The main thread's own game code took 10.2-10.3 ms in every setting tried; only its waits shrank. So about 19 ms of
the 22.8 ms chain is lifted game code on the main thread and the workers.

- **Ceiling.** Game code 10% faster takes about 1.9 ms off the chain (about +4 fps at 44 fps), 20% about 3.8 ms.
- **Floor.** The GPU was busy 17.5 ms a frame in the slowest stretch, so the frame stops shrinking near 17.5 ms
  (57 fps) unless the GPU's own work shrinks too.
- **The code today.** The generated C compiles at `-O1` (`tools/build_game.py` `GEN_FLAGS`; `-O2` only with `--pgo`,
  and the Linux setup has no profile). It has six register locals (`recomp/regs.py` `LOCALS`, tuned on x86-64's 16
  registers). FP/SIMD goes through the portable lowering (`runtime/simd.h`, `simd_fp.h`), with every V register in
  memory (`c->v[n]`). The code level is `oryon` on the Odin when the setup's host check picks it. A 1.4.3 aarch64 build
  of 2026-11-05 has 180 MiB of `.text`.

## Rules for every step

- **Only ARM64 Linux changes.** New generator options and compiler flags get a per-architecture default; x86-64 and
  Windows builds stay as they are unless measured there. The setup generates the C on the player's machine, so the
  generated C may differ per architecture.
- **Correctness gates.** These must pass on aarch64 before any fps test:
  - `tests/diff/lift.py`, `simd.py`, `atomic.py` and `func.py` against Unicorn (natively on aarch64: the Docker build
    on an Apple Silicon Mac, or `qemu-aarch64`);
  - `tests/jumptables_check.py`;
  - `--selftest` and the probes;
  - shots of a scene where the change acts.
- **How to measure.** Whole sessions on the same route, two per setting, back to back, with `TOTK_SYSMON=1`. Compare:
  - the slowest 20 s and world 20-45 s;
  - the main thread's `run` share in the sched lines;
  - the critical path's game-code share;
  - slow frames: zero new ones.
- **Install cost.** The setup compiles the game on the handheld, so every compiler change records the compile's wall
  time and peak memory from the setup log. The release plan expects 10-20 minutes at `-O1` on eight phone cores.
- **One change at a time.** Steps that change the generated C invalidate an execution profile (step 1.4), so PGO is
  trained last.

## Step 0: measure first

Nothing in steps 1-4 is chosen before these say where the time goes.

1. **The critical path.** Run `TOTK_CRIT=w30-40 TOTK_CRIT_OUT=<file>` in the slow stretch, then
   `tools/crit_path.py`. It gives the game code's share of the frame's chain, per thread, and replaces the "about 19 ms"
   above. One launch.
2. **A sampling profile on the device.**
   - The Odin 3's distribution has no `perf` (checked 2026-11-09), so port `runtime/prof_win.c` to Linux
     (`prof_linux.c`, behind the same `TOTK_PROFILE` switches and `prof.h`). A sampler thread wakes every 1 ms and sends each thread of `/proc/self/task` a real-time signal with
     `tgkill`. It must be another signal than `SIGRTMIN + 4`, which the asynchronous preemption uses
     (`linux.c` `plat_intr_self`). The handler stores `uc_mcontext.pc` and, for chains, the frame-pointer chain into a
     per-thread ring, with no locks or allocation. The sampler drains the rings into the profile format that
     `tools/prof_report.py` reads with `totk.map`.
   - Chains need frame pointers in the generated code: a profiling build with `-fno-omit-frame-pointer`.
3. **The code's shape.** For the 30 hottest functions of the profile, compare the host code (`llvm-objdump` of
   `totk`) with the guest code:
   - host instructions per guest instruction;
   - loads and stores of the `Cpu` (`c->x`, `c->v`, `c->nzcv`);
   - FP/SIMD helpers inlined, and their NaN and flush branches;
   - spills around calls.

   A small `tools/code_shape.py` reading the map, the binary and the lifted C does it. This decides between steps 2
   and 3.
4. **The game's FPCR.** Log the values guest code writes with `MSR FPCR` once each (FZ, DN, RMode, AHP). Step 3
   depends on them.

The results go into [arm64-performance.md](arm64-performance.md).

## Step 1: compiler and link only (no generator change)

1. **Check the code level.** The setup's log should show `oryon` on the Odin (`installer/steps.py` `cpu_level`). If
   it picked `a64`, fix the host check first.
2. **`-O2` for all generated code.**
   - Done: `build_game.py --gen-opt O1|O2` (`GEN_OPT_DEFAULT`: `-O2` on aarch64, `-O1` on x86-64), the setup's
     `--gen-opt` or `TOTK_GEN_OPT`, and `build_docker.sh`'s `TOTK_GEN_OPT`. The setup log names a level asked for, and
     its compile phase prints the wall time and peak memory.
   - Result (2026-11-09, one pair, `-O2` then `-O1`, back to back): over 25-55 s of the captures, where both ran at the
     same clock limits, the main thread used 10.6 against 11.2 ms of CPU per frame and the busiest worker 9.9 against
     10.5 ms. The fps was 41.4 against 40.1. The `-O1` run throttled later (performance cores capped at 1997 MHz), so
     its slowest 20 s do not compare. The setup's compile time at `-O2` was not recorded.
   - Made the aarch64 default on that pair.
   - Cost: compile time and memory on the device (`JOB_MEM` was sized at `-O1`: 120-180 MiB per chunk, 568 MiB for
     the main table).
   - Expected: unknown alone. On x86-64, `-O2` with PGO gave 12-15% in the fight bench.
   - If the whole build is too slow on the device: `-O2` only for the chunks holding the profile's hot functions.
3. **Hot code together.** 180 MiB of `.text` with the hot set scattered over it costs iTLB and i-cache misses. The
   profile of step 0.2 gives the hot functions.
   - Change: the generated code with `-ffunction-sections`, and an lld `--symbol-ordering-file` that puts them first.
     The file is per game version and generator, shipped like the PGO profiles in `data/`.
   - Risk: none to behaviour.
   - Gate: fps, and the iTLB and L1i misses where a machine with `perf` runs the same build (the Odin has none).
   - Further option, only if iTLB misses are high: back the hot range with huge pages at start.
4. **PGO on aarch64.** The x86-64 Windows release already ships profiles (`data/pgo-<ver>.profdata`,
   [building.md](building.md) "Profile-guided build"); the Linux setup compiles without one (`installer/layout.py`
   `pgo_dir`).
   1. Check how much of an existing profile still matches the aarch64 build. Functions are matched by name and
      control-flow hash, so those inlining architecture-specific `simd.h` code will not match. Count the setup's
      "no profile data" and "out of date" warnings. This is cheap.
   2. Otherwise train on the device:
      - an instrumented aarch64 build, which needs compiler-rt's profile library for aarch64 in the training
        toolchain, not in the setup;
      - a periodic counter write in `linux.c`, as `win32.c` does every minute (today only `plat_exit` dumps);
      - 20-30 minutes of varied play, then `tools/pgo.py merge`.
   3. Ship `pgo-v143-aarch64.profdata` and let the Linux setup use it.

   This is done last, after the generator changes. Profile-driven function order is an alternative to 1.3.

## Step 2: register locals (generator)

1. **More X registers on aarch64.**
   - Why: AArch64 has about 28 usable general registers against x86-64's 16; `LOCALS = 6` was the x86-64 optimum.
   - Sweep: `gen.py --reg-locals` at 6, 8, 10, 12 and 16 over the 300 hottest functions, counting instructions and
     memory operands as the `LOCALS` comment did on x86-64. Then fps for the best two.
   - Change: a per-architecture default in `recomp/regs.py`.
   - Gates: `tests/diff/func.py` and `lift.py`.
2. **V registers in locals (new).**
   - Why: every FP/SIMD operand is loaded from and stored to `c->v[n]`, and under `-fno-strict-aliasing` each guest
     store forces those loads again.
   - Change: keep a function's most used V registers in vector locals, with the plan of `recomp/regs.py`: sync before
     calls, SVCs, traps, returns and tail exits.
   - After a call: AAPCS64 preserves only the low 64 bits of V8-V15, so reload V0-V7, V16-V31 and the high halves of
     V8-V15.
   - `recomp/simd_c.py` then emits lane accesses on the vector locals (`v[i]`) instead of casts of `&c->v[n]`. The
     textual rewrite of `regs.py` assumes nothing takes a register's address, which `simd_c.py` does today.
   - Ceiling: from step 0.3's count of `c->v` loads and stores in the hot functions. It is worth the work only if
     FP/SIMD is a large part of them.
   - Gates: `tests/diff/simd.py` and `func.py`.

## Step 3: FP/SIMD on the hardware (a NEON backend of `simd.h`)

On an AArch64 host, an FP instruction run with the guest's FPCR gives the guest's result bit for bit, NaN propagation
included. Today `rt_fpcr_sync` sets only the host's FZ (DN off, rounding to nearest). `simd_fp.h` then computes NaN
propagation, default NaN, flushing and directed rounding in C around each operation.

1. **FPCR.** Whenever guest code runs, the host FPCR gets the guest's FZ, DN, RMode and AHP. `rt_fpcr_sync` and its
   callers already switch it where the guest's mode or the FP context changes.
   - Host calls run with the guest's FZ today. If step 0.4 finds the game using DN or a directed rounding mode for
     long, host calls switch to the default mode and back.
   - That costs one `msr fpcr` each way: measure it, as host calls are thousands a frame.
2. **Backend.** Each helper becomes its instruction through `arm_neon.h`:
   - `vaddq_f32` and `vfmaq_f32` for arithmetic;
   - `vminq_f32` / `vmaxq_f32` for FMIN / FMAX (not C `fminf`, whose NaN rules are FMINNM's);
   - `vcvtq_s32_f32` for saturating conversions;
   - `vrecpeq_f32` / `vrsqrteq_f32` for ARM's estimate tables;
   - `vrndxq_f32` for FRINTX.

   Plain C operators stay only where clang emits exactly the instruction. The portable code remains for x86-64.
3. **Gates.**
   - `tests/diff/simd.py` over the whole FP/SIMD encoding space, natively on aarch64, bit-exact against Unicorn.
   - Shots of a physics-heavy and an effects-heavy scene.
4. **With step 2.2** the FP code ends up close to native: values in registers, one instruction per operation.

Not chosen: the lifter's `simd='asm'` passthrough (LAS Recomp, `recomp/lifter.py` `emit_asm_batch`). It is
bit-exact, but each batch goes through `c->v[]` in memory, the optimiser cannot see into it, and it breaks the
register locals.

## Step 4: what the audit turns up

Placeholders, each needing step 0.3's numbers first:

- **Calls between chunks.** These are never inlined; ThinLTO costs compile time on the device.
- **Exclusive load/store pairs** in hot locks: the `ldx`/`stx` helpers of `runtime/cpu.h`.
- **Spin waits** (`rt_spin_gate`), already on the Phase 4 list.
- **Indirect calls** through `rt_dispatch`: one range check and one load.

## Order and rough value

| Step | Work | Risk | Guess at the gain, before step 0 |
|---|---|---|---|
| 0 measure | 1 launch (crit); 1-2 days for `prof_linux.c` | none | decides the rest |
| 1.2 `-O2` | a flag, and setup timing | install time | 0-10% |
| 1.3 hot code together | small, and a data file per version | none | 0-5%, more if iTLB misses are high |
| 2.1 more X locals | a sweep and a default | low | 2-5% |
| 3 NEON backend | medium | FP exactness (tests gate it) | depends on the FP share |
| 2.2 V locals | large | medium | depends on the FP share |
| 1.4 PGO on aarch64 | training on the device, shipping a profile | profile drift | x86-64 saw 12-15% |

The first useful milestone is step 0 plus 1.2 and 1.3: no generator change, one or two sessions of tests.
