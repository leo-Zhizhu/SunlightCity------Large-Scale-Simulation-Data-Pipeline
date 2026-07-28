#!/usr/bin/env bash
# =============================================================================
# Worker entrypoint.
#
# Responsibilities, in order:
#   1. forward signals correctly so preemption is graceful
#   2. fail fast on missing configuration, with an actionable message
#   3. wait for the database rather than crash-looping against it
#   4. exec the player so it becomes PID 1's child with clean log passthrough
#
# WHY SIGNAL HANDLING IS THE FIRST CONCERN
# ----------------------------------------
# Kubernetes sends SIGTERM, waits terminationGracePeriodSeconds, then SIGKILL.
# If the shell does not forward SIGTERM to the Unity player, the player never
# learns it is being evicted, never releases its lease, and the task stays
# unclaimable until the lease expires (up to 15 min of a wasted worker slot).
#
# On spot/preemptible nodes this is the difference between a fleet that
# self-heals in seconds and one that stalls for minutes on every reclamation.
# =============================================================================

set -euo pipefail

readonly PLAYER="/app/SunlightCityWorker"
CHILD_PID=""

log() { printf '%s [entrypoint] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { printf '%s [entrypoint] FATAL: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# Signal forwarding
# -----------------------------------------------------------------------------
forward_signal() {
    local sig="$1"
    if [[ -n "$CHILD_PID" ]]; then
        log "received SIG${sig}; forwarding to player (pid ${CHILD_PID})"
        # Negative PID would signal the whole group; we want just the player so
        # it can run its own OnApplicationQuit shutdown path.
        kill -s "$sig" "$CHILD_PID" 2>/dev/null || true
    else
        log "received SIG${sig} before player start; exiting"
        exit 143
    fi
}
trap 'forward_signal TERM' TERM
trap 'forward_signal INT'  INT
# SIGHUP intentionally not trapped: nothing in this deployment sends it, and
# trapping it would mask a genuine terminal-detach bug during local debugging.

# -----------------------------------------------------------------------------
# Configuration preflight.
#
# Checked here as well as in WorkerConfig.Validate() because failing in the shell
# costs ~50 ms and produces a legible one-line error, whereas failing inside the
# player costs a ~3 s engine boot and buries the message in Unity's log preamble.
# At 50 pods x a misconfigured rollout that difference is very visible.
# -----------------------------------------------------------------------------
: "${SUNLIT_RUN_ID:?SUNLIT_RUN_ID is required — the run whose tasks to claim. Set it in the Job spec.}"
: "${SUNLIT_COORD_HOST:?SUNLIT_COORD_HOST is required — the control-plane instance (normally PgBouncer).}"
: "${SUNLIT_DB_PASSWORD:?SUNLIT_DB_PASSWORD is required — project it from a Kubernetes Secret, never bake it into the image.}"

# The SHARD endpoints are deliberately NOT checked here, and are not required env at
# all: the worker reads them from the coordinator's meo_shards registry at boot, so
# an instance can be replaced mid-run without redeploying 50 pods. Only the DNS
# template is configured, and it has a sensible default in the image.

# Pod name as worker id: already unique, and it lets a leased task be traced
# straight back to a pod's logs. Injected via the downward API in the Job spec.
export SUNLIT_WORKER_ID="${SUNLIT_WORKER_ID:-${HOSTNAME:-worker-$RANDOM}}"

# -----------------------------------------------------------------------------
# Wait for the database.
#
# On a cold cluster start, 50 workers and PgBouncer/Postgres come up
# concurrently. Without this, every worker crash-loops for the first minute and
# Kubernetes applies exponential backoff — so the fleet takes far longer to
# converge than the database took to become ready. Polling here is strictly
# better than letting CrashLoopBackOff mediate startup ordering.
#
# Uses bash's /dev/tcp rather than pg_isready so the image needs no postgres
# client package. This only proves the port is accepting connections, which is
# exactly the condition we are waiting on; the player does real auth after.
# -----------------------------------------------------------------------------
wait_for_db() {
    local host="$1" port="$2" timeout="${3:-180}"
    local waited=0 interval=2

    log "waiting for ${host}:${port} (timeout ${timeout}s)"
    while (( waited < timeout )); do
        if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
            exec 3<&- 2>/dev/null || true
            exec 3>&- 2>/dev/null || true
            log "database reachable after ${waited}s"
            return 0
        fi
        sleep "$interval"
        waited=$(( waited + interval ))
    done

    die "database ${host}:${port} not reachable after ${timeout}s"
}

# Only the COORDINATOR is waited for. The shards are reached later, after the worker
# has read the routing map — and a shard that is slow to start is a per-task failure
# the lease recovers, not a reason to hold up a pod that could be working on another
# shard's sections meanwhile.
wait_for_db "${SUNLIT_COORD_HOST}" "${SUNLIT_COORD_PORT:-6432}" "${SUNLIT_DB_WAIT_SECONDS:-300}"

# -----------------------------------------------------------------------------
# Launch
# -----------------------------------------------------------------------------
[[ -x "$PLAYER" ]] || die "player not executable at ${PLAYER}"

log "starting worker: run=${SUNLIT_RUN_ID} worker=${SUNLIT_WORKER_ID} coordinator=${SUNLIT_COORD_HOST}:${SUNLIT_COORD_PORT:-6432}"

# -batchmode  : no window, no interactive prompts
# -nographics : do not initialise a graphics device at all. Belt-and-braces
#               alongside the Server subtarget — if the image were ever built with
#               the wrong subtarget, this keeps it from probing for a GPU.
# -logFile /dev/stdout : Unity's log goes to the container log stream, where
#               kubectl logs and any collector can see it. Without this Unity
#               writes to ~/.config/unity3d/... inside the container and the pod
#               appears silent.
# -nolog is deliberately NOT used: we want the log.
#
# Run in background + `wait` rather than `exec`, because `exec` would replace this
# shell and destroy the signal traps installed above.
"$PLAYER" \
    -batchmode \
    -nographics \
    -logFile /dev/stdout \
    &
CHILD_PID=$!
log "player pid ${CHILD_PID}"

# `wait` returns immediately when a trapped signal arrives, so we must re-wait
# until the child is genuinely gone. Without that, a forwarded SIGTERM would make
# this script exit while the player was still flushing and releasing its lease.
#
# The unconditional first `wait` matters: initialising EXIT_CODE from the loop
# alone would leave it unset whenever the player exits before the loop's first
# `kill -0` check (a fast fatal-config exit), and `set -u` would then abort here
# with "unbound variable" — masking the real error the player just printed.
EXIT_CODE=0
set +e
wait "$CHILD_PID"
EXIT_CODE=$?
while kill -0 "$CHILD_PID" 2>/dev/null; do
    wait "$CHILD_PID"
    EXIT_CODE=$?
done
set -e

log "player exited with ${EXIT_CODE}"

# Exit code semantics, consumed by the Job's backoffLimit:
#   0   drained cleanly (queue empty) -> counts as a Job completion
#   1   fatal misconfiguration        -> retrying will not help; surfaces in events
#   143 SIGTERM (128+15)              -> normal for preemption; task lease released
exit "${EXIT_CODE}"
