"""Report for a tools/odin_sysmon.sh capture: where the CPU and GPU time of a totk run went, and what holds the frame.

    python3 tools/sysmon_report.py sysmon-<date>.tsv[.gz] [--log totk.log[.gz]] [--from S --to S] [--stretch 20]
                                   [--bucket 10] [--windows 8] [--top 20]

With the game's log of the same run (TOTK_TRACE=perf,sched) and a capture that recorded the log's position (the L
records, which odin_sysmon.sh writes once it finds the game's log open: logs/totk.log, or the file its stderr goes to),
every sample gets its frame. The report then shows:

- a timeline of the session in --bucket second steps: frame rate, GPU busy share and clock, the busiest thread;
- the slowest and the fastest --stretch seconds of play (loading screens and stalls left out), found by themselves,
  or the span --from/--to (seconds from the capture's start) in place of the slowest;
- for the slowest stretch the full sections: the CPUs (load, clock, limits), the GPU (clock, the busy share of the
  game's DRM client), the game's threads (share of a core, time waiting for a CPU, how much of it on the fastest
  cores), thermal zones and cooling devices, and the log's numbers for the same frames: frame interval, GPU busy and
  idle, fence waits, replay time, shadow copies and the guest scheduler's view of its three virtual cores;
- slowest against fastest side by side, then hints.

Without the alignment (a capture taken by hand without the log, or a log alone) it reports the whole capture and the
last --windows perf windows of the log. docs/arm64-performance.md says how to read it. Standard library only: it runs
on the handheld as well as on the development machine.
"""
import argparse
import bisect
import gzip
import re
import statistics
import sys
from collections import Counter, defaultdict

SERIES = ('stat', 'freq', 'devfreq', 'drm', 'thr', 'zone', 'cool', 'pmax')
GUEST = re.compile(r'^(g[0-9a-f]+ p\d+|guest|guest main)$')


def open_text(path, binary=False):
    if str(path).endswith('.gz'):
        return gzip.open(path, 'rb' if binary else 'rt', **({} if binary else {'encoding': 'utf-8', 'errors': 'replace'}))
    return open(path, 'rb') if binary else open(path, encoding='utf-8', errors='replace')


def pct(a, b):
    return 100.0 * a / b if b else 0.0


def mean(v, default=0.0):
    v = list(v)
    return statistics.mean(v) if v else default


def kv(text):
    return dict(x.split('=', 1) for x in text.split() if '=' in x)


# ---- the capture

def parse_capture(path):
    d = {'meta': defaultdict(list), 'times': [], 'logpos': [], 'kgsl': [], 'names': {},
         'trans': defaultdict(lambda: defaultdict(list))}
    for s in SERIES:
        d[s] = defaultdict(list)
    t = None
    with open_text(path) as f:
        for line in f:
            p = line.rstrip('\n').split('\t')
            k = p[0]
            try:
                if k == 'M':
                    d['meta'][p[1]].append(p[2] if len(p) > 2 else '')
                elif k == 'X':
                    d['trans'][p[2]][p[1]].append('\t'.join(p[3:]))
                elif k == 'T':
                    t = int(p[1])
                    d['times'].append(t)
                elif t is None:
                    continue
                elif k == 'S':
                    d['stat'][int(p[1])].append((t, int(p[2]), int(p[3])))
                elif k == 'F':
                    d['freq'][int(p[1])].append((t, int(p[2])))
                elif k == 'G':
                    if p[2]:
                        d['devfreq'][p[1]].append((t, int(p[2])))
                elif k == 'K':
                    d['kgsl'].append((t, int(p[1] or 0), int(p[2] or 0), int(p[3] or 0)))
                elif k == 'D':
                    s = d['drm'][p[2] or 'fd' + p[1]]
                    if not s or s[-1][0] != t:   # one client seen through two fds counts once
                        s.append((t, int(p[3]), int(p[4] or 0)))
                elif k == 'L':
                    d['logpos'].append((t, int(p[1])))
                elif k == 'H':
                    tid = int(p[1])
                    d['thr'][tid].append((t, int(p[2]), int(p[3] or 0), int(p[4] or 0), int(p[5])))
                    d['names'][tid] = p[6] if len(p) > 6 else '?'
                elif k == 'Z':
                    d['zone'][int(p[1])].append((t, int(p[2])))
                elif k == 'C':
                    d['cool'][int(p[1])].append((t, int(p[2])))
                elif k == 'P':
                    d['pmax'][int(p[1])].append((t, int(p[2])))
            except (ValueError, IndexError):
                continue   # a record cut short (the game exited mid-sample)
    return d


def cut(series, t0, t1):
    """The records of a time-sorted series (tuples that start with the time) in [t0, t1]."""
    return series[bisect.bisect_left(series, (t0,)):bisect.bisect_left(series, (t1 + 1,))]


def sliced(d, t0, t1):
    s = dict(d)
    s['times'] = d['times'][bisect.bisect_left(d['times'], t0):bisect.bisect_right(d['times'], t1)]
    for k in SERIES:
        s[k] = {key: cut(v, t0, t1) for key, v in d[k].items()}
    s['kgsl'] = cut(d['kgsl'], t0, t1)
    s['trans'] = {}   # time-in-state tables cover the whole capture only
    return s


