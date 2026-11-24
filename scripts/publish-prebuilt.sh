#!/usr/bin/env bash
# Attach an already-built ARMv6 tarball to a GitHub Release.
# This script does not compile. The bytes come from a successful prebuilt
# workflow run for SOURCE_SHA, and they must match prebuilt/armv6.env on
# origin/main. If the release already exists, the script exits rather than
# replacing an asset.
set -euo pipefail

if [[ -z "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]]; then
  echo "GH_TOKEN is not set." >&2
  exit 1
fi
export GH_TOKEN="${GH_TOKEN:-$GITHUB_TOKEN}"

repo="${GITHUB_REPOSITORY:?}"
event_path="${GITHUB_EVENT_PATH:?}"
event_name="${GITHUB_EVENT_NAME:?}"

if [[ "$event_name" == "workflow_dispatch" ]]; then
  tag="$(python3 -c 'import json,os; d=json.load(open(os.environ["GITHUB_EVENT_PATH"])); print((d.get("inputs") or {}).get("publish_tag") or "")')"
  sha="$(python3 -c 'import json,os; d=json.load(open(os.environ["GITHUB_EVENT_PATH"])); print((d.get("inputs") or {}).get("source_sha") or "")')"
  if [[ -z "$tag" || -z "$sha" ]]; then
    echo "workflow_dispatch publish requires both publish_tag and source_sha." >&2
    echo "source_sha is the commit whose artifact you are publishing, not the ref this dispatch checked out." >&2
    exit 1
  fi
else
  tag="${GITHUB_REF_NAME:?}"
  sha="${GITHUB_SHA:?}"
fi

tag="$(printf '%s' "$tag" | tr -d '[:space:]')"
sha="$(printf '%s' "$sha" | tr '[:upper:]' '[:lower:]')"

if [[ ! "$tag" =~ ^prebuilt- ]]; then
  echo "Refusing to publish '$tag'. The name has to start with prebuilt-." >&2
  exit 1
fi
if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "source SHA must be 40 hex characters, got '$sha'." >&2
  exit 1
fi

git fetch --depth=1 origin main
if ! env_text="$(git show origin/main:prebuilt/armv6.env 2>/dev/null)"; then
  echo "origin/main has no prebuilt/armv6.env. Commit PREBUILT_TAG and PREBUILT_SHA256 on main before publishing." >&2
  exit 1
fi
tag_line="$(printf '%s\n' "$env_text" | sed -n 's/^PREBUILT_TAG=//p' | head -n 1 | tr -d '[:space:]')"
sha_line="$(printf '%s\n' "$env_text" | sed -n 's/^PREBUILT_SHA256=//p' | head -n 1 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
if [[ "$tag_line" != "$tag" ]]; then
  echo "origin/main PREBUILT_TAG is '${tag_line:-empty}', not $tag." >&2
  echo "Commit that tag name on main, then publish. Do not publish a different name." >&2
  exit 1
fi
if [[ ! "$sha_line" =~ ^[0-9a-f]{64}$ ]]; then
  echo "origin/main PREBUILT_SHA256 is not 64 hex digits." >&2
  exit 1
fi

run_ids=()
mapfile -t run_ids < <(
  gh run list \
    --repo "$repo" \
    --workflow prebuilt.yml \
    --commit "$sha" \
    --status success \
    --limit 50 \
    --json databaseId,headSha \
    --jq '.[] | select(.headSha == "'"$sha"'") | .databaseId'
)
if [[ ${#run_ids[@]} -eq 0 ]]; then
  echo "No successful prebuilt run for $sha. Build that commit first. This job does not compile." >&2
  exit 1
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
chosen=""
chosen_id=""
for id in "${run_ids[@]}"; do
  names="$(gh api "repos/${repo}/actions/runs/${id}/artifacts" --jq '.artifacts[].name')"
  if ! grep -qx 'sideload-pi-prebuilt-armv6' <<<"$names"; then
    echo "Run $id has no sideload-pi-prebuilt-armv6 artifact. Skipping."
    continue
  fi
  dir="$stage/$id"
  mkdir -p "$dir"
  gh run download "$id" --repo "$repo" --name sideload-pi-prebuilt-armv6 --dir "$dir"
  if ! (cd "$dir" && sha256sum -c sideload-pi-prebuilt-armv6.tar.gz.sha256); then
    echo "Run $id artifact failed its own sha256sum -c." >&2
    continue
  fi
  got="$(awk 'NR==1 {print $1}' "$dir/sideload-pi-prebuilt-armv6.tar.gz.sha256" | tr '[:upper:]' '[:lower:]')"
  if [[ "$got" != "$sha_line" ]]; then
    echo "Run $id digest $got does not match origin/main PREBUILT_SHA256 $sha_line." >&2
    continue
  fi
  chosen="$dir"
  chosen_id="$id"
  break
done
if [[ -z "$chosen" ]]; then
  echo "No artifact for $sha matches the PREBUILT_SHA256 committed on origin/main." >&2
  exit 1
fi
echo "Publishing artifact from run $chosen_id (sha256 $sha_line)."

if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
  echo "Release $tag already exists. Refusing to replace its assets." >&2
  exit 1
fi

target_args=()
if existing="$(gh api --jq .sha "repos/${repo}/commits/${tag}" 2>/dev/null)"; then
  existing="$(printf '%s' "$existing" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  if [[ "$existing" != "$sha" ]]; then
    echo "Tag $tag points at $existing, not $sha. Not creating a release for the wrong commit." >&2
    exit 1
  fi
  target_args=(--verify-tag)
else
  # workflow_dispatch may be the thing that creates the tag. Point it at the
  # built commit, not at whatever main happens to be.
  target_args=(--target "$sha")
fi

gh release create "$tag" \
  --repo "$repo" \
  --title "$tag" \
  "${target_args[@]}" \
  --notes "ARMv6 phone tools from the already-built artifact of ${sha} (workflow run ${chosen_id}). This job did not compile." \
  "$chosen/sideload-pi-prebuilt-armv6.tar.gz" \
  "$chosen/sideload-pi-prebuilt-armv6.tar.gz.sha256"
