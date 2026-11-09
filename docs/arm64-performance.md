# ARM64 performance: what holds the frame on the handheld

Status: written 2026-11-05, updated 2026-11-09 after the write-tracking and placement tests and the new ARM64 Linux
defaults. Everything under "Suspects" is a hypothesis
drawn from the code, the bring-up log and that capture. Replace each one with what the session profiler (below) shows,
and move the ones it rules out to "Ruled out" with the reason.

The port is measured and tuned at the handheld's default settings: the CPU governor and clock limits the system sets,
which every other game on the device shares. Changing them is out of scope. The profiler only reads them.

## The observation

On the AYN Odin 3 (Snapdragon 8 Elite, Adreno 830, 16 GB; Mesa 26.2.4 Turnip; kernel 7.2.6 built without
`CONFIG_USERFAULTFD`), with the scene at 1600x900 and FSR 1 to 1080p, the game runs at 28 fps when there are far
objects on the horizon and up to 48 fps elsewhere. The GPU is about 70% busy throughout.

Below 60 fps with the GPU 70% busy, the GPU sits idle for about 30% of every frame. Two things can cause that:

- **The GPU waits for the CPU side.** A frame's CPU side is a chain, and the GPU gets its work only at the end of
  it:
  1. The guest threads run on three virtual cores (`runtime/kernel.c`, `NCORE`).
  2. The NVN command stream is replayed into Vulkan (`runtime/vk_backend.c`).
  3. The shadow pools are copied at each submit (`runtime/vk_memory.c`).
  4. The work is submitted.

  A far view adds actors and draws, and every link of the chain grows with them.
- **A governor lowers a clock.** If a clock governor lowers the GPU's clock, the busy share is that of a slower
  clock. The first capture rules this out: the GPU sat at its top clock.

The in-game overlay cannot tell the two apart on this device. On Linux its GPU percentage comes from amdgpu's
`gpu_busy_percent` (`runtime/platform/linux.c` `plat_gpu_load`), which the Adreno does not have. The session profiler
measures the game's own GPU time instead, from its DRM client (`drm-engine-gpu` in `/proc/<pid>/fdinfo`).

## First capture (2026-11-06)

This was a session from Steam's gaming mode with `TOTK_SYSMON=1`, game 1.4.3. The sampler of that day waited for
`logs/totk.log`. Under Steam the game's stderr is a file, so the game writes its log there and opens no
`logs/totk.log` (`runtime/main.c` `log_open`). The sampler gave up after two minutes, and the capture holds only
4 samples (1.6 s, while the game was being quit).

A second session, with `TOTK_REPLAY_ASYNC=1`, ended before those two minutes and left no capture. Its frame rate did
not change, as the player saw it (see "Ruled out").

The sampler now starts at the game's exec and follows the log to the stderr file. The fixed facts of the capture
still count.

- **The default clock limits.** `scaling_max_freq` was set before the game started:
  - CPUs 0-5: 2746 of 3533 MHz (78%);
  - CPUs 6-7: 3072 of 4090 MHz (75%).

  The cpufreq cooling devices (`cpufreq-cpu0`, `cpufreq-cpu6`) stayed at 0, so this is the system's setting, not
  temperature.
- **The governor is `conservative`** on both policies. It raises a cluster's clock only under sustained load.
  - CPUs 0-5 sat at their 2746 MHz limit.
  - The two prime cores, less loaded, were sampled at 1402, 2246 and 3072 MHz. A p44 thread that ran on them averaged
    2030 MHz, below the other cluster.
  - The second capture shows both clusters at their limits once the game plays.
- **The prime cores top out at 4090 MHz** on this kernel, not the 4.32 GHz of the chip's specification.
- **The GPU runs at its top clock.** The GPU devfreq governor is `simple_ondemand` with a top clock of 1100 MHz, and
  every sample and the time-in-state table show it there. The busy share is that of the top clock.
- **Temperatures:** CPU zones 75-81 C, GPU zones 68-73 C, the fan at state 3 of 7, no CPU or GPU cooling device
  active.