def cpu_classes(d):
    """{cpu: 'prime' | 'perf' | 'little' | 'core'} by each CPU's top clock, and the per-CPU facts."""
    ci = {}
    for key, vals in d['meta'].items():
        m = re.fullmatch(r'cpu(\d+)', key)
        if m:
            ci[int(m.group(1))] = kv(vals[0])
    maxes = sorted({int(v.get('max_khz') or 0) for v in ci.values()}, reverse=True)
    cls = {}
    for c, v in ci.items():
        f = int(v.get('max_khz') or 0)
        if len(maxes) < 2:
            cls[c] = 'core'
        else:
            cls[c] = 'prime' if f == maxes[0] else 'perf' if f == maxes[1] else 'little'
    return cls, ci


def gpu_devfreqs(d):
    return [n for n in d['devfreq'] if 'gpu' in n.lower() or 'kgsl' in n.lower()
            or 'adreno' in (d['meta'].get(f'devfreq {n}') or [''])[0].lower()]


def devfreq_top(d, n):
    text = (d['meta'].get(f'devfreq {n}') or [''])[0]
    av = re.search(r'available=(.*)$', text)   # the last field, a list
    avail = [int(x) for x in av.group(1).split() if x.isdigit()] if av else []
    return (max(avail) if avail else int(kv(text).get('max_hz') or 0)), kv(text)


def measure(d, cls):
    """The numbers of a capture or of a slice of one."""
    m = {'span': (d['times'][-1] - d['times'][0]) / 1e6 if len(d['times']) > 1 else 0.0}
    clk_tck = int((d['meta'].get('clk_tck') or ['100'])[0] or 100)
    m['cpu'] = {}
    for c, s in d['stat'].items():
        if len(s) >= 2:
            db, di = s[-1][1] - s[0][1], s[-1][2] - s[0][2]
            fr = [x[1] for x in d['freq'].get(c, [])]
            m['cpu'][c] = (pct(db, db + di), mean(fr) / 1000, max(fr) / 1000 if fr else 0.0)
    gd = gpu_devfreqs(d)
    m['gpu_dev'] = gd[0] if gd else None
    m['gpu_mhz'] = mean(x[1] for x in d['devfreq'].get(gd[0], [])) / 1e6 if gd else None
    m['gpu_busy'], m['gpu_busy_mhz'], m['gpu_samples'] = None, None, []
    for cid, rows in d['drm'].items():
        if len(rows) < 2:
            continue
        de, dc = rows[-1][1] - rows[0][1], rows[-1][2] - rows[0][2]
        b = pct(de, (rows[-1][0] - rows[0][0]) * 1000)
        if de > 0 and (m['gpu_busy'] is None or b > m['gpu_busy']):
            m['gpu_busy'] = b
            mhz = dc / (de / 1e9) / 1e6 if dc else 0
            m['gpu_busy_mhz'] = mhz if 50 <= mhz <= 5000 else None   # msm's drm-cycles-gpu can be far off
            m['gpu_samples'] = [pct(r1[1] - r0[1], (r1[0] - r0[0]) * 1000) for r0, r1 in zip(rows, rows[1:])
                                if r1[0] > r0[0]]
    k = d['kgsl']
    m['kgsl_busy'] = pct(k[-1][1] - k[0][1], k[-1][2] - k[0][2]) if len(k) >= 2 and k[-1][2] > k[0][2] else None
    m['kgsl_mhz'] = mean(r[3] for r in k if r[3]) / 1e6 if any(r[3] for r in k) else None
    rows = []
    clock_at = {c: dict(s) for c, s in d['freq'].items()}   # cpu -> {sample time: kHz}
    for tid, s in d['thr'].items():
        if len(s) < 2:
            continue
        dt = (s[-1][0] - s[0][0]) / 1e6
        if dt <= 0:
            continue
        run_ns = s[-1][2] - s[0][2]
        cpu = run_ns / 1e9 / dt if run_ns > 0 else (s[-1][1] - s[0][1]) / clk_tck / dt
        wait = max(0, s[-1][3] - s[0][3]) / 1e9 / dt
        on = Counter(cls.get(x[4], '?') for x in s)
        moves = sum(1 for a, b in zip(s, s[1:]) if a[4] != b[4]) / dt
        # the clock of the CPU the thread was last on, at each sample: what its code ran at
        khz = [clock_at.get(x[4], {}).get(x[0]) for x in s]
        mhz = mean(k for k in khz if k) / 1000 if any(khz) else None
        rows.append((cpu, wait, tid, d['names'].get(tid, '?'), on, moves, Counter(x[4] for x in s), mhz))
    rows.sort(reverse=True)
    m['threads'] = rows
    groups = defaultdict(lambda: [0.0, 0])
    for cpu, wait, tid, name, *_ in rows:
        g = 'guest' if GUEST.match(name) else re.sub(r'[\d_-]+$', '', name)
        groups[g][0] += cpu
        groups[g][1] += 1
    m['groups'] = groups
    m['guest_cores'] = groups['guest'][0] if 'guest' in groups else 0.0
    zt = [(max(x[1] for x in v), mean(x[1] for x in v), i) for i, v in d['zone'].items() if v]
    m['zones'] = sorted(zt, reverse=True)
    m['temp_max'] = m['zones'][0][0] / 1000 if m['zones'] else None
    m['cooling'] = {i: (min(x[1] for x in v), max(x[1] for x in v)) for i, v in d['cool'].items() if v and max(x[1] for x in v) > 0}
    m['pmax'] = {p: min(x[1] for x in v) for p, v in d['pmax'].items() if v}
    return m


