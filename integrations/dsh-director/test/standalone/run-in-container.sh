#!/bin/sh
# Plugin step 5: run scenario.mjs in a disposable container that holds nothing but
# the pinned DSH install and this plugin. The image's own application code (/app) is
# masked by an empty tmpfs, there is no network, and the scenario starts from an
# empty environment (env -i) so no variable the image sets can reach it.
#   usage: run-in-container.sh <dsh node_modules dir> <image with node 24, python3 + PyYAML and git>
set -eu
modules=${1:?usage: run-in-container.sh <dsh node_modules dir> <image>}
image=${2:?usage: run-in-container.sh <dsh node_modules dir> <image>}
plugin=$(cd "$(dirname "$0")/../.." && pwd)
exec docker run --rm --network none --cpus 2 --memory 2g -u "$(id -u):$(id -g)" --init \
  --mount type=tmpfs,destination=/app \
  -v "$modules:/opt/dsh/node_modules:ro" \
  -v "$plugin:/opt/dsh-director:ro" \
  -w /tmp "$image" \
  sh -c 'if [ -n "$(ls -A /app)" ]; then echo "precondition: /app is not empty" >&2; exit 3; fi
    exec env -i PATH="$PATH" HOME=/tmp PYTHONDONTWRITEBYTECODE=1 STANDALONE_PYTHON=python3 \
      node /opt/dsh-director/test/standalone/scenario.mjs /opt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js'
