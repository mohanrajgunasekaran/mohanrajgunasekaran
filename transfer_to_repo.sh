#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./transfer_to_repo.sh https://github.com/<user>/election-affidavit-downloader-2026.git
#
# This script copies the generated project files from this repository into
# a separate target repository and creates a commit there.

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <target-repo-url>"
  exit 1
fi

TARGET_REPO_URL="$1"
WORKDIR="$(mktemp -d)"
TARGET_DIR="$WORKDIR/target"

cleanup() {
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "[1/6] Cloning target repository..."
git clone "$TARGET_REPO_URL" "$TARGET_DIR"

echo "[2/6] Copying files..."
cp -f main.py "$TARGET_DIR/main.py"
cp -f config.yaml "$TARGET_DIR/config.yaml"
cp -f requirements.txt "$TARGET_DIR/requirements.txt"
cp -f .env.example "$TARGET_DIR/.env.example"
cp -f README.md "$TARGET_DIR/README.md"

cd "$TARGET_DIR"

echo "[3/6] Staging files..."
git add main.py config.yaml requirements.txt .env.example README.md

if git diff --cached --quiet; then
  echo "No changes to commit in target repo."
  exit 0
fi

echo "[4/6] Creating commit..."
git commit -m "Import Playwright ECI affidavit downloader project files"

echo "[5/6] Pushing to origin..."
git push origin HEAD

echo "[6/6] Done."
