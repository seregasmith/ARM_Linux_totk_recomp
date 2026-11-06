#!/usr/bin/env bash
# Samples what the machine does while totk runs: each core's load and clock, the GPU's clock and busy time, thermal
# zones and throttling, and every thread of the game (CPU time, time spent waiting for a CPU, the core it ran on).
# The loop uses bash builtins only, so the sampler itself costs little. tools/sysmon_report.py makes the report.
#
#   tools/odin_sysmon.sh [-d seconds] [-i interval] [-p pid] [-o file] [-L totk.log] [-R]
#
# By hand: start the game, stand where the frame rate drops, run this. From Steam: TOTK_SYSMON=1 %command% in the
# launch options of the setup's Steam entry makes its script start it for the whole session (sysmon_launch.sh, installed
# as sysmon/launch.sh; docs/arm64-performance.md).
#   -d  seconds to sample, 0 until the game exits (default 30)
#   -i  seconds between samples (default 0.5)
#   -p  the game's pid (default: the oldest process named totk); the Steam entry's script passes its own pid before
#       it execs the game, and the sampler waits for that exec
#   -o  output (default sysmon-<date>.tsv in the current folder)
#   -L  the game's log when the sampler never sees it open. The log the game writes is found by itself: logs/totk.log,
#       or the file its stderr goes to (Steam's gaming mode), which the game then writes instead. With -L or -R it is
#       followed into <output>.log while the game runs
#   -R  at the end, run sysmon_report.py (beside this script) into <output>.txt, then gzip the capture and the log copy.
#       Captures in the same folder that have no report yet (Steam ended their sampler first) get one at the start
#
# Output: one record per line, tab separated, first field the kind. M: fixed facts (key, value). T: a sample's time in
# us. S: a CPU's busy and idle jiffies (/proc/stat). F: a CPU's clock in kHz. G: a devfreq device's clock in Hz.
# K: kgsl busy and total, clock (downstream Qualcomm kernels). D: a DRM fd of the game (fd, client id, GPU busy ns,
# GPU cycles). L: the position of the game's log file (the report finds the frame of each sample from it); M logpath
# names that file, M logcopy and logbase (at the end) the copy's source and the position the copy starts at.
# H: a thread (tid, user+system ticks, run ns, run-queue wait ns, last CPU, name). Every 4th sample: Z a thermal zone
# (index, milli-degrees), C a cooling device (index, state), P a CPU policy's current upper limit. X: a devfreq
# device's time-in-state table at the start and the end.
set -u
export LC_ALL=C   # the decimal point of EPOCHREALTIME and sleep
# Steam preloads its overlay library into the game's environment; every command this script runs would fail to load it
# and say so in the log.
unset LD_PRELOAD

SYS=${SYSMON_SYS:-/sys}   # another root for tests
dur=30 ival=0.5 pid= out= logsrc= report=
while getopts d:i:p:o:L:Rh opt; do
  case $opt in
    d) dur=$OPTARG ;;
    i) ival=$OPTARG ;;
    p) pid=$OPTARG ;;
    o) out=$OPTARG ;;
    L) logsrc=$OPTARG ;;
    R) report=1 ;;
    *) sed -n '6,17p' "$0"; exit 2 ;;
  esac
done
if [ -z "$pid" ]; then
  pid=$(pgrep -o -x totk) || { echo "odin_sysmon: no process named totk; start the game or pass -p PID" >&2; exit 1; }
fi
[ -r "/proc/$pid/stat" ] || { echo "odin_sysmon: no process $pid" >&2; exit 1; }
[ -n "$out" ] || out=sysmon-$(date +%Y%m%d-%H%M%S).tsv
stop=
trap 'stop=1' TERM INT HUP   # finish the files when the session ends under us
base=${out%.tsv}
copy=   # follow the game's log into <output>.log while the game runs
if [ -n "$logsrc" ] || [ -n "$report" ]; then copy=1; fi
copy_pid=

# The report and the gzip of one capture (its .tsv, and its .log when there is one); $2: the nice level.
finish_capture() {
  local tsv=$1 stem=${1%.tsv} rep
  rep=$(dirname "$0")/sysmon_report.py
  if command -v python3 >/dev/null 2>&1 && [ -f "$rep" ]; then
    if [ -f "$stem.log" ]; then nice -n "$2" python3 "$rep" "$tsv" --log "$stem.log" > "$stem.txt" 2>&1
    else nice -n "$2" python3 "$rep" "$tsv" > "$stem.txt" 2>&1; fi
    echo "odin_sysmon: report in $stem.txt" >&2
  else
    echo "odin_sysmon: no python3 here: run sysmon_report.py on $tsv elsewhere" >&2
  fi
  if command -v gzip >/dev/null 2>&1; then
    gzip -f "$tsv"
    if [ -f "$stem.log" ]; then gzip -f "$stem.log"; fi
  fi
}