def cpus_of_policy(text):
    mm = re.search(r'cpus=(.*)', text)
    return {int(x) for x in mm.group(1).split()} if mm else set()


def render_detail(d, m, cls, ci, top):
    out, hints = [], []
    p = out.append
    meta = d['meta']
    p('CPU   class   governor      busy   avg MHz  max MHz  limit MHz (lowest seen)')
    for c in sorted(m['cpu']):
        busy, avg, mx = m['cpu'][c]
        v = ci.get(c, {})
        top_khz = int(v.get('max_khz') or 0)
        lim = [x for pol, x in m['pmax'].items() if c in cpus_of_policy((meta.get(f'policy{pol}') or [''])[0])]
        lim_khz = min(lim) if lim else int(v.get('scaling_max_khz') or 0)
        p(f"{c:<5} {cls.get(c, '?'):<7} {v.get('governor', '?'):<12} {busy:5.0f}%  {avg:7.0f}  {mx:7.0f}  "
          f"{lim_khz / 1000:.0f} of {top_khz / 1000:.0f}")
    # Clock caps, one hint per cpufreq policy: set before the game started or during it, and whether a cpufreq
    # cooling device (temperature) was active.
    cool_types = [kv((meta.get(f'cooling{i}') or [''])[0]).get('type', '') for i in m['cooling']]
    for key in sorted(k for k in meta if re.fullmatch(r'policy\d+', k)):
        cpus = sorted(cpus_of_policy(meta[key][0]) & set(ci))
        if not cpus:
            continue
        top_khz = max(int(ci[c].get('max_khz') or 0) for c in cpus)
        start = int(ci[cpus[0]].get('scaling_max_khz') or 0)
        seen = m['pmax'].get(int(key[6:]))
        lim = min(x for x in (start, seen) if x) if (start or seen) else 0
        if not (lim and top_khz and lim < 0.97 * top_khz):
            continue
        hot = any(re.search(rf'cpufreq-cpu{c}\b|thermal-cpufreq', t) for t in cool_types for c in cpus)
        when = 'from the start' if start and start < 0.97 * top_khz else 'during the run'
        why = ('a cpufreq cooling device was active: temperature' if hot else
               'its cpufreq cooling device stayed at 0, so not temperature: a power profile, a TDP setting or a daemon')
        hints.append(f'CPUs {cpus[0]}-{cpus[-1]}: the clock was capped at {lim / 1000:.0f} of {top_khz / 1000:.0f} MHz '
                     f'({100 * lim / top_khz:.0f}%) {when}; {why}')
    # The system's clock settings are facts to work within (other programs share them), not something to change.
    govs = {v.get('governor') for v in ci.values()}
    if 'conservative' in govs:
        hints.append('the conservative governor raises a cluster\'s clock only under sustained load: a busy thread '
                     'that moves between cores, or waits often, runs below the cluster\'s limit (the threads\' MHz '
                     'column)')

    p('')
    for n in d['devfreq']:
        vals = [x[1] for x in d['devfreq'][n]]
        if not vals:
            continue
        top_hz, mm = devfreq_top(d, n)
        tag = 'GPU ' if n in gpu_devfreqs(d) else ''
        p(f"{tag}devfreq {n}: governor {mm.get('governor', '?')}, clock avg {mean(vals) / 1e6:.0f} MHz, min "
          f"{min(vals) / 1e6:.0f}, max {max(vals) / 1e6:.0f}; top {top_hz / 1e6:.0f} MHz, limit "
          f"{int(mm.get('max_hz') or 0) / 1e6:.0f} MHz")
        hist = Counter(vals)
        p('  sampled clocks: ' + ', '.join(f'{f / 1e6:.0f} MHz {pct(c, len(vals)):.0f}%'
                                          for f, c in sorted(hist.items(), reverse=True)))
        tr = d['trans'].get(n, {}) if d['trans'] else {}
        r0, r1 = trans_residency(tr.get('start', [])), trans_residency(tr.get('end', []))
        if r0 and r1:
            dt = {f: r1.get(f, 0) - r0.get(f, 0) for f in r1}
            tot = sum(dt.values())
            if tot > 0:
                p('  time per clock, whole capture: ' + ', '.join(f'{f / 1e6:.0f} MHz {pct(v, tot):.0f}%'
                                                                   for f, v in sorted(dt.items(), reverse=True) if v > 0))
        if tag and top_hz and mean(vals) < 0.9 * top_hz:
            hints.append(f'the GPU clock averaged {mean(vals) / top_hz * 100:.0f}% of its top ({top_hz / 1e6:.0f} '
                         f'MHz): its busy share was measured at that lower clock')
        if tag and top_hz and int(mm.get('max_hz') or 0) and int(mm['max_hz']) < top_hz:
            hints.append(f"the GPU's devfreq max_freq is {int(mm['max_hz']) / 1e6:.0f} MHz, below its top clock "
                         f"{top_hz / 1e6:.0f} MHz")
    if m['gpu_busy'] is not None:
        eff = f", {m['gpu_busy_mhz']:.0f} MHz while busy" if m['gpu_busy_mhz'] else ''
        p(f"GPU busy (the game's DRM client): {m['gpu_busy']:.0f}% of the time{eff}")
        if len(m['gpu_samples']) > 4:
            q = statistics.quantiles(m['gpu_samples'], n=10)
            p(f"  per sample: p10 {q[0]:.0f}%, median {statistics.median(m['gpu_samples']):.0f}%, p90 {q[-1]:.0f}%")
    if m['kgsl_busy'] is not None:
        p(f"kgsl: GPU busy {m['kgsl_busy']:.0f}%" + (f", gpuclk avg {m['kgsl_mhz']:.0f} MHz" if m['kgsl_mhz'] else ''))
    busy = m['gpu_busy'] if m['gpu_busy'] is not None else m['kgsl_busy']
    if busy is None:
        p("GPU busy: not measured (no drm-engine-gpu in the game's fdinfo and no kgsl device)")
    elif busy >= 90:
        hints.append(f'the GPU was busy {busy:.0f}% of the time: GPU-bound; resolution, the passes and the driver '
                     f'(Turnip gmem/sysmem per pass) are where the time is')
    elif busy < 80:
        hints.append(f'the GPU was busy only {busy:.0f}% of the time: it waits for the CPU side; the threads below and '
                     f'the log\'s "waiting for the next submission" say who is late')

    p('')
    rows = m['threads']
    p(f"threads: {len(rows)}, together {sum(r[0] for r in rows):.2f} cores")
    p('  tid      name              core%  waitCPU%    MHz  on prime  moves/s  CPUs seen')
    have_prime = 'prime' in cls.values()
    for cpu, wait, tid, name, on, moves, seen, mhz in rows[:top]:
        n_on = sum(on.values())
        prime = f"{pct(on.get('prime', 0), n_on):7.0f}%" if have_prime else '      -'
        cs = ' '.join(f'{c}:{n}' for c, n in sorted(seen.items()))
        ms = f'{mhz:5.0f}' if mhz else '    -'
        p(f'  {tid:<8} {name[:16]:<16} {100 * cpu:6.0f}  {100 * wait:8.0f}  {ms}  {prime}  {moves:7.1f}  {cs}')
    p('  by name: ' + ', '.join(f'{g} {c:.2f} cores ({n})' for g, (c, n) in
                                sorted(m['groups'].items(), key=lambda x: -x[1][0]) if c >= 0.02))
    prime_lim = max((x for p_, x in m['pmax'].items()
                     if any(cls.get(c) == 'prime' for c in cpus_of_policy((meta.get(f'policy{p_}') or [''])[0]))),
                    default=None)
    for cpu, wait, tid, name, on, moves, seen, mhz in rows[:6]:
        if cpu >= 0.85:
            hints.append(f'thread {tid} "{name}" ran {100 * cpu:.0f}% of a core: one thread at its limit sets the '
                         f'frame rate (a guest thread\'s name gives its handle in the sched lines)')
        if wait >= 0.10 and cpu >= 0.3:
            hints.append(f'thread {tid} "{name}" waited {100 * wait:.0f}% of the time for a CPU while runnable: more '
                         f'busy threads than cores it may use')
        n_on = sum(on.values())
        if have_prime and cpu >= 0.6 and pct(on.get('prime', 0), n_on) < 50:
            at = f', at {mhz:.0f} MHz on average' if mhz else ''
            lim = f' (their limit here: {prime_lim / 1000:.0f} MHz)' if prime_lim else ''
            hints.append(f'thread {tid} "{name}" ({100 * cpu:.0f}% of a core) ran on the prime cores in only '
                         f'{pct(on.get("prime", 0), n_on):.0f}% of the samples{at}: the game placing it there{lim} is '
                         f'worth an A/B')
    if m['guest_cores'] >= 2.6:
        hints.append(f"guest threads used {m['guest_cores']:.2f} cores, near the three virtual cores the game gets: "
                     f"the guest scheduler (sched lines) is the limit, not the host")

    p('')
    if m['zones']:
        p('hottest zones: ' + ', '.join(f"{(meta.get(f'zone{i}') or ['?'])[0]} {mx / 1000:.0f} C (avg {av / 1000:.0f})"
                                        for mx, av, i in m['zones'][:8]))
    act = []
    for i, (lo, hi) in m['cooling'].items():
        mm = kv((meta.get(f'cooling{i}') or [''])[0])
        act.append(f"{mm.get('type', i)} {lo}..{hi} of {mm.get('max_state', '?')}")
    p('cooling devices in use: ' + ('; '.join(act) if act else 'none'))
    if any(re.search(r'cpu|gpu|devfreq', a, re.I) for a in act):
        hints.append('a CPU or GPU cooling device was active: temperature capped the clocks')
    return out, hints


