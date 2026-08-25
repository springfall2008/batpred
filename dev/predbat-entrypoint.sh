#!/bin/sh
# Waits for the ha-bootstrap container to publish a token, renders apps.yaml from
# the dev/apps.dev.yaml.tmpl template, then hands off to Predbat's standalone runner.
set -eu

TOKEN_FILE="${TOKEN_FILE:-/shared/ha_token.json}"
APPS_TEMPLATE="${APPS_TEMPLATE:-/app/apps.dev.yaml.tmpl}"
APPS_FILE="${PREDBAT_APPS_FILE:-/app/apps.yaml}"

echo "Waiting for ${TOKEN_FILE}..."
until [ -f "$TOKEN_FILE" ]; do
    sleep 1
done

python3 - "$TOKEN_FILE" "$APPS_TEMPLATE" "$APPS_FILE" <<'EOF'
import json
import sys

token_file, template_file, out_file = sys.argv[1:4]

with open(token_file) as f:
    token = json.load(f)

with open(template_file) as f:
    rendered = f.read()

rendered = rendered.replace("{{HA_URL}}", token["ha_url"]).replace("{{HA_KEY}}", token["ha_key"])

with open(out_file, "w") as f:
    f.write(rendered)
EOF

echo "Wrote ${APPS_FILE}, starting Predbat..."
export PREDBAT_APPS_FILE="$APPS_FILE"
cd /app
exec python3 predbat/hass.py
