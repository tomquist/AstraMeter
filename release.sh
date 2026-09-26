#!/usr/bin/env bash
# Release astrameter from develop: bump version, finalize CHANGELOG, merge to main, tag,
# and open a pull request that prepares develop for the next release.
# Requires: git, yq (https://github.com/mikefarah/yq), clean develop, origin/develop up to date.
# Optional: gh (https://cli.github.com/) to open the develop pull request; without it the
# script prints the link to open it by hand.
# Usage: ./release.sh X.Y.Z
#
# develop only accepts squash-merged pull requests, so main never becomes an ancestor of
# develop. Each release therefore merges main into its release branch first: the merge
# into main is then conflict-free, and the develop sync can be squashed like any other PR.

set -euo pipefail

trap 'echo "[ERROR] release.sh failed at line ${LINENO:-?}. Check git status and branches before retrying." >&2' ERR

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Files carrying a copy-paste `github://tomquist/astrameter@<ref>` external_components
# snippet that must track the release (tag at release time, "develop" in between).
COMPONENT_REF_FILES=(esphome.example.yaml docs/esphome-powermeters.md docs/installation/esphome.md)

# Point the ESPHome external_components ref in the docs/examples above at the
# given git ref (a tag at release time, "develop" between releases).
set_component_ref() {
  local ref="$1"
  sed -i.bak -E "s#(github://tomquist/astrameter@)[^[:space:]]+#\1${ref}#g" \
    "${COMPONENT_REF_FILES[@]}"
  rm -f "${COMPONENT_REF_FILES[@]/%/.bak}"
}

if [ -z "${1:-}" ]; then
  print_error "Usage: $0 <version>"
  print_info "Example: $0 1.2.0"
  print_info "Run from a clean develop branch, synced with origin/develop."
  exit 1
fi

VERSION="$1"
RELEASE_BRANCH="release/v${VERSION}"
SYNC_BRANCH="release/v${VERSION}-develop"

if ! command -v yq >/dev/null 2>&1; then
  print_error "yq is required. Install: https://github.com/mikefarah/yq"
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  print_error "uv is required (to refresh uv.lock). Install: https://docs.astral.sh/uv/"
  exit 1
fi

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  print_error "Version must be semantic (e.g. 1.2.3), no v prefix."
  exit 1
fi

print_info "Fetching origin..."
git fetch origin

# GitHub Actions checkout commonly leaves detached HEAD at origin/develop; only the SHA must match.
CURRENT_BRANCH=$(git branch --show-current)
if [ -n "$CURRENT_BRANCH" ] && [ "$CURRENT_BRANCH" != "develop" ]; then
  print_error "You must be on branch develop (current: $CURRENT_BRANCH)"
  exit 1
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/develop)
if [ "$LOCAL" != "$REMOTE" ]; then
  print_error "HEAD is not at origin/develop. Run: git pull origin develop"
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  print_error "Working tree is not clean."
  git status --short
  exit 1
fi

if git show-ref --verify --quiet "refs/heads/$RELEASE_BRANCH"; then
  print_error "Branch $RELEASE_BRANCH already exists locally."
  exit 1
fi

for branch in "$RELEASE_BRANCH" "$SYNC_BRANCH"; do
  if [ -n "$(git ls-remote --heads origin "$branch" 2>/dev/null)" ]; then
    print_error "Branch $branch already exists on origin."
    exit 1
  fi
done

if git show-ref --verify --quiet "refs/tags/$VERSION"; then
  print_error "Tag $VERSION already exists."
  exit 1
fi

if ! grep -q '^## Next$' CHANGELOG.md; then
  print_error "CHANGELOG.md must contain a line exactly: ## Next"
  exit 1
fi

# Require at least one list item under ## Next (before the next ## heading)
if ! awk '
/^## Next$/ { in_next=1; found=0; next }
in_next && /^## / { exit !found }
in_next && /^[[:space:]]*- / { found=1 }
END { if (in_next) exit !found; exit 0 }
' CHANGELOG.md; then
  print_error "## Next must include at least one bullet line (e.g. lines starting with \"- \")."
  exit 1
fi

CURRENT_VER=$(grep -E '^version = ' pyproject.toml | head -1 | sed -E 's/^version = "([^"]+)".*/\1/')
if [ -z "$CURRENT_VER" ]; then
  print_error "Could not read version from pyproject.toml (expected: version = \"…\")."
  exit 1
fi

if [ "$VERSION" = "$CURRENT_VER" ]; then
  print_error "Release version $VERSION equals current pyproject.toml version. Bump to a newer version."
  exit 1
fi

if [ "$(printf '%s\n' "$CURRENT_VER" "$VERSION" | sort -V | tail -1)" != "$VERSION" ]; then
  print_error "Release version $VERSION must be strictly greater than pyproject.toml version $CURRENT_VER."
  exit 1
fi

print_info "Pre-checks passed. Starting release $VERSION"

print_info "Creating branch $RELEASE_BRANCH"
git checkout -b "$RELEASE_BRANCH"