def trans_residency(rows):
    """devfreq trans_stat lines -> {freq_hz: time}, the last column of each state row."""
    res = {}
    for r in rows:
        mm = re.match(r'\s*\*?\s*(\d+):.*?(\d+)\s*$', r)
        if mm:
            res[int(mm.group(1))] = int(mm.group(2))
    return res


# ---- the game's log (TOTK_TRACE=perf,sched)
PERF1 = re.compile(r'perf: frame (\d+): interval avg ([\d.]+) ms max ([\d.]+) ms \(([\d.]+) fps\); render thread blocked: '
                   r'pacing (\d+)% fence (\d+)% drain (\d+)% pipeline build (\d+)% shader xlate (\d+)% texture (\d+)%'
                   r'.*?render busy avg ([\d.]+) ms; replay avg ([\d.]+) ms')
PERF2 = re.compile(r'perf: frame (\d+): (?:gpu avg ([\d.]+) ms|gpu avg n/a).*?fence wait per frame: render thread '
                   r'([\d.]+) ms, other threads ([\d.]+) ms.*?bound: (\w+)')
PERF3 = re.compile(r'perf: frame (\d+): gpu idle per frame ([\d.]+) ms between timed parts: ([\d.]+) ms waiting for the '
                   r'next submission, ([\d.]+) ms with it queued')
