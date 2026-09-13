#!/bin/sh
# Pick the interpreter AstraMeter runs under, then hand over.
#
# The image ships two interpreters: the ordinary one, and a copy carrying
# CAP_NET_BIND_SERVICE so the non-root user can bind the privileged port the
# Shelly HTTP surface uses. The capability is on a *copy* rather than on the
# interpreter itself because a file capability the container is not permitted
# to use makes execve fail outright — so putting it on the only interpreter
# would stop a capability-dropping deployment from starting at all, rather
# than merely leaving it unable to bind port 80.
#
# Anything that is not the app's own entry point is executed verbatim, so
# `docker run <image> sh`, a direct interpreter path and a custom command all
# behave exactly as they did before this script existed.
set -e

VENV_BIN=/app/.venv/bin
CAPABLE="$VENV_BIN/python-cap"

interpreter() {
    # The probe is the point: under `--cap-drop` the exec itself fails, and
    # falling back keeps the container running (the port bind then fails with
    # a logged error, which is recoverable) instead of never starting.
    if [ -x "$CAPABLE" ] && "$CAPABLE" -c "" 2>/dev/null; then
        printf '%s' "$CAPABLE"
    else
        printf '%s' "$VENV_BIN/python"
    fi
}

case "$1" in
    astrameter | astra-sim)
        script="$VENV_BIN/$1"
        shift
        exec "$(interpreter)" "$script" "$@"
        ;;
    "")
        exec "$(interpreter)" "$VENV_BIN/astrameter"
        ;;
    -*)
        # Flags only: the app is implied.
        exec "$(interpreter)" "$VENV_BIN/astrameter" "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
