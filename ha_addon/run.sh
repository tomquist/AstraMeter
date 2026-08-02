#!/usr/bin/with-contenv bashio
set -e

# Redact secrets from everything this script and its children log. The Python
# app inherits this filtered stdout, so its logs are covered too — a last line
# of defence for values that must never reach a shared debug log, such as the
# MQTT broker password or the Marstek account credentials (see discussion
# #520).
read -r -d '' __ASTRAMETER_REDACT_PY <<'PYEOF' || true
import re, sys

# Stay alive on non-UTF-8 bytes: this filter is in the critical log path, so a
# single undecodable byte must not crash it and break SIGPIPE-sensitive writers.
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

PATTERNS = [
    # JSON: "<...password|token|secret|username|mailbox...>": "<value>"
    (re.compile(
        r'("[A-Za-z0-9_]*'
        r'(?:password|passwd|secret|token|api[_-]?key|username|mailbox)"'
        r'\s*:\s*")[^"]*"', re.I), r'\1REDACTED"'),
    # URI userinfo: scheme://user:pass@host
    (re.compile(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@'),
     r'\1***:***@'),
]

for line in iter(sys.stdin.readline, ''):
    for pat, repl in PATTERNS:
        line = pat.sub(repl, line)
    sys.stdout.write(line)
    sys.stdout.flush()
PYEOF

if [ -z "${ASTRAMETER_NO_LOG_REDACT:-}" ] && command -v python3 >/dev/null 2>&1; then
    export ASTRAMETER_NO_LOG_REDACT=1
    exec > >(python3 -c "$__ASTRAMETER_REDACT_PY") 2>&1
fi

# Everything else — reading the add-on options, generating config.ini,
# resolving the MQTT service and add-on slug, waiting for Home Assistant — is
# done by `astrameter --addon`, where it is covered by unit tests.
. /app/.venv/bin/activate
cd /app

exec astrameter --addon