SHADOW = re.compile(r'mem: shadow at frame (\d+): .*?since the last: (\d+) pool syncs, \d+ queries, ([\d.]+) MiB copied '
                    r'in.*?([\d.]+) MiB written back, ([\d.]+) ms')
WW = re.compile(r'mem: write-watch frame (\d+): per frame, every consumer: ([\d.]+) queries over ([\d.]+) MiB, ([\d.]+) ms')
SCHED = re.compile(r'sched: frame (\d+): ([\d.]+) s, cores busy ([\d.]+) of (\d+), core wait ([\d.]+) thread-s')
THREAD = re.compile(r'sched: (render )?(0x[0-9a-f]+) p(\d+) m\w+ run (\d+)% core (\d+)% host (\d+)% sleep (\d+)% '
                    r'sync (\d+)% cv (\d+)% lock (\d+)% addr (\d+)% cpu (\d+)%.*?\((.*)\)$')
WIN = 60    # presents per perf window
SCHED_N = 120   # presents per sched window


def parse_log(path):
    """Perf windows (dicts in log order, each with the byte offset where it was written and its run) and sched windows.
    A shared log (Steam's stderr file) holds several runs, each counting frames from 1: "version:" lines start a run,
    and a window is (run, frame)."""
    wins, extra, sched = {}, defaultdict(dict), []
    off, run = 0, 0
    with open_text(path, binary=True) as f:
        for raw in f:
            off += len(raw)
            line = raw.decode('utf-8', 'replace').rstrip('\n')
            if '[host] version: TotK Recomp' in line:
                run += 1
            elif 'perf: frame' in line:
                if mm := PERF1.search(line):
                    g = mm.groups()
                    wins[run, int(g[0])] = {'run': run, 'frame': int(g[0]), 'off': off, 'interval': float(g[1]),
                                            'max': float(g[2]), 'pace': float(g[4]), 'fence': float(g[5]),
                                            'drain': float(g[6]), 'pipe': float(g[7]), 'xlate': float(g[8]),
                                            'tex': float(g[9]), 'rbusy': float(g[10]), 'replay': float(g[11])}
                elif mm := PERF2.search(line):
                    g = mm.groups()
                    extra[run, int(g[0])].update(gpu=float(g[1]) if g[1] else None, rw=float(g[2]), ow=float(g[3]),
                                                 bound=g[4])
                elif mm := PERF3.search(line):
                    g = mm.groups()
                    extra[run, int(g[0])].update(idle=float(g[1]), starved=float(g[2]), queued=float(g[3]))
            elif 'mem: shadow at frame' in line and (mm := SHADOW.search(line)):
                g = mm.groups()
                extra[run, int(g[0])].update(sh_mib=float(g[2]) / WIN, sh_wb=float(g[3]) / WIN, sh_ms=float(g[4]) / WIN)
            elif 'mem: write-watch frame' in line and (mm := WW.search(line)):
                g = mm.groups()
                extra[run, int(g[0])].update(ww_q=float(g[1]), ww_mib=float(g[2]), ww_ms=float(g[3]))
            elif 'sched: ' in line:
                if mm := SCHED.search(line):
                    g = mm.groups()
                    sched.append({'run': run, 'frame': int(g[0]), 'span': float(g[1]), 'busy': float(g[2]),
                                  'ncore': int(g[3]), 'wait': float(g[4]), 'threads': []})
                elif sched and (mm := THREAD.search(line)):
                    sched[-1]['threads'].append(mm.groups())
    for key, e in extra.items():
        if key in wins:
            wins[key].update(e)
    return sorted(wins.values(), key=lambda w: w['off']), sched


def place(wins, logpos, base=0):
    """Each window's time: the first sample whose log position is past its line. base: where the log copy starts in
    the file the positions were read from (M logbase)."""
    pos = [p - base for _, p in logpos]
    for w in wins:
        i = bisect.bisect_left(pos, w['off'])
        # A line from before the first sample belongs to an earlier run (Steam's shared stderr file): no time.
        w['t'] = logpos[i][0] if 0 < i < len(logpos) else None


def playing(w):
    return w.get('t') is not None and w['interval'] <= 100 and w['max'] <= 400


