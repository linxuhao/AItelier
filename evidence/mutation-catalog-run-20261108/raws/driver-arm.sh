#!/bin/bash
# usage: arm.sh <container-name> <scratch-dir> <out-raw-file> <pytest ids...>
# Runs pytest in a disposable container (no --rm combined with timeout; detached, polled, logged, removed).
NAME="$1"; SCR="$2"; OUT="$3"; shift 3
docker rm -f "$NAME" >/dev/null 2>&1
docker run -d --name "$NAME" --network none --cpus 2 --memory 2g --init -u 1000:1000 \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -v "$SCR":/w -w /w -e PYTHONPATH=/w \
  aitelier:latest /usr/local/bin/python -m pytest -q -p no:cacheprovider "$@" >/dev/null || { echo "RUN_FAILED_TO_START"; exit 97; }
while true; do
  ST=$(docker inspect -f '{{.State.Status}}' "$NAME")
  [ "$ST" = "exited" ] && break
  [ "$ST" = "dead" ] && break
  sleep 3
done
RC=$(docker inspect -f '{{.State.ExitCode}}' "$NAME")
docker logs "$NAME" > "$OUT" 2>&1
docker rm -f "$NAME" >/dev/null
echo "RC=$RC"
exit 0