- **CPU time per thread since the start** (about 131 s, loading included):

  | Thread | CPU time | Share of the session |
  |---|---|---|
  | `g1004 p30` | 76 s | 58% |
  | `guest main` | 45 s | 34% |
  | the five p44 threads 0x1037-0x103b (likely the job workers) | 23-40 s each | 18-30% |
  | `vk recorder` | 26 s | 20% |

  Placement samples put these threads mostly on CPUs 0-5. Which guest thread submits the frames needs the sched lines
  of a complete log.

## Second capture (2026-11-06, `TOTK_REPLAY_ASYNC=1`)

The fixed sampler ran for 81 s: 158 samples, game 1.4.3.

- **Where the log went.** The game's log was found on its stderr:
  `~/.local/share/armada/logs/steam-launch-<date>.log`. The launcher of the handheld's image writes one such file per
  Steam start, and every game's output goes into it.
- **Steam ended the sampler right after the game exited.** It was still waiting to copy the log and make the report,
  so neither exists. Since then, two changes keep that from losing data:
  - the sampler follows the log into its copy while the game runs (`tail --pid`);
  - the next launch makes the reports that are missing.
- **No frame rates.** Without the log, the capture has no frame rates.

Steady play, from 25 s to 79 s:

| Thread or device | Load |
|---|---|
| GPU | busy 61-67% of the time, at 1100 MHz |
| `vk replay` (the replay's own thread in this mode) | 63-69% of a core; 70% of its samples on the prime cores |
| `guest main` | 32-41% |
| the five p44 workers | 27-38% each |
| `vk recorder` | 23-30% |
| `g1004 p30` | 1% |

- Both clusters sat at their limits (2746 and 3072 MHz) throughout play. The busy threads averaged 2540-2720 MHz.
- The host CPUs were 35-46% busy.

What it says:

- **`g1004 p30` submits the frames.** With the replay moved to its own thread, its share of the session went from
  58% (first capture) to 1%, and `vk replay` took that work over. In the default mode the replay runs on `g1004 p30`
  and costs about two thirds of a core in play.
- **A chain of waits holds the frame, not one thread or the GPU.** (The third capture, with the log, corrects this:
  the replay holds the frame, wherever it runs.) No thread is near a full core. The GPU is idle a
  third of the time. Moving the replay off the game thread did not change the frame rate. What remains is a chain of
  hand-offs between the game's threads, the replay, the recorder and the GPU, where each waits for the one before. The
  sched lines show it: the render and main threads' longest waits, by object and by the thread whose signal ended
  them. `TOTK_CRIT` gives the whole chain of one frame.
- **The governor does not lower the clocks in play.** Both clusters stay at their limits, so placement matters only
  for the 12% between them.

With the Steam launch log, the report has the frame rates of this session too. Its slowest 20 s ran at 32.8 fps,
and the third capture explains them.

## Third capture (2026-11-06, the recorder A/B)

This session ran on the build with the Steam entry's profiler, game 1.4.3: 136 s, 263 samples, with the game's log
aligned. Its launch options were `TOTK_SYSMON=1 TOTK_AB=20 TOTK_VK_RECORDER=ab`. The replay ran on the submitting
thread (the default).

**The recorder A/B is inconclusive.** `tools/ab_summary.py --skip 30` gives 40.9 fps with the recorder off and 38.4 fps
with it on (+6.5% frame time). But the play held one cycle only. The two "on" blocks came first and covered the
heaviest part of the run (40-60 s); the "off" blocks came later. A repeat with `TOTK_AB=5`, standing at one spot for
two minutes, would settle it. Either way the recorder is not the main cost.

**The submitting thread holds the frame.** In the slowest windows (frames 1800-2160, 23-31 fps), the sched lines show:

- the render thread `0x1004` (`g1004 p30`) in its host call, the replay, 79-83% of the time, and waiting for the main
  thread the rest;
- the main thread waiting 19-28% of the time for a lock the render thread holds.

At about 40 fps the two even out: the render thread is in the replay 66-70% of the time and waits for the main
thread 29-33%. `g1004 p30` ran on the prime cores 93% of the time, at 3008 MHz on average.

**Two costs in the replay grow with the far view, and both come from the 2 MiB write tracking.** Per frame, from the
log's `mem: shadow` and `texture re-uploads` lines:

| Where | Frame | Replay | Shadow copies | Texture re-uploads |
|---|---|---|---|---|
| the 60 fps stretches | 16.7 ms | 0.4-3 ms | 5-15 MiB, 0.5-1.6 ms | 1-2 textures, 2-8 MiB, 0.2-0.7 ms |
| the far view | 23-39 ms | 15-31 ms | 73-100 MiB, 5.3-6.7 ms | 17-25 textures, about 32 MiB, 4.5-4.9 ms |

- **Together the two take 10-11.5 ms of each slow frame**, on the thread that holds it.
- **The GPU needs 13.6-17.4 ms per frame there.** It waits 9-13 ms a frame for the next submission.
- **Most of the tracked memory counts as written.** The write watch reports about 354 regions (708 MiB) a frame as
  written, 239 of them in their cooldown (`runtime/memory.c` `region_written`). A region written anywhere counts as
  written whole, so the pools in it are copied again and the textures in it re-uploaded, changed or not.
- **The render thread's `texture` share is these re-uploads.** It is blocked on textures 15-20% of the time in the
  far view, which matches the re-upload time.
- **The second capture shows the same.** With the replay on its own thread, its slowest 20 s had 7.2 ms of shadow
  copies (90 MiB) and a `texture` share of 17%.

Other facts of the capture:

- **New areas add pipeline builds.** The render thread's `pipeline` share was 13-49% in the first minute in a new
  area, and 0% after.
- **The prime cores reached 98-100 C.** The `cpufreq-cpu6` cooling device went up to state 4 of 15, so in a long
  session temperature starts to lower their clock.

## Write tracking tests (2026-11-09)

Six sessions back to back, game 1.4.3, each at the far view; the reports' slowest 20 s of play. The recorder is on by
default on Turnip.

| Blocks KiB / cooldown ms | Recorder | fps | Frame ms | Replay ms | Shadow copies | Texture re-uploads a frame | Write faults a frame |
|---|---|---|---|---|---|---|---|
| 2048 / 1024 (default) | on | 30.7 | 32.5 | 21.3 | 8.1 ms (113 MiB) | 52 | 7 |
| 2048 / 1024 (default) | off | 34.6 | 28.9 | 21.1 | 7.3 ms (111 MiB) | 53 | 4 |
| 256 / 256 | on | 35.1, 36.3 | 28.5, 27.6 | 19.6, 16.4 | 4.9, 4.8 ms (60 MiB) | 29, 25 | 24, 25 |
| 256 / 256 | off | 38.4 | 26.1 | 16.7 | 3.9 ms (57 MiB) | 19 | 21 |
| 64 / 64 | on | 37.3 | 26.8 | 16.1 | 3.7 ms (41 MiB) | 13 | 107 |

- **Smaller blocks and a shorter cooldown halve the copies and the re-uploads.** Against the default with the same
  recorder setting, 256 / 256 gave +3.8 fps with the recorder off, and +4.4 to +5.6 with it on.
- **64 / 64 is close to 256 / 256,** with five times the write faults: 107 a frame did not show in the frame rate.
  The faults land on the game threads that write, which have room (1.7-2.0 of 3 virtual cores busy). The copies land
  on the render thread, which holds the frame.
- **The recorder off was faster in both pairs:** +2.1 to +3.3 fps at 256 / 256, +3.9 at the default. With the
  recorder, the render thread ran on the prime cores 34-92% of the time (2.8-3.0 GHz); without it, 98-100%
  (3.04-3.06 GHz).
- **The default with the recorder on ran fifth in a row, and hot.** The prime cores reached 101 C, and the cpufreq
  cooling devices went to 6 and 7 of 15, so part of its loss is temperature.
- **One session per setting.** The slowest 20 s fell at different places in each, so differences of 1-2 fps are within
  the noise.

## Suspects

Most likely first. "Shows as" is what the session profiler's report and the log say when the suspect is the cause.

1. **Write tracking per 2 MiB region: shadow copies and texture re-uploads.**
   - Measured in the third capture: 10-11.5 ms of the slow frames, on the render thread, which holds the frame there.
   - Cause: the kernel has no userfaultfd, so `runtime/memory.c` tracks guest writes per 2 MiB region (`FRZ_SHIFT`,
     the same unit as the frozen regions). Any write in a region marks all of it.
   - Shows as: the log's `mem: shadow ... MiB copied in` and `texture re-uploads` per frame; the render thread's
     `texture` share.
   - Fix, in the game: a finer unit than 2 MiB, or page tracking by write faults (`mprotect` and the ESR path of
     Phase 2; Phase 3 step 6 of `docs/ARM64_PORT_PLAN.md`). `TOTK_WW_REGION_KB` (64 to 2048; default 256 on ARM64 Linux,
     2048 elsewhere) does the first: a write fault thaws only its block of the region, and the region counts as written in those blocks
     (`runtime/memory.c` `thaw_block_locked`, `region_written`). Smaller blocks report less, but each block's first
     write after a freeze costs a fault. Skipping the re-upload of a texture whose content did not
     change is a second line of defence. A kernel built with `CONFIG_USERFAULTFD` and `CONFIG_PTE_MARKER_UFFD_WP`
     would give page tracking as it is, but that is the device's kernel, not the game.
2. **The rest of the replay.** Without those two costs the replay still took 5-20 ms of the slow frames. That is the
   translation of the NVN command stream into Vulkan, which grows with the draws of a far view.
   - Shows as: the log's `replay avg` against the shadow and re-upload times.
   - Fix: a profile of the replay (`perf record` on the device), then the hot paths.
3. **The main thread and its workers.** At about 40 fps the render thread waits for the main thread a third of the
   time. The main thread waits for the p44 workers (`0x1037`, `0x1038`), and for a host call
   (`exe+0xc649360`, 15-20%).
   - Shows as: the sched lines' "render wait" on the main thread and "main wait" entries.
   - The lifted code's speed is part of this: `-O1` until 2026-11-09, `-O2` since (`tools/build_game.py`
     `GEN_OPT_DEFAULT`; the oryon code level, no PGO on aarch64), six register locals (`recomp/regs.py` `LOCALS`) and the portable FP/SIMD lowering
     (`runtime/simd.h`). The fixes are those of Phase 4: a NEON backend of `simd.h`, more register locals, `-O2` and
     a profile. Since the 2026-11-09 defaults this suspect holds the frame; the plan is in
     [arm64-guest-code.md](arm64-guest-code.md).