def stretches(wins, length_s):
    """The slowest and the fastest run of consecutive playing windows lasting length_s, as lists of windows."""
    runs, cur = [], []
    for w in wins:
        if playing(w) and cur and w['run'] == cur[-1]['run'] and w['frame'] - cur[-1]['frame'] == WIN:
            cur.append(w)
        else:
            if cur:
                runs.append(cur)
            cur = [w] if playing(w) else []
    if cur:
        runs.append(cur)
    slow = fast = None
    for run in runs:
        j, total = 0, 0.0
        for i in range(len(run)):
            while j < len(run) and total < length_s * 1000:
                total += run[j]['interval'] * WIN
                j += 1
            if total >= length_s * 1000:
                ms = total / (j - i) / WIN
                if slow is None or ms > slow[0]:
                    slow = (ms, run[i:j])
                if fast is None or ms < fast[0]:
                    fast = (ms, run[i:j])
            total -= run[i]['interval'] * WIN
    if slow is None:   # no stretch that long: the longest run
        longest = max(runs, key=len, default=[])
        return longest or None, None
    return slow[1], fast[1]


def span_of(sel, wins):
    """Capture time covered by the selected windows: from the time of the window before the first to the last."""
    i = wins.index(sel[0])
    t0 = wins[i - 1]['t'] if i > 0 and wins[i - 1].get('t') else sel[0]['t'] - int(sel[0]['interval'] * WIN * 1000)
    return t0, sel[-1]['t']


def log_numbers(sel, sched):
    """Means over selected windows; the sched windows that overlap them."""
    n = {'frames': (sel[0]['frame'] - WIN, sel[-1]['frame'])}
    for k in ('interval', 'pace', 'fence', 'drain', 'pipe', 'xlate', 'tex', 'rbusy', 'replay', 'rw', 'ow', 'idle',
              'starved', 'queued', 'sh_mib', 'sh_ms', 'sh_wb', 'ww_q', 'ww_mib', 'ww_ms'):
        v = [w[k] for w in sel if w.get(k) is not None]
        n[k] = mean(v) if v else None
    g = [w['gpu'] for w in sel if w.get('gpu') is not None]
    n['gpu'] = mean(g) if g else None
    n['max'] = max(w['max'] for w in sel)
    n['bound'] = Counter(w.get('bound', '?') for w in sel)
    lo, hi = n['frames']
    n['sched'] = [s for s in sched if s['run'] == sel[0]['run'] and s['frame'] > lo and s['frame'] - SCHED_N < hi]
    return n


def render_log(n):
    out, hints = [], []
    p = out.append
    iv = n['interval']
    p(f"log: frames {n['frames'][0]}..{n['frames'][1]}")
    p(f"  frame interval avg {iv:.2f} ms ({1000 / iv:.1f} fps), slowest frame {n['max']:.1f} ms")
    p(f"  render thread blocked: pacing {n['pace']:.0f}% fence {n['fence']:.0f}% drain {n['drain']:.0f}% pipeline "
      f"{n['pipe']:.0f}% xlate {n['xlate']:.0f}% texture {n['tex']:.0f}%")
    p(f"  render thread busy {n['rbusy']:.2f} ms per frame, replay {n['replay']:.2f} ms per frame")
    if n['gpu'] is not None:
        p(f"  GPU busy (timestamps) {n['gpu']:.2f} ms per frame = {pct(n['gpu'], iv):.0f}% of the interval; fence "
          f"waits: render thread {n['rw']:.2f} ms, other threads {n['ow']:.2f} ms; bound: "
          + ', '.join(f'{k} {v}' for k, v in n['bound'].most_common()))
        if n['bound'].get('cpu', 0) >= sum(n['bound'].values()) / 2:
            hints.append(f"the log calls the frame CPU-bound: GPU busy {n['gpu']:.1f} ms of {iv:.1f} ms")
    if n['starved'] is not None:
        p(f"  GPU idle {n['idle']:.2f} ms per frame: {n['starved']:.2f} ms waiting for the next submission (the CPU "
          f"side was late), {n['queued']:.2f} ms with work queued (untimed work or waits)")
        if n['starved'] > 0.2 * iv:
            hints.append(f"the GPU waited {n['starved']:.1f} ms a frame for the next submission: the game thread that "
                         f"submits is late; the sched lines name the render thread, and TOTK_CRIT gives the chain of "
                         f"waits behind it")
    if n['sh_ms'] is not None:
        p(f"  shadow storage: {n['sh_mib']:.1f} MiB copied in per frame in {n['sh_ms']:.2f} ms, {n['sh_wb']:.2f} MiB "
          f"written back")
        if n['sh_ms'] > 0.1 * iv:
            hints.append(f"shadow copies take {n['sh_ms']:.1f} ms a frame ({n['sh_mib']:.0f} MiB): per-2-MiB write "
                         f"tracking (no userfaultfd in this kernel) copies whole regions")
    if n['ww_ms'] is not None:
        p(f"  write watch: {n['ww_q']:.0f} queries over {n['ww_mib']:.0f} MiB per frame, {n['ww_ms']:.2f} ms")
    sc = n['sched']
    if sc:
        busy = mean(s['busy'] for s in sc)
        p(f"  guest scheduler: virtual cores busy {busy:.2f} of {sc[0]['ncore']}, threads waited "
          f"{mean(s['wait'] / s['span'] for s in sc if s['span']):.2f} thread-s per s for a core")
        if busy >= 2.6:
            hints.append(f'the guest keeps {busy:.2f} of its 3 virtual cores busy: the lifted game code is the limit')
        rows = defaultdict(list)
        for s in sc:
            for g in s['threads']:
                rows[(g[1], g[12])].append(g)
        agg = []
        for (h, entry), gs in rows.items():
            mv = lambda i: mean(int(x[i]) for x in gs) * len(gs) / len(sc)   # absent from a window: 0 there
            agg.append((mv(3), h, gs[0][2], entry, bool(gs[0][0]), mv(4), mv(5), mv(6),
                        mv(7) + mv(8) + mv(9) + mv(10)))
        agg.sort(reverse=True)
        p('  busiest guest threads (share of the time): run, waiting for a virtual core, host calls, sleep, waits')
        for run, h, prio, entry, render, core, host, sleep, syn in agg[:8]:
            p(f'    {h:<7} p{prio:<3} run {run:3.0f}%  core {core:3.0f}%  host {host:3.0f}%  sleep {sleep:3.0f}%  '
              f'waits {syn:3.0f}%  {"[render] " if render else ""}{entry}')
            if run >= 85:
                hints.append(f'guest thread {h} ({entry}) runs {run:.0f}% of the time: it alone can set the frame rate')
            if core >= 15:
                hints.append(f'guest thread {h} ({entry}) waits {core:.0f}% of the time for a virtual core')
    return out, hints


