#!/bin/sh
# unity-builder entrypoint.
#
# Manual .alf/.ulf activation was discontinued by Unity for Personal licenses, so
# the only headless route for a free account is ONLINE activation with the Unity
# account credentials (the game-ci model). Before starting the server we:
#   1. Force a writable HOME — the image's default HOME (/root) isn't writable by
#      the non-root run user; the LicensingClient and the editor's Library cache
#      both need somewhere to write. Exported BEFORE activation and kept for the
#      server so the play-test subprocess reuses the same cached license.
#   2. Log in with UNITY_EMAIL + UNITY_PASSWORD (secret files) to obtain a Personal
#      entitlement online. On success we touch a marker the server checks; on
#      failure (or no creds) the marker is absent and /playtest reports "skipped"
#      rather than hard-failing — a licensing problem is infra, not a code defect.
#
# Credentials are read from secret FILES (never env), and activation runs to
# completion BEFORE the server (which later executes LLM-generated game code)
# starts — so the password is only in this process's argv during a window when no
# untrusted code is running. The password file at /run/secrets/UNITY_PASSWORD does
# remain readable by the editor uid; that residual exposure is inherent to this
# approach.
set -e

export HOME=/tmp/unity-home
# Pre-create the editor's writable dirs (it creates them non-recursively and fails
# otherwise) plus a throwaway project the activation run can use as its project
# folder — without -projectPath the editor treats CWD (/) as the project, hits a
# "read only project folder" dialog, and aborts rc=1 *after* a successful login.
mkdir -p "$HOME/.local/share/unity3d" "$HOME/.cache/unity3d" \
         "$HOME/.config/unity3d" "$HOME/activation"
# Run from inside the writable activation dir so the editor uses it as the project
# folder (CWD). Passing -projectPath instead double-joins it against CWD.
cd "$HOME/activation"
MARKER="$HOME/.aitelier_licensed"
rm -f "$MARKER"

EMAIL_F=/run/secrets/UNITY_EMAIL
PASS_F=/run/secrets/UNITY_PASSWORD
if [ -s "$EMAIL_F" ] && [ -s "$PASS_F" ]; then
  EMAIL=$(cat "$EMAIL_F")
  echo "unity-builder: activating Unity Personal online for $EMAIL ..."
  # -nographics avoids needing a display; wrap in xvfb-run if present as a fallback
  # for editor paths that still probe for one.
  set +e
  if command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run --auto-servernum --server-args='-screen 0 640x480x24' \
      /opt/unity/Editor/Unity -batchmode -nographics -logFile /tmp/activate.log \
      -username "$EMAIL" -password "$(cat "$PASS_F")" -quit
  else
    /opt/unity/Editor/Unity -batchmode -nographics -logFile /tmp/activate.log \
      -username "$EMAIL" -password "$(cat "$PASS_F")" -quit
  fi
  rc=$?
  set -e
  echo "unity-builder: activation editor exited rc=$rc"
  # Success heuristic: clean exit and no obvious licensing failure in the log.
  # (The exact Unity 6 success string is confirmed empirically on first run; this
  # errs toward marking active on rc=0, and the play-test surfaces any real
  # licensing error in its own log if the heuristic is wrong.)
  if [ "$rc" -eq 0 ] && ! grep -qiE "invalid (credential|username|password)|authentication failed|no valid license|not been activated|two[- ]factor|2fa|unauthorized|access denied" /tmp/activate.log; then
    : > "$MARKER"
    echo "unity-builder: license appears ACTIVE — /playtest enabled."
  else
    echo "unity-builder: activation FAILED — /playtest will report skipped. Log tail:"
    tail -n 30 /tmp/activate.log 2>/dev/null | sed 's/^/  | /'
  fi
else
  echo "unity-builder: no UNITY_EMAIL/UNITY_PASSWORD secrets — /compile works, /playtest will report skipped."
fi

exec python3 /srv/unity_compile.py --serve