# Bring in main's history (the previous release merge, any hotfixes) so main is an
# ancestor of the release. Nothing is pushed yet, so any doubt aborts here.
if ! git merge-base --is-ancestor origin/main HEAD; then
  print_info "Merging origin/main into $RELEASE_BRANCH"

  # A line-level merge of CHANGELOG.md goes wrong (it renames develop's new
  # "## Next" to the previous release's heading), so develop's copy is kept. That
  # is only safe when it already holds every line of main's.
  missing=$(grep -vxF -f <(git show "$LOCAL:CHANGELOG.md") <(git show origin/main:CHANGELOG.md) |
    grep -v '^[[:space:]]*$' || true)
  if [ -n "$missing" ]; then
    print_error "CHANGELOG.md on main has lines develop lacks. Merge the previous release's develop sync PR, or port main's changes to develop, then rerun:"
    printf '  %s\n' "$missing" >&2
    exit 1
  fi

  git merge origin/main --no-ff --no-commit >/dev/null || true
  if ! git rev-parse -q --verify MERGE_HEAD >/dev/null; then
    print_error "Merging origin/main into $RELEASE_BRANCH failed. Nothing was pushed."
    exit 1
  fi
  git checkout "$LOCAL" -- CHANGELOG.md
  # uv.lock is regenerated by `uv lock` below; any other conflict needs a person.
  if git diff --name-only --diff-filter=U | grep -qxF uv.lock; then
    git checkout "$LOCAL" -- uv.lock
  fi
  conflicts=$(git diff --name-only --diff-filter=U)
  if [ -n "$conflicts" ]; then
    print_error "main conflicts with develop in these files. Nothing was pushed; resolve by hand:"
    printf '  %s\n' "$conflicts" >&2
    git merge --abort
    exit 1
  fi
  git commit --no-edit -m "Merge main into release v${VERSION}"
fi

print_info "Setting version in pyproject.toml"
sed -i.bak "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
rm -f pyproject.toml.bak

print_info "Refreshing uv.lock for $VERSION"
uv lock

print_info "Setting ha_addon/config.yaml version"
yq eval --inplace ".version = \"$VERSION\"" ha_addon/config.yaml

print_info "Pinning ESPHome external_components ref to $VERSION"
set_component_ref "$VERSION"

print_info "Renaming ## Next to ## $VERSION in CHANGELOG.md"
LINE=$(grep -n '^## Next$' CHANGELOG.md | head -1 | cut -d: -f1)
sed -i.bak "${LINE}s/^## Next$/## $VERSION/" CHANGELOG.md
rm -f CHANGELOG.md.bak

git add pyproject.toml uv.lock ha_addon/config.yaml "${COMPONENT_REF_FILES[@]}" CHANGELOG.md
git commit -m "Release v${VERSION}

- Set version in pyproject.toml, uv.lock and ha_addon/config.yaml
- Pin ESPHome external_components ref to v${VERSION}
- Finalize CHANGELOG for v${VERSION}"

print_success "Release commit created on $RELEASE_BRANCH"

print_info "Pushing $RELEASE_BRANCH to origin"
git push origin "$RELEASE_BRANCH"

print_info "Checking out main and merging $RELEASE_BRANCH"
git checkout main
git pull origin main

if ! git merge "$RELEASE_BRANCH" --no-ff -m "Merge release v${VERSION}"; then
  if git status --short | grep -q "^UU ha_addon/config.yaml"; then
    print_info "Merge conflict in ha_addon/config.yaml; using release branch version."
    git checkout --theirs ha_addon/config.yaml
    git add ha_addon/config.yaml
    git commit --no-edit
  else
    print_error "Merge failed with conflicts. Resolve manually and resume, or reset."
    exit 1
  fi
fi

print_info "Tagging $VERSION"
git tag "$VERSION"

print_info "Pushing main and tag $VERSION"
git push origin main
git push origin "$VERSION"

print_info "Preparing $SYNC_BRANCH for develop"
git checkout -b "$SYNC_BRANCH" "$RELEASE_BRANCH"

print_info "Setting ha_addon/config.yaml version to next"
yq eval --inplace '.version = "next"' ha_addon/config.yaml

print_info "Resetting ESPHome external_components ref to develop"
set_component_ref "develop"

if ! grep -q '^## Next$' CHANGELOG.md; then
  print_info "Prepending ## Next to CHANGELOG.md"
  tmp=$(mktemp)
  {
    head -n1 CHANGELOG.md
    echo ""
    echo "## Next"
    echo ""
    tail -n +2 CHANGELOG.md
  } >"$tmp"
  mv "$tmp" CHANGELOG.md
fi

git add ha_addon/config.yaml "${COMPONENT_REF_FILES[@]}" CHANGELOG.md
if ! git diff --cached --quiet; then
  git commit -m "Prepare develop for next release (add-on next, ## Next)"
fi

# main and the tag are already out, and the caller still has to publish the GitHub
# Release, so a failed push or PR is reported for fixing by hand, never fatal.
print_info "Pushing $SYNC_BRANCH"
if ! git push origin "$SYNC_BRANCH"; then
  print_error "Could not push $SYNC_BRANCH. Push it by hand (or recreate it from tag $VERSION: config.yaml version \"next\", ESPHome refs @develop, a new ## Next) and open a pull request into develop."
else
  SYNC_TITLE="Sync develop with release v${VERSION}"
  SYNC_BODY="Brings v${VERSION}'s version bump and finalized CHANGELOG back to develop, sets the add-on version to \`next\`, points the ESPHome external_components ref back at \`develop\`, and opens a new \`## Next\` section. Squash-merge as usual."
  if command -v gh >/dev/null 2>&1 &&
    gh pr create --base develop --head "$SYNC_BRANCH" --title "$SYNC_TITLE" --body "$SYNC_BODY"; then
    print_success "Opened pull request $SYNC_BRANCH -> develop."
  else
    REPO_URL=$(git remote get-url origin | sed -E 's#^git@github\.com:#https://github.com/#; s#\.git$##')
    print_error "Could not open the develop pull request. Open it by hand:"
    echo "  ${REPO_URL}/compare/develop...${SYNC_BRANCH}?expand=1"
  fi
fi

print_success "Release v${VERSION} complete."
print_info "Summary: main and tag $VERSION pushed; merge the $SYNC_BRANCH pull request to update develop."
