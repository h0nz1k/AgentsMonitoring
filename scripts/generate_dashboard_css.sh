#!/bin/sh
# Requires the temporary Tailwind CSS 3.4.17 CLI; no project npm dependency.
set -eu
cd "$(dirname "$0")/.."
cli=${1:?Usage: scripts/generate_dashboard_css.sh /path/to/tailwindcss-3.4.17}
input=$(mktemp)
trap 'rm -f "$input"' EXIT HUP INT TERM
printf '@tailwind base;\n@tailwind components;\n@tailwind utilities;\n' > "$input"
"$cli" --input "$input" --content './agentsmon/dashboard.py' --output agentsmon/static/dashboard.css --minify