4. **Pipeline builds in new areas.** They add 13-49% of the render thread's time for a minute after arriving
   somewhere new. The pipeline cache keeps them for the next visit.
5. **Where the critical threads run.** `TOTK_ONE_PER_CORE` (`runtime/perf.c` `cpu_placement`) is for SMT and does
   nothing here. The render thread ran on the prime cores 93% of the time in the third capture. In the same-path pair
   it was 95% without the recorder and 51% with it ("Open" below).
   - Test: `TOTK_PIN_RENDER` (`runtime/perf.c` `perf_pin_frame`), "Tests for suspect 5" below. The game restricts
     its own threads (`plat_thread_affinity`), only while it runs. No system setting changes. This is the first step
     of the "Cores" item of Phase 4 (`docs/ARM64_PORT_PLAN.md`).
6. **The GPU's own time.** Look here once the GPU is busy 90% or more; it was 54-62% in the slow stretches. Turnip
   chooses GMEM (tiled) or sysmem rendering per render pass, and a pass split by a barrier or a copy stores and
   reloads its tiles. Mesa's u_trace (`MESA_GPU_TRACES`, `MESA_GPU_TRACEFILE`; see Mesa's `docs/envvars.rst`) times
   each render pass on Turnip.

### Tests for suspect 1

Launches at the far view, each with `TOTK_SYSMON=1`. Compare the reports' `shadow copy ms (MiB)`, the log's
`texture re-uploads` and the frame rate:

1. `TOTK_SHADOW=0`: no shadow copies; the GPU reads guest memory itself. It needs `VK_EXT_external_memory_host`
   (`vulkaninfo | grep external_memory_host`); without it the game stops at its Vulkan check.
2. `TOTK_FREEZE_IDLE=8` (default 60): idle regions are frozen after 8 submissions instead of 60, so fewer count as
   written.
3. `TOTK_WW_REGION_KB=256`, then `64`: blocks of 256 or 64 KiB in place of the 2 MiB region. More write faults
   (`thaws by a write` in the log's `mem: write-watch` lines) are the cost to watch.
4. `TOTK_WW_COOL_MS=128`, then `32` (1024 at the time, now 256 on ARM64 Linux): the longest a region found written stays thawed. While thawed it
   counts as written at every query (239 of the 354 regions reported a frame in the third capture were in that
   cooldown), so a shorter cap reports less, and a region the game writes every frame takes a write fault and a
   protection change more often. Also together with `TOTK_WW_REGION_KB=256`.

   The idle freeze of test 2 does little under region tracking: a thawed region reports all its pages at every query,
   so it never looks idle, and region_written refreezes it after its cooldown instead.

### Tests for suspect 5

`TOTK_PIN_RENDER` keeps the thread that replays on the fastest cores: those with the largest `cpu_capacity`, 6-7 on
the Odin 3. That thread is `g1004 p30`, or the replay thread with `TOTK_REPLAY_ASYNC=1`.

| Value | Pinned |
|---|---|
| `0` | nothing |
| unset | `2` on ARM64 Linux when at least two cores are the fastest, else nothing ("Defaults on ARM64 Linux") |
| `1` | the thread that replays |
| `2` | that thread and the recorder thread (same as `1` with `TOTK_VK_RECORDER=0`) |
| `ab` | `1` in the B blocks of `TOTK_AB`, nothing in the A blocks |

The log names the cores: `cpu: TOTK_PIN_RENDER=1: the thread that replays on processors 6-7, the fastest of 8`.

Launches back to back on the route of the same-path pair, each with `TOTK_SYSMON=1`, `TOTK_WW_REGION_KB=256` and
`TOTK_WW_COOL_MS=256`:

1. `TOTK_VK_RECORDER=0`: the faster run of the pair, the reference.
2. `TOTK_VK_RECORDER=1 TOTK_PIN_RENDER=1`.
3. `TOTK_VK_RECORDER=1 TOTK_PIN_RENDER=2`.
4. `TOTK_VK_RECORDER=0 TOTK_PIN_RENDER=1`.

In the report, `g1004 p30` should be `on prime` nearly 100% of the time. Its `waitCPU%` is the cost: the time it was
ready while other threads held both prime cores. `ab` (with `TOTK_AB=20`) is for a static scene only, standing still.

**Results (2026-11-09).** The four launches, one session each, in this order. "World 20-50 s" averages the log's
frame windows by the game's world clock, where the runs follow the same route.

| Run | Slowest 20 s | World 20-50 s | Replay, slowest 20 s | `g1004 p30` on prime, MHz | `vk recorder` on prime, MHz |
|---|---|---|---|---|---|
| `VK_RECORDER=0` | 37.9 fps (26.4 ms) | 42.1 fps | 16.8 ms | 92%, 3007 | none |
| `VK_RECORDER=1 PIN_RENDER=1` | 39.1 fps (25.6 ms) | 40.7 fps | 13.0 ms | 100%, 3000 | 26%, 2758 |
| `VK_RECORDER=1 PIN_RENDER=2` | 39.7 fps (25.2 ms) | 42.6 fps | 12.9 ms | 100%, 3057 | 100%, 3057 |
| `VK_RECORDER=0 PIN_RENDER=2` (same as `1`) | 38.1 fps (26.3 ms) | 39.7 fps | 17.3 ms | 100%, 3038 | none |

- **The pin holds.** The thread that replays stayed on cores 6-7, and it waited for a CPU 1% of the time, as before.
  The pinned recorder waited 4%.
- **Without the recorder it changes nothing.** The render thread was already on the prime cores 92-95% of the time.
- **With the recorder it turns the loss of the same-path pair into a gain.** The recorder takes 2-4 ms of the replay
  off the render thread (`g1004 p30` busy 53-54% of a core against 65-67%). `VK_RECORDER=1 PIN_RENDER=2` was the
  fastest by both measures and over the whole capture (45.9 fps against 44.5-45.2), 1.8 fps over `VK_RECORDER=0` in
  the slowest 20 s. The second session below confirmed it.
- **Slow frames.** Every run had the same 4-5 at arrival. The 50-53 ms frames of the pinned runs came in their last
  seconds, while leaving; the first run ended on a different view (17-20 ms frames there).
- **What is left.** The GPU still waits 6.6 ms a frame for the next submission. The replay (12.9 ms) still holds the
  shadow copies (4.3 ms, 60 MiB) and the texture re-uploads.

### Defaults on ARM64 Linux (2026-11-09)

A second session on the same route, in this order, the last run with the old write tracking defaults:

| Run | Slowest 20 s | World 20-45 s | Replay, slowest 20 s | Shadow copies, slowest 20 s |
|---|---|---|---|---|
| 256 / 256, `VK_RECORDER=0 PIN_RENDER=1` | 38.1 fps (26.2 ms) | 41.7 fps | 16.8 ms | 4.6 ms, 67 MiB |
| 256 / 256, `VK_RECORDER=1 PIN_RENDER=2` | 39.4 fps (25.4 ms) | 43.8 fps | 12.9 ms | 4.2 ms, 59 MiB |
| 256 / 256, `VK_RECORDER=0 PIN_RENDER=0` | 38.0 fps (26.3 ms) | 40.7 fps | 17.3 ms | 4.7 ms, 68 MiB |
| 2048 / 1024, `VK_RECORDER=0 PIN_RENDER=0` | 31.9 fps (31.3 ms) | 32.2 fps | 21.9 ms | 8.1 ms, 126 MiB |

"256 / 256" is `TOTK_WW_REGION_KB=256 TOTK_WW_COOL_MS=256`; "2048 / 1024" were the defaults.

- **Write tracking.** 256 / 256 against the old defaults: +6.1 fps in the slowest 20 s, +8.5 in world 20-45 s, half
  the shadow copies. The last run was also the hottest (the prime cores' cooling device was active), but the copies
  and the replay time show the work itself shrank.
- **Placement.** The recorder pinned with the render thread against the recorder off and nothing pinned, in two
  sessions: +1.8 and +1.4 fps in the slowest 20 s, +2.5 and +3.1 fps in world 20-45 s (41.1 against 43.6 in the
  first). Pinning without the recorder changed nothing in either session.
- **Slow frames.** 20-22 in each 256 / 256 run, most of them at arrival; 38 with the old defaults, with a stretch of
  50-54 ms frames in play.

So on ARM64 Linux (`#if defined(__aarch64__) && defined(__linux__)`):

- `TOTK_WW_REGION_KB` defaults to 256 and `TOTK_WW_COOL_MS` to 256 (`runtime/memory.c` `WW_REGION_KB`,
  `RW_COOL_MAX_MS`). They only act with region write tracking, on a kernel without userfaultfd. The log line that
  turns it on gives the values in use.
- `TOTK_PIN_RENDER` defaults to 2 (`runtime/perf.c` `PIN_RENDER_DEFAULT`), when at least two cores are the fastest:
  with one, the thread that replays would share it with everything else. The log says `(default)`.
- The recorder stays on, its default on Turnip.

Elsewhere the defaults stay as they were: 2048, 1024 and 0. An x86 Linux kernel without userfaultfd also tracks per
region, and the x86 harness favoured small blocks there too, but no run in play measured it. Setting any of the three
still overrides the default, so the old behaviour is `TOTK_WW_REGION_KB=2048 TOTK_WW_COOL_MS=1024 TOTK_PIN_RENDER=0`.

### Ruled out

- **Replay on the submitting thread** (2026-11-06).
  - What it was: `vk_replay_async` (`runtime/vk_backend.c`) is off by default, so the submitting thread
    (`g1004 p30`) also replays its commands into Vulkan.
  - The test: `TOTK_REPLAY_ASYNC=1` ("GPU frame prep on its own thread" in the menu) moves the replay to its own
    thread.
  - The result: on the Odin 3 the frame rate did not change. The replay is the slowest stage wherever it runs.
- **The GPU's clock** (2026-11-06). The GPU was at its top 1100 MHz throughout play in every capture.
- **The governor lowering the CPU clocks in play** (2026-11-06). Both clusters sat at their limits throughout play in
  the second and third captures.

### Open

- **The recorder thread** (`TOTK_VK_RECORDER`, on by default on Turnip). One A/B cycle in the third capture was
  inconclusive. In the write tracking tests, off was 2-4 fps faster in both pairs.
  - **The same-path pair (2026-11-09).** Two sessions at 256 / 256, back to back, the same route and actions, with no
    cooling device active but the fan. Off was faster:
    - by 1.9 fps over the first 25 s of world time, where the runs line up (they drift apart after that);
    - by 1.4 fps in the slowest 20 s (25.8 against 26.7 ms).
  - **Why.** The difference is where the render thread runs. With the recorder, `g1004 p30` was on the prime cores 51%
    of the time at 2855 MHz on average. Without it, 95% at 3049 MHz. The recorder's own hand-offs cost little: its
    drains waited 593 ms in the 74 s session, about 0.2 ms a frame. With the recorder the replay was 0.9 ms shorter.
  - **With the render thread pinned** (`TOTK_PIN_RENDER=2`, "Tests for suspect 5" above) the recorder was the faster
    of the two in two sessions: 39.7 and 39.4 against 37.9 and 38.0 fps in the slowest 20 s. Recorder on and pinned is
    the default on ARM64 Linux now ("Defaults on ARM64 Linux").

## Profiling a session from Steam's gaming mode

The session profiler comes with the setup's install (`installer/games.py` `install_sysmon`). The install's `sysmon/`
folder, beside `totk`, holds three files:

- `launch.sh` (`tools/sysmon_launch.sh`);
- `odin_sysmon.sh`, which samples the machine;
- `sysmon_report.py`, which writes the report.

The script of the game's Steam entry, `TotK Recomp <version>` (`installer/games.py` `STEAM_SCRIPT`), sources
`sysmon/launch.sh` before it starts the game. Every install brings the script and `sysmon/` up to date, so an entry
added to Steam before runs the new ones. Set it in the game's launch options in Steam:

```
TOTK_SYSMON=1 %command%
```

Then play as usual. Stay at least 20 s where the frame rate drops (the far view) and 20 s somewhere ordinary, then
quit the game. An A/B of a game setting is a second launch with that setting added to the launch options.

The report is written a few seconds after the game exits, while Steam still shows the game as running. Steam may
end the sampler before that. The copy of the log is complete anyway, because the sampler follows the log while the
game runs. A report cut short is made at the next launch, at the lowest priority while the game loads.

Everything goes into the install's `logs/` folder, its data folder
(`~/.local/share/totk-recomp/<version>/logs` in the default games folder):

- `sysmon-<date>.txt`: the report.
- `sysmon-<date>.tsv.gz`: the capture.
- `sysmon-<date>.log.gz`: a copy of the game's log of that run.
- `sysmon.log`: the sampler's own messages, for every run.

With `TOTK_SYSMON` set, `sysmon/launch.sh` does two things before the script starts the game:

- It adds `perf,sched` to `TOTK_TRACE`, keeping the list from the environment or `totk.ini`.
- It starts the sampler with the script's pid, which becomes the game's at the `exec`.

The sampler waits for that exec, then samples every 0.5 s (`TOTK_SYSMON_INTERVAL` changes the interval) until the
game exits. At each sample it records the position of the game's log file, so the report knows which frames every
sample belongs to.

The sampler only reads `/proc` and `/sys`. It changes no governor, clock limit or other system setting, so other
games keep theirs. Its cost is a few percent of one core, more with more threads. The perf trace adds GPU timestamp
queries.

Under Steam's gaming mode the game writes its log into the file Steam gives it as stderr, not into `logs/totk.log`.
The sampler finds that file and names it in `sysmon.log` and in the report's header. It copies the file from 64 KiB
before the game's first position it saw. A shared file holds earlier runs too; the report keeps only the frames
written while the capture ran (each run starts at its `version:` line and counts frames from 1).

By hand, with the game already running, run `sysmon/odin_sysmon.sh -d 30` from the install, or `tools/odin_sysmon.sh`
from the repository (30 s, or `-d 0` until the game exits). Then run
`python3 sysmon/sysmon_report.py sysmon-<date>.tsv --log <the game's log>`.

## Reading the report

- **Timeline.** One row per 10 s (more for long runs): frame rate from the log, the GPU's busy share and clock, the
  host cores the guest threads used, the busiest thread, the hottest thermal zone. `<- slow` and `<- fast` mark the
  stretches below.
- **The slowest stretch.** The report picks the slowest 20 s of play (`--stretch`) by itself, leaving out loading
  screens and stalls (windows over 100 ms a frame or with a frame over 400 ms). `--from`/`--to` (seconds of the
  capture) choose a span instead. The full sections follow:
  - **CPUs:** class (prime, perf), governor, load, average and top clock, and the lowest upper limit seen. The hints
    say whether a limit was there from the start (the system's setting) or came during the run, and whether a cooling
    device (temperature) was active.
  - **GPU:** the devfreq clock samples and time per clock, and the busy share of the game's DRM client with its clock
    while busy.
  - **Threads:**
    - `core%` is the share of one core a thread ran.
    - `waitCPU%` is its time runnable but waiting for a CPU (`/proc/<pid>/task/<tid>/schedstat`).
    - `MHz` is the average clock of the core it was on at each sample: what its code ran at.
    - `on prime` is the share of samples on the fastest cores.
  - **Thread names:** guest threads are named `g<handle> p<priority>` (`runtime/kernel.c` `kern_svc`, CreateThread),
    so they match the sched lines; the main thread is `guest main` (0x1001). Host threads keep their own names:
    `vk replay`, `vk recorder`, `vk present`, `pipe worker`, `xlate`, `audren`, ...
  - **Thermal zones and cooling devices.**
  - **The log's numbers for the same frames:**
    - the frame interval;
    - the render thread's waits and busy time, and the replay time;
    - the GPU's busy time from timestamps, and its idle time split into waiting for the next submission and idle
      with work queued;
    - the shadow copies;
    - the guest scheduler's virtual cores and its busiest guest threads.
- **Side by side.** The slowest stretch against the fastest one and the whole capture. What grows from the fast
  column to the slow one is what the far view costs.
- **Hints.** The rules above, applied to the slowest stretch.

Report a run with its `sysmon-<date>.txt`; attach the `.tsv.gz` and `.log.gz` when they are needed for another span.

## Not covered yet

- The function-level sampling profiler `TOTK_PROFILE` is Windows-only (`runtime/prof_win.c`). On Linux,
  `runtime/perf.c`'s weak `prof_sampler_*` stubs only log that there is none. When the report points at a CPU
  thread, the next step is `perf record` on the device (the Docker build does not strip `totk`, and `totk.map` is
  written at the link) or a Linux backend of the sampler for `tools/prof_report.py`.
- The critical path per frame (`TOTK_CRIT` with `TOTK_TRACE=sched`, `tools/crit_path.py`) runs on Linux as it is.
  Use it once the report has named the thread that holds the frame.