# ---- timeline and comparison

def hottest(m):
    if not m['threads']:
        return '-'
    cpu, _, tid, name, *_ = m['threads'][0]
    return f'{name[:14]} {100 * cpu:.0f}%'


def timeline(d, wins, cls, bucket, marks):
    t0, t1 = d['times'][0], d['times'][-1]
    out = [f'timeline ({bucket:.0f} s steps; fps from the log, GPU busy from the game\'s DRM client):',
           '     s    fps  GPU%  GPU MHz  guest cores  busiest thread         max C']
    tw = [w for w in wins if w.get('t') is not None]
    ts = [w['t'] for w in tw]
    b = t0
    while b < t1:
        e = b + int(bucket * 1e6)
        m = measure(sliced(d, b, e), cls)
        ws = tw[bisect.bisect_left(ts, b):bisect.bisect_left(ts, e)]
        fps = f'{1000 / mean(w["interval"] for w in ws):5.1f}' if ws else '    -'
        gb = f"{m['gpu_busy']:4.0f}" if m['gpu_busy'] is not None else '   -'
        gm = f"{m['gpu_mhz']:7.0f}" if m['gpu_mhz'] else '      -'
        tm = f"{m['temp_max']:5.0f}" if m['temp_max'] is not None else '    -'
        mark = ''.join(f'  <- {k}' for k, (s0, s1) in marks.items() if s0 < e and s1 > b)
        out.append(f'{(b - t0) / 1e6:6.0f}  {fps}  {gb}  {gm}  {m["guest_cores"]:11.2f}  {hottest(m):<22} {tm}{mark}')
        b = e
    return out


