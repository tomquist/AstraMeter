#!/usr/bin/env bash
# Rewrite the committed GitHub links in web/ so a deployed build points at its
# own ref:
#   main    -> @main / tree/main / blob/main
#   develop -> unchanged (committed source already uses develop)
#   PR      -> the PR's head branch
# Run against the ephemeral CI checkout before publishing — never committed.
#
# Covers every ref-bearing link shape:
#   github://tomquist/astrameter@develop          (ESPHome external_components)
#   .../astrameter/blob/develop/<path>            (doc file links)
#   .../astrameter/tree/develop/<path>
#   .../astrameter#<anchor>                        (README section links)
#   .../astrameter                                 (bare repo home links)
# Intentionally left ref-agnostic: .../astrameter/issues (not branch-specific).
set -euo pipefail

ref="${1:?usage: stamp-ref.sh <git-ref>}"
# Escape sed replacement specials for the '%' delimiter used below ('%' and the
# whole-match '&'). Branch names may contain '/', which is fine with '%'.
esc=$(printf '%s' "$ref" | sed -e 's/[%&]/\\&/g')

# Order matters: rewrite the more specific shapes (blob/tree/@/#) before the
# bare "astrameter<quote>" home-link rule, so the latter only catches true home
# links (the others now have /blob, /tree, /issues, or # after "astrameter").
find web -type f \( -name '*.html' -o -name '*.js' \) -print0 |
  xargs -0 sed -i \
    -e "s%astrameter/blob/develop/%astrameter/blob/${esc}/%g" \
    -e "s%astrameter/tree/develop/%astrameter/tree/${esc}/%g" \
    -e "s%astrameter@develop%astrameter@${esc}%g" \
    -e "s%astrameter#%astrameter/tree/${esc}#%g" \
    -e "s%astrameter\"%astrameter/tree/${esc}\"%g" \
    -e "s%astrameter'%astrameter/tree/${esc}'%g"

echo "Stamped GitHub ref into web/: ${ref}"