# Steam ends what a game left running soon after the game exits, which can cut a report short. Captures of earlier
# sessions left without one get it now, at the lowest priority, while this game loads.
if [ -n "$report" ]; then
  for old in "$(dirname "$out")"/sysmon-*.tsv; do
    [ -f "$old" ] && [ "$old" != "$out" ] && [ ! -f "${old%.tsv}.txt" ] || continue
    finish_capture "$old" 19 &
  done
fi

now_us() {   # sets t: microseconds since the epoch
  if [ -n "${EPOCHREALTIME:-}" ]; then t=${EPOCHREALTIME//[.,]/}; else t=$(($(date +%s%N) / 1000)); fi
}
rd() {   # rd VAR FILE: first line of FILE into VAR, empty when unreadable
  local _v=
  { read -r _v < "$2"; } 2>/dev/null
  printf -v "$1" '%s' "$_v"
}
alive() {   # the game runs (not gone, not a zombie waiting for its parent)
  local s
  { read -r s < "/proc/$pid/stat"; } 2>/dev/null || return 1
  s=${s##*) }
  [ "${s%% *}" != Z ]
}
logfd= logpath= new_log= firstpos= drmfds=()
scan_fds() {   # the game's DRM fds (the Vulkan device's counts its GPU time) and its log; one ls per call
  local line fd target
  drmfds=()
  while IFS= read -r line; do
    case $line in *' -> '*) ;; *) continue ;; esac
    target=${line#* -> }
    fd=${line%% -> *}
    fd=${fd##* }
    case $target in
      /dev/dri/*) drmfds+=("$fd") ;;
      */logs/totk.log) [ "$logfd" = "$fd" ] || { logfd=$fd logpath=$target new_log=1; } ;;
    esac
  done < <(ls -l "/proc/$pid/fd" 2>/dev/null)
  # With stderr in a file (Steam's gaming mode, `2> file`) the game writes its log there and opens no logs/totk.log
  # (runtime/main.c log_open).
  if [ -z "$logfd" ] && [ -f "/proc/$pid/fd/2" ]; then
    logfd=2 logpath=$(readlink "/proc/$pid/fd/2" 2>/dev/null) new_log=1
  fi
}

# A launcher script passes its own pid and then execs the game: wait for the exec (the process takes the name totk),
# at most 10 s.
n=0
while [ "$n" -lt 40 ] && [ -z "$stop" ]; do
  rd comm "/proc/$pid/comm"
  [ "$comm" != totk ] || break
  sleep 0.25
  n=$((n + 1))
done
alive || { echo "odin_sysmon: process $pid ended before sampling started" >&2; exit 1; }
scan_fds

exec 3>"$out"
meta() { printf 'M\t%s\t%s\n' "$1" "$2" >&3; }

# ---- fixed facts
meta model "$({ tr '\0' ' ' < /proc/device-tree/model; } 2>/dev/null)"
meta kernel "$(uname -r)"
meta clk_tck "$(getconf CLK_TCK)"
meta interval "$ival"
meta pid "$pid"
meta exe "$(readlink "/proc/$pid/exe" 2>/dev/null)"
meta start "$(date '+%Y-%m-%d %H:%M:%S')"
# the game's TOTK_ settings from its environment (nothing else of it)
while IFS= read -r -d '' kv; do
  case $kv in TOTK_*) meta env "$kv" ;; esac
done < "/proc/$pid/environ" 2>/dev/null

cpus=()
for d in "$SYS"/devices/system/cpu/cpu[0-9]*; do
  [ -d "$d" ] || continue
  c=${d##*cpu}
  cpus+=("$c")
  rd online "$d/online"
  rd maxf "$d/cpufreq/cpuinfo_max_freq"
  rd minf "$d/cpufreq/cpuinfo_min_freq"
  rd gov "$d/cpufreq/scaling_governor"
  rd smax "$d/cpufreq/scaling_max_freq"
  rd cap "$d/cpu_capacity"
  meta "cpu$c" "online=${online:-1} max_khz=$maxf min_khz=$minf governor=$gov scaling_max_khz=$smax capacity=$cap"
done

devfreqs=()
for d in "$SYS"/class/devfreq/*; do
  [ -e "$d" ] || continue
  n=${d##*/}
  devfreqs+=("$n")
  rd gov "$d/governor"
  rd minf "$d/min_freq"
  rd maxf "$d/max_freq"
  rd avail "$d/available_frequencies"
  compat=$({ tr '\0' ',' < "$d/device/of_node/compatible"; } 2>/dev/null)
  meta "devfreq $n" "governor=$gov min_hz=$minf max_hz=$maxf compatible=${compat%,} available=$avail"
