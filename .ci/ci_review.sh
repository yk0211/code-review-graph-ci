#!/bin/bash
# CI AI Review runner — auto-detect CI environment and run review.py
# Supports: GitLab CI, GitHub Actions, Jenkins, manual
#
# Usage:
#   bash ci_review.sh                        # auto-detect CI env
#   bash ci_review.sh <sha1> <sha2>          # manual: review sha2..sha1 diff

set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

# --- OS-aware Python detection ---
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) PYTHON="python"   ;;
    *)                      PYTHON="python3" ;;
esac

# --- Check required commands ---
if ! command -v git &>/dev/null; then
    echo "[CI Review] ERROR: 'git' not found. Please install Git." >&2
    exit 1
fi
if ! command -v "$PYTHON" &>/dev/null; then
    echo "[CI Review] ERROR: '$PYTHON' not found. Please install Python 3." >&2
    exit 1
fi

# --- Detect remote default branch (main / master / whatever) ---
detect_base_branch() {
    # 0. Check if remote 'origin' exists at all
    if ! git remote get-url origin >/dev/null 2>&1; then
        echo ""
        return
    fi
    # 1. GitLab built-in variable
    if [ -n "${CI_DEFAULT_BRANCH:-}" ]; then
        echo "$CI_DEFAULT_BRANCH"
        return
    fi
    # 2. Read remote HEAD pointer
    local ref
    ref=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null)
    if [ -n "$ref" ]; then
        echo "${ref#refs/remotes/origin/}"
        return
    fi
    # 3. Guess from available remote branches
    for name in main master; do
        if git rev-parse "origin/$name" >/dev/null 2>&1; then
            echo "$name"
            return
        fi
    done
    # 4. Remote exists but no branches fetched yet
    echo ""
}

BASE_BRANCH=$(detect_base_branch)
ZERO_SHA="0000000000000000000000000000000000000000"

# --- Determine diff range ---
if [ $# -ge 2 ]; then
    # Manual mode
    LOCAL_SHA="$1"
    REMOTE_SHA="$2"

elif [ -n "$CI_COMMIT_SHA" ]; then
    # GitLab CI
    LOCAL_SHA="$CI_COMMIT_SHA"
    if [ -n "$BASE_BRANCH" ]; then
        # CI_COMMIT_BEFORE_SHA may be all-zeros for the first push of a new branch
        if [ -z "$CI_COMMIT_BEFORE_SHA" ] || [ "$CI_COMMIT_BEFORE_SHA" = "$ZERO_SHA" ]; then
            REMOTE_SHA="$(git merge-base origin/$BASE_BRANCH HEAD 2>/dev/null || echo "$ZERO_SHA")"
        else
            REMOTE_SHA="$CI_COMMIT_BEFORE_SHA"
        fi
    else
        REMOTE_SHA="$ZERO_SHA"
    fi
    echo "[CI Review] GitLab CI | base=${BASE_BRANCH:-zero_sha}"

elif [ -n "$GITHUB_SHA" ]; then
    # GitHub Actions
    LOCAL_SHA="$GITHUB_SHA"
    if [ -n "$BASE_BRANCH" ]; then
        if [ -z "$GITHUB_BEFORE_SHA" ] || [ "$GITHUB_BEFORE_SHA" = "$ZERO_SHA" ]; then
            REMOTE_SHA="$(git merge-base origin/$BASE_BRANCH HEAD 2>/dev/null || echo "$ZERO_SHA")"
        else
            REMOTE_SHA="$GITHUB_BEFORE_SHA"
        fi
    else
        REMOTE_SHA="$ZERO_SHA"
    fi
    echo "[CI Review] GitHub Actions | base=${BASE_BRANCH:-zero_sha}"

elif [ -n "$GIT_COMMIT" ]; then
    # Jenkins
    LOCAL_SHA="$GIT_COMMIT"
    if [ -n "$BASE_BRANCH" ]; then
        if [ -z "$GIT_PREVIOUS_COMMIT" ] || [ "$GIT_PREVIOUS_COMMIT" = "$ZERO_SHA" ]; then
            REMOTE_SHA="$(git merge-base origin/$BASE_BRANCH HEAD 2>/dev/null || echo "$ZERO_SHA")"
        else
            REMOTE_SHA="$GIT_PREVIOUS_COMMIT"
        fi
    else
        REMOTE_SHA="$ZERO_SHA"
    fi
    echo "[CI Review] Jenkins | base=${BASE_BRANCH:-zero_sha}"

else
    echo "[CI Review] ERROR: No CI environment detected and no args given." >&2
    echo "  Usage: bash ci_review.sh <latest_sha> <base_sha>" >&2
    exit 1
fi

echo "[CI Review] Diff range: ${REMOTE_SHA:0:8}..${LOCAL_SHA:0:8}"

# --- Run review ---
$PYTHON "$SCRIPT_DIR/review.py" "$LOCAL_SHA" "$REMOTE_SHA"
