#!/usr/bin/env bash
# Release astrameter from develop: bump version, finalize CHANGELOG, merge to main, tag, sync develop.
# Requires: git, yq (https://github.com/mikefarah/yq), clean develop, origin/develop up to date.
# Usage: ./release.sh X.Y.Z

set -euo pipefail

trap 'echo "[ERROR] release.sh failed at line ${LINENO:-?}. Check git status and branches before retrying." >&2' ERR

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [ -z "${1:-}" ]; then
  print_error "Usage: $0 <version>"
  print_info "Example: $0 1.2.0"
  print_info "Run from a clean develop branch, synced with origin/develop."
  exit 1
fi

VERSION="$1"
RELEASE_BRANCH="release/v${VERSION}"

if ! command -v yq >/dev/null 2>&1; then
  print_error "yq is required. Install: https://github.com/mikefarah/yq"
  exit 1
fi

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  print_error "Version must be semantic (e.g. 1.2.3), no v prefix."
  exit 1
fi

CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "develop" ]; then
  print_error "You must be on branch develop (current: $CURRENT_BRANCH)"
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  print_error "Working tree is not clean."
  git status --short
  exit 1
fi

print_info "Fetching origin..."
git fetch origin

LOCAL=$(git rev-parse develop)
REMOTE=$(git rev-parse origin/develop)
if [ "$LOCAL" != "$REMOTE" ]; then
  print_error "develop is not up to date with origin/develop. Run: git pull origin develop"
  exit 1
fi

if git show-ref --verify --quiet "refs/heads/$RELEASE_BRANCH"; then
  print_error "Branch $RELEASE_BRANCH already exists locally."
  exit 1
fi

if [ -n "$(git ls-remote --heads origin "$RELEASE_BRANCH" 2>/dev/null)" ]; then
  print_error "Branch $RELEASE_BRANCH already exists on origin."
  exit 1
fi

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
/^## Next$/ { in=1; found=0; next }
in && /^## / { exit !found }
in && /^[[:space:]]*- / { found=1 }
END { if (in) exit !found; exit 0 }
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

print_info "Setting version in pyproject.toml"
sed -i.bak "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
rm -f pyproject.toml.bak

print_info "Setting ha_addon/config.yaml version"
yq eval --inplace ".version = \"$VERSION\"" ha_addon/config.yaml

print_info "Renaming ## Next to ## $VERSION in CHANGELOG.md"
LINE=$(grep -n '^## Next$' CHANGELOG.md | head -1 | cut -d: -f1)
sed -i.bak "${LINE}s/^## Next$/## $VERSION/" CHANGELOG.md
rm -f CHANGELOG.md.bak

git add pyproject.toml ha_addon/config.yaml CHANGELOG.md
git commit -m "Release v${VERSION}

- Set version in pyproject.toml and ha_addon/config.yaml
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

print_info "Merging main into develop"
git checkout develop
git pull origin develop

if ! git merge main --no-ff -m "Sync develop with main after release v${VERSION}"; then
  print_error "Merge main into develop failed. Resolve manually."
  exit 1
fi

print_info "Setting ha_addon/config.yaml version to next on develop"
yq eval --inplace '.version = "next"' ha_addon/config.yaml

needs_commit=false
if ! git diff --quiet ha_addon/config.yaml; then
  git add ha_addon/config.yaml
  needs_commit=true
fi

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
  git add CHANGELOG.md
  needs_commit=true
fi

if [ "$needs_commit" = true ]; then
  git commit -m "Prepare develop for next release (add-on next, ## Next)"
fi

print_info "Pushing develop"
git push origin develop

print_success "Release v${VERSION} complete."
print_info "Summary: branch $RELEASE_BRANCH kept; main and tag $VERSION pushed; develop updated."