done
kgsl=$SYS/class/kgsl/kgsl-3d0
if [ -d "$kgsl" ]; then
  rd kmax "$kgsl/max_gpuclk"
  rd kgov "$kgsl/devfreq/governor"
  meta kgsl "max_gpuclk=$kmax governor=$kgov"
else
  kgsl=
fi

zones=()
for d in "$SYS"/class/thermal/thermal_zone*; do
  [ -d "$d" ] || continue
  i=${d##*thermal_zone}
  zones+=("$i")
  rd ty "$d/type"
  meta "zone$i" "$ty"
done
cools=()
for d in "$SYS"/class/thermal/cooling_device*; do
  [ -d "$d" ] || continue
  i=${d##*cooling_device}
  cools+=("$i")
  rd ty "$d/type"
  rd mx "$d/max_state"
  meta "cooling$i" "type=$ty max_state=$mx"
done
policies=()
for d in "$SYS"/devices/system/cpu/cpufreq/policy*; do
  [ -d "$d" ] || continue
  policies+=("${d##*policy}")
  rd rel "$d/related_cpus"
  meta "policy${d##*policy}" "cpus=$rel"
done

trans_stat() {   # X records: devfreq time-in-state tables (phase start|end)
  local n line
  for n in "${devfreqs[@]}"; do
    [ -r "$SYS/class/devfreq/$n/trans_stat" ] || continue
    while IFS= read -r line; do printf 'X\t%s\t%s\t%s\n' "$1" "$n" "$line" >&3; done < "$SYS/class/devfreq/$n/trans_stat"
  done
}
trans_stat start

# Follows the game's log into <output>.log while the game runs, so the copy is complete even when Steam ends the
# sampler right after the game. GNU tail's --pid makes it end by itself after the game.
start_copy() {
  local lb=0
  # Steam's stderr file holds what other programs wrote before: start 64 KiB before the game's first position seen.
  if [ "$logfd" = 2 ] && [ -n "$firstpos" ] && [ "$firstpos" -gt 65536 ]; then lb=$((firstpos - 65536)); fi
  tail --pid=1 -c 0 /dev/null 2>/dev/null || return 0   # not GNU tail: one copy at the end instead
  tail -c +$((lb + 1)) --pid="$pid" -F -- "$logpath" > "$base.log" 2>/dev/null 3>&- &
  copy_pid=$!
  meta logcopy "$logpath"
  meta logbase "$lb"
}

# ---- samples
now_us
t0=$t
if [ "${dur%.*}" -gt 0 ] 2>/dev/null; then end=$((t0 + ${dur%.*} * 1000000)); else end=$((t0 + 10 ** 12)); fi
k=0
echo "odin_sysmon: sampling pid $pid every ${ival}s ($([ "$end" -gt $((t0 + 10 ** 11)) ] && echo "until it exits" || echo "for ${dur}s")) into $out" >&2
while [ -z "$stop" ] && alive; do
  now_us
  [ "$t" -le "$end" ] || break
  # The log and the Vulkan device open in the game's first second: a scan every 2 s for the first minute, then
  # every 10 s, keeps up with new fds.
  [ "$k" -gt 0 ] && [ $((k % (k < 120 ? 4 : 20))) -eq 0 ] && scan_fds
  printf 'T\t%s\n' "$t" >&3

  if [ -n "$logfd" ]; then
    pos=
    { read -r key pos _ < "/proc/$pid/fdinfo/$logfd"; } 2>/dev/null
    [ -n "$pos" ] && printf 'L\t%s\n' "$pos" >&3
    [ -n "$firstpos" ] || firstpos=$pos
  fi
  if [ -n "$new_log" ]; then
    meta logpath "$logpath"
    echo "odin_sysmon: the game's log is $logpath (fd $logfd)" >&2
    new_log=
    if [ -n "$copy" ] && [ -z "$copy_pid" ]; then start_copy; fi
  fi

  while read -r name u n s idle io irq sirq steal _; do
    case $name in
      cpu[0-9]*) printf 'S\t%s\t%s\t%s\n' "${name#cpu}" $((u + n + s + irq + sirq + steal)) $((idle + io)) >&3 ;;
      cpu) ;;
      *) break ;;
    esac
  done < /proc/stat

  for c in "${cpus[@]}"; do
    rd f "$SYS/devices/system/cpu/cpu$c/cpufreq/scaling_cur_freq"
    [ -n "$f" ] && printf 'F\t%s\t%s\n' "$c" "$f" >&3
  done
  for n in "${devfreqs[@]}"; do
    rd f "$SYS/class/devfreq/$n/cur_freq"
    printf 'G\t%s\t%s\n' "$n" "$f" >&3
  done
  if [ -n "$kgsl" ]; then
    b1=0 b2=0
    { read -r b1 b2 _ < "$kgsl/gpubusy"; } 2>/dev/null
    rd clk "$kgsl/gpuclk"
    printf 'K\t%s\t%s\t%s\n' "$b1" "$b2" "$clk" >&3
  fi

  for fd in "${drmfds[@]}"; do
    id= eng= cyc=
    while read -r key val _; do
      case $key in
        drm-client-id:) id=$val ;;
        drm-engine-gpu:) eng=$val ;;
        drm-cycles-gpu:) cyc=$val ;;
      esac
    done < "/proc/$pid/fdinfo/$fd" 2>/dev/null
    [ -n "$eng" ] && printf 'D\t%s\t%s\t%s\t%s\n' "$fd" "$id" "$eng" "${cyc:-0}" >&3
  done

  for td in /proc/"$pid"/task/*; do
    { read -r line < "$td/stat"; } 2>/dev/null || continue
    comm=${line#*(}
    comm=${comm%)*}
    set -f
    set -- ${line##*) }
    set +f
    # after the name: $1 is field 3 (state); utime 14, stime 15, processor 39
    run=0 wait=0
    { read -r run wait _ < "$td/schedstat"; } 2>/dev/null
    printf 'H\t%s\t%s\t%s\t%s\t%s\t%s\n' "${td##*/}" $((${12} + ${13})) "$run" "$wait" "${37}" "$comm" >&3
  done

  if [ $((k % 4)) -eq 0 ]; then
    for i in "${zones[@]}"; do
      rd v "$SYS/class/thermal/thermal_zone$i/temp"
      [ -n "$v" ] && printf 'Z\t%s\t%s\n' "$i" "$v" >&3
    done
    for i in "${cools[@]}"; do
      rd v "$SYS/class/thermal/cooling_device$i/cur_state"
      [ -n "$v" ] && printf 'C\t%s\t%s\n' "$i" "$v" >&3
    done
    for p in "${policies[@]}"; do
      rd v "$SYS/devices/system/cpu/cpufreq/policy$p/scaling_max_freq"
      [ -n "$v" ] && printf 'P\t%s\t%s\n' "$p" "$v" >&3
    done
  fi
  k=$((k + 1))
  sleep "$ival"
