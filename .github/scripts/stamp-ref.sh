#!/usr/bin/env bash
# Rewrite the committed @develop / blob/develop GitHub refs in web/ to a given
# ref, so a deployed build links back to its own ref:
#   main    -> @main      / blob/main/
#   develop -> @develop    (unchanged)
#   PR      -> @<head>     / blob/<head>/
# Run against the ephemeral CI checkout before publishing — never committed.
set -euo pipefail

ref="${1:?usage: stamp-ref.sh <git-ref>}"
# Escape sed replacement specials (& whole-match, # delimiter). Branch names may
# contain '/', which is fine with the '#' delimiter below.
esc=$(printf '%s' "$ref" | sed -e 's/[&#]/\\&/g')

find web -type f \( -name '*.html' -o -name '*.js' \) -print0 |
  xargs -0 sed -i \
    -e "s#astrameter@develop#astrameter@${esc}#g" \
    -e "s#astrameter/blob/develop/#astrameter/blob/${esc}/#g" \
    -e "s#astrameter/tree/develop/#astrameter/tree/${esc}/#g"

echo "Stamped GitHub ref into web/: ${ref}"
