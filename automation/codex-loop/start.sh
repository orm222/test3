#!/usr/bin/env bash
# A single tmux session displays the runner. Python owns all workflow decisions.
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/../../.venv/bin/python}"
config="$SCRIPT_DIR/config.json"
use_tmux=1
inside_tmux=0
runner_args=()

usage() {
    cat <<'USAGE'
Usage: bash start.sh [--config PATH] [--no-tmux] [--preflight] [--probe-models]
  --config PATH    Runner configuration (default: config.json beside this script).
  --no-tmux        Run in the current terminal.
  --preflight     Validate configuration, CLI flags and login; do not implement.
  --probe-models  Also make two short real model calls during preflight.
Set PYTHON_BIN to select Python, e.g. PYTHON_BIN=python3.9 bash start.sh ...
USAGE
}

while (( $# )); do
    case "$1" in
        --config)
            if (( $# < 2 )); then
                printf '%s\n' 'Missing value for --config' >&2
                exit 2
            fi
            config="$2"
            shift 2
            ;;
        --config=*) config="${1#--config=}"; shift ;;
        --no-tmux) use_tmux=0; shift ;;
        --preflight|--probe-models) runner_args+=("$1"); shift ;;
        --inside-tmux) inside_tmux=1; use_tmux=0; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    printf 'Python executable not found: %s\n' "$PYTHON_BIN" >&2
    exit 2
fi
if [[ ! -f "$config" ]]; then
    printf 'Config not found: %s\nConnect the project with setup_env.py attach and configure its tasks first.\n' "$config" >&2
    exit 2
fi
config="$(cd -- "$(dirname -- "$config")" && pwd)/$(basename -- "$config")"

if (( use_tmux )) && ! command -v tmux >/dev/null 2>&1; then
    printf '%s\n' 'Linux work startup requires tmux. Install it once (Debian/Ubuntu: sudo apt-get install tmux), then repeat the same loop command.' >&2
    exit 2
fi

# Reuse a real current tmux session instead of nesting another terminal session.
if (( use_tmux )) && [[ -n "${TMUX:-}" ]] && tmux display-message -p '#S' >/dev/null 2>&1; then
    exec "$PYTHON_BIN" "$SCRIPT_DIR/run.py" --config "$config" "${runner_args[@]}"
fi

if (( ! use_tmux )); then
    if (( inside_tmux )); then
        "$PYTHON_BIN" "$SCRIPT_DIR/run.py" --config "$config" "${runner_args[@]}"
        result=$?
        printf '\nRunner exit code: %s (0=COMPLETED/preflight passed, 2=STOPPED, 130=interrupted)\n' "$result"
        printf '%s' 'Press Enter to close this session. '
        read -r _ || true
        exit "$result"
    fi
    exec "$PYTHON_BIN" "$SCRIPT_DIR/run.py" --config "$config" "${runner_args[@]}"
fi

session="codex-loop-$(date +%Y%m%d-%H%M%S)-$$"
# A running tmux server may have older paths. Pass runtime paths, never credentials.
runtime_env=(env -u PYTHONHOME -u PYTHONPATH -u CODEX_HOME -u HOME
    "PYTHON_BIN=$PYTHON_BIN" "PATH=$PATH" "VIRTUAL_ENV=${VIRTUAL_ENV:-}"
    "PYTHONIOENCODING=${PYTHONIOENCODING:-utf-8}")
for name in PYTHONPATH CODEX_HOME HOME; do
    if [[ -v "$name" ]]; then runtime_env+=("$name=${!name}"); fi
done
# Multiple command arguments bypass tmux's default shell and preserve literal paths.
tmux_args=(new-session -d -s "$session" -c "$PWD" "${runtime_env[@]}" bash "$SCRIPT_DIR/start.sh"
    --inside-tmux --config "$config" "${runner_args[@]}")
# tmux interprets a trailing semicolon even when the caller supplies an argv array.
for i in "${!tmux_args[@]}"; do
    if [[ "${tmux_args[$i]}" == *';' ]]; then tmux_args[$i]="${tmux_args[$i]%;}\\;"; fi
done
if ! tmux "${tmux_args[@]}"; then
    printf '%s\n' 'Could not start tmux session.' >&2
    exit 2
fi
printf 'Session: %s\nReattach: tmux attach-session -t %s\n' "$session" "$session"
if [[ -t 0 && -t 1 ]]; then
    exec tmux attach-session -t "$session"
fi
# Non-interactive launchers return after starting the detached session.
exit 0