done
trans_stat end
exec 3>&-
echo "odin_sysmon: $k samples in $out" >&2

[ -n "$logpath" ] || echo "odin_sysmon: the game's log was never seen open; samples carry no frame numbers" >&2
if [ -n "$copy_pid" ]; then
  if alive; then
    kill "$copy_pid" 2>/dev/null   # the game runs on (-d): stop following its log
  else
    n=0   # tail ends by itself a moment after the game, with the game's last lines
    while kill -0 "$copy_pid" 2>/dev/null && [ "$n" -lt 10 ]; do sleep 0.5; n=$((n + 1)); done
    kill "$copy_pid" 2>/dev/null
  fi
  wait "$copy_pid" 2>/dev/null
elif [ -n "$copy" ]; then
  # Without GNU tail: one copy now, of the log the game was seen writing, else of -L.
  src=
  if [ -n "$logpath" ] && [ -f "$logpath" ]; then src=$logpath
  elif [ -n "$logsrc" ] && [ -f "$logsrc" ]; then src=$logsrc; fi
  lb=0
  if [ "$logfd" = 2 ] && [ -n "$firstpos" ] && [ "$firstpos" -gt 65536 ]; then lb=$((firstpos - 65536)); fi
  if [ -n "$src" ] && tail -c +$((lb + 1)) "$src" > "$base.log" 2>/dev/null; then
    printf 'M\tlogcopy\t%s\nM\tlogbase\t%s\n' "$src" "$lb" >> "$out"
  fi
fi
if [ -n "$report" ]; then finish_capture "$out" 0; fi
