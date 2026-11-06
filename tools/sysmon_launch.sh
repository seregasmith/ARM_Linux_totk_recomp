# Sourced by the script of the game's Steam entry (installer/games.py STEAM_SCRIPT) right before it execs totk, with
# $here the install folder; installed there as sysmon/launch.sh (installer/games.py install_sysmon). With TOTK_SYSMON
# set (Steam's launch options: TOTK_SYSMON=1 %command%) it adds perf,sched to TOTK_TRACE and starts the session
# profiler, sysmon/odin_sysmon.sh, which writes logs/sysmon-<date>.txt when the game exits (docs/arm64-performance.md).
# POSIX sh, as the script runs under /bin/sh; its variables start with sysmon_ to leave the script's alone.
if [ -n "${TOTK_SYSMON:-}" ] && [ "$TOTK_SYSMON" != 0 ]; then
    sysmon_data=${TOTK_DATA_DIR:-$here}   # where the game writes its settings and logs (runtime/cfg.h cfg_data_dir)
    sysmon_trace=${TOTK_TRACE:-}
    if [ -z "$sysmon_trace" ] && [ -f "$sysmon_data/totk.ini" ]; then   # the environment would replace totk.ini's list
        sysmon_trace=$(sed -n 's/^[[:space:]]*trace[[:space:]]*=//p' "$sysmon_data/totk.ini" | tail -n 1 | tr -d '[:space:]')
    fi
    case ",$sysmon_trace," in *,perf,*|*,all,*) ;; *) sysmon_trace=${sysmon_trace:+$sysmon_trace,}perf ;; esac
    case ",$sysmon_trace," in *,sched,*|*,all,*) ;; *) sysmon_trace=$sysmon_trace,sched ;; esac
    export TOTK_TRACE="$sysmon_trace"
    if command -v bash >/dev/null 2>&1 && mkdir -p "$sysmon_data/logs"; then
        sysmon_out=$sysmon_data/logs/sysmon-$(date +%Y%m%d-%H%M%S)
        # $$ is the script, which becomes the game at its exec; the sampler waits for that. Without Steam's
        # LD_PRELOAD: its overlay library does not load into aarch64 programs, and each one would say so.
        (cd "$sysmon_data" && unset LD_PRELOAD && exec bash "$here/sysmon/odin_sysmon.sh" -p $$ -d 0 \
            -i "${TOTK_SYSMON_INTERVAL:-0.5}" -o "$sysmon_out.tsv" -L "$sysmon_data/logs/totk.log" -R) \
            </dev/null >/dev/null 2>>"$sysmon_data/logs/sysmon.log" &
    else
        echo "TotK Recomp: TOTK_SYSMON needs bash; no profile this time" >&2
    fi
fi
