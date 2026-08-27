#!/usr/bin/env bash
# Build the Linux one-file binary into dist/minjpg.
set -euo pipefail

cd "$(dirname "$0")"

VENV="${VENV:-.venv}"
if [ ! -d "$VENV" ]; then
    echo "Creating virtualenv in $VENV"
    python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt

echo "Checking the vendored MozJPEG binary"
"$VENV/bin/python" tools/fetch_cjpeg.py --check

rm -rf build dist
"$VENV/bin/pyinstaller" --noconfirm --clean minjpg.spec

echo
echo "Built: $(pwd)/dist/minjpg  ($(du -h dist/minjpg | cut -f1))"