def compare(cols):
    """cols: [(title, measure, log numbers)]: the main numbers side by side."""
    def f(v, fmt):
        return format(v, fmt) if v is not None else '-'
    lines = [('fps', lambda m, n: f(1000 / n['interval'], '.1f') if n else '-'),
             ('frame interval ms', lambda m, n: f(n and n['interval'], '.2f')),
             ('GPU busy ms/frame (timestamps)', lambda m, n: f(n and n['gpu'], '.2f')),
             ('GPU idle, waiting for a submission', lambda m, n: f(n and n['starved'], '.2f')),
             ('render thread busy ms', lambda m, n: f(n and n['rbusy'], '.2f')),
             ('replay ms', lambda m, n: f(n and n['replay'], '.2f')),
             ('shadow copy ms (MiB)', lambda m, n: f'{n["sh_ms"]:.2f} ({n["sh_mib"]:.0f})' if n and n['sh_ms'] is not None else '-'),
             ('guest virtual cores busy', lambda m, n: f(mean(s['busy'] for s in n['sched']) if n and n['sched'] else None, '.2f')),
             ('GPU busy % (DRM client)', lambda m, n: f(m['gpu_busy'], '.0f')),
             ('GPU clock MHz', lambda m, n: f(m['gpu_mhz'], '.0f')),
             ('host cores used by the game', lambda m, n: f(sum(r[0] for r in m['threads']), '.2f')),
             ('busiest thread', lambda m, n: hottest(m)),
             ('hottest zone C', lambda m, n: f(m['temp_max'], '.0f'))]
    w = max(len(t) for t, *_ in cols) + 2
    out = [f'{"":36}' + ''.join(f'{t:>{max(w, 22)}}' for t, *_ in cols)]
    for name, fn in lines:
        out.append(f'{name:36}' + ''.join(f'{fn(m, n):>{max(w, 22)}}' for _, m, n in cols))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('capture', nargs='?', help='odin_sysmon.sh output (.tsv or .tsv.gz)')
    ap.add_argument('--log', help='totk.log of the same run (TOTK_TRACE=perf,sched)')
    ap.add_argument('--from', dest='t_from', type=float, help='seconds from the capture start: analyse from here')
    ap.add_argument('--to', dest='t_to', type=float, help='seconds from the capture start: analyse up to here')
    ap.add_argument('--stretch', type=float, default=20, help='seconds of the slowest and fastest stretch')
    ap.add_argument('--bucket', type=float, default=0, help='timeline step in seconds (default: 10, more for long runs)')
    ap.add_argument('--windows', type=int, default=8, help='without alignment: the last perf windows of the log')
    ap.add_argument('--top', type=int, default=20, help='threads to list')
    a = ap.parse_args()
    if not a.capture and not a.log:
        ap.error('give a capture, a --log, or both')

    d = parse_capture(a.capture) if a.capture else None
    wins, sched = parse_log(a.log) if a.log else ([], [])
    if d is not None and len(d['times']) < 3:
        sys.exit('fewer than 3 samples: run the capture longer')
    hints = []
    if d is not None:
        meta = d['meta']
        cls, ci = cpu_classes(d)
        span = (d['times'][-1] - d['times'][0]) / 1e6
        print(f"device: {(meta.get('model') or ['?'])[0].strip() or '?'}; kernel {(meta.get('kernel') or ['?'])[0]}; "
              f"started {(meta.get('start') or ['?'])[0]}; {len(d['times'])} samples over {span:.0f} s "
              f"(every {(meta.get('interval') or ['?'])[0]} s)")
        for e in meta.get('env', []):
            print(f'  game environment: {e}')
        for lp in meta.get('logpath', [])[:1]:
            print(f'  the game\'s log: {lp}')
    aligned = d is not None and wins and d['logpos']
    if aligned:
        place(wins, d['logpos'], int((d['meta'].get('logbase') or ['0'])[-1] or 0))
        aligned = any(w.get('t') for w in wins)

    if aligned:
        t0 = d['times'][0]
        if a.t_from is not None or a.t_to is not None:
            lo = t0 + int((a.t_from or 0) * 1e6)
            hi = t0 + int(a.t_to * 1e6) if a.t_to is not None else d['times'][-1]
            slow = [w for w in wins if w.get('t') and lo <= w['t'] <= hi]
            fast = None
            title = f'the span {(a.t_from or 0):.0f}..{(hi - t0) / 1e6:.0f} s'
        else:
            slow, fast = stretches(wins, a.stretch)
            title = f'the slowest {a.stretch:.0f} s of play'
        user_span = a.t_from is not None or a.t_to is not None
        marks = {}
        if slow:
            marks['span' if user_span else 'slow'] = span_of(slow, wins)
        if fast:
            marks['fast'] = span_of(fast, wins)
        bucket = a.bucket or max(10, -(-span // 600) * 10)   # at most about 60 rows
        print('')
        print('\n'.join(timeline(d, wins, cls, bucket, marks)))
        if not slow:
            print('\nno playing windows in the log (all loading or stalls): the whole capture follows')
            m = measure(d, cls)
            lines, h = render_detail(d, m, cls, ci, a.top)
            print('\n' + '\n'.join(lines))
            hints += h
        else:
            s0, s1 = span_of(slow, wins)
            ds = sliced(d, s0, s1)
            ms_, ns = measure(ds, cls), log_numbers(slow, sched)
            print(f'\n==== {title}: {(s0 - t0) / 1e6:.0f}..{(s1 - t0) / 1e6:.0f} s of the capture, '
                  f'{1000 / ns["interval"]:.1f} fps')
            lines, h = render_detail(ds, ms_, cls, ci, a.top)
            print('\n'.join(lines))
            hints += h
            lines, h = render_log(ns)
            print('\n' + '\n'.join(lines))
            hints += h
            cols = [('span' if user_span else 'slowest', ms_, ns)]
            if fast:
                f0, f1 = span_of(fast, wins)
                cols.append(('fastest', measure(sliced(d, f0, f1), cls), log_numbers(fast, sched)))
                print(f'\n==== the fastest {a.stretch:.0f} s of play: {(f0 - t0) / 1e6:.0f}..{(f1 - t0) / 1e6:.0f} s, '
                      f'{1000 / cols[1][2]["interval"]:.1f} fps')
            cols.append(('whole capture', measure(d, cls), log_numbers([w for w in wins if playing(w)] or slow, sched)))
            print('')
            print('\n'.join(compare(cols)))
    else:
        if d is not None:
            bucket = a.bucket or max(10, -(-span // 600) * 10)
            print('')
            print('\n'.join(timeline(d, [], cls, bucket, {})))
            if d['logpos'] == [] and a.log:
                print('(the capture has no log positions, so its samples cannot be matched with frames: whole capture '
                      'and the log\'s last windows)')
            m = measure(d, cls)
            lines, h = render_detail(d, m, cls, ci, a.top)
            print('\n' + '\n'.join(lines))
            hints += h
        if wins:
            lines, h = render_log(log_numbers([w for w in wins if w['run'] == wins[-1]['run']][-a.windows:], sched))
            print('\n' + '\n'.join(lines))
            hints += h
        elif a.log:
            print('\nlog: no perf lines (run with TOTK_TRACE=perf,sched)')
    print('\nhints:' if hints else '\nhints: none')
    for h in dict.fromkeys(hints):
        print(f'  - {h}')


if __name__ == '__main__':
    main()
