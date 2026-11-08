# Who owns the python3.12 coredumps on this host, and what may be done about them

Measurement window: **2026-11-08T20:27:50Z – 2026-11-08T20:31:05Z** (host clock, UTC).
Operator identity for every command below: `uid=1000(linxuhao)`, sudo group member but **no
passwordless sudo** (`sudo -n true` → `sudo: a password is required`, exit 1).
State issue pointer (not amended by this document): `iss-4f3485436afc4e8e`.

Everything under "Remedy" is **PROPOSAL**. No option was applied, no container was touched, no
core or crash file was deleted, moved or re-permissioned. Effects on a service this operator does
not own are the owner's decision.

The re-measurement changed the story in three places, marked ⚠ below:

1. the recorded signal on both 2026-11-08 events is **6 (SIGABRT)**, not SIGSEGV;
2. the 13:50 core on disk is **578,191,360 B smaller** than the size apport logged for it;
3. `/var/log/apport.log` was rotated at 2026-11-08 00:00:01, so no 11-06/11-07 *log* lines exist
   in it — recurrence for those two days rests on core-file mtimes only.

## 1. Owner provenance

### 1.1 The lines that identify the crashing process

Command: `tail -n 60 /var/log/apport.log` (run 2026-11-08T20:27:50Z; the whole file is 11 lines /
1,989 B, `Modify: 2026-11-08 13:52:19.455551802 +0000`, so the tail is the whole log). Verbatim:

```
INFO: apport (pid 4057560) 2026-11-08 13:49:39,489: called for global pid 3844822, signal 6, core limit 18446744073709551615, dump mode 1
ERROR: apport (pid 4057560) 2026-11-08 13:49:39,489: ignoring implausibly big core limit, treating as unlimited
INFO: apport (pid 4057560) 2026-11-08 13:49:39,491: executable: /usr/bin/python3.12 (command line "python -m gunicorn immich_ml.main:app -k immich_ml.config.CustomUvicornWorker -c /usr/src/immich_ml/gunicorn_conf.py -b [::]:3003 -w 1 -t 900 --log-config-json /usr/src/immich_ml/log_conf.json --keep-alive 2 --graceful-timeout 10 --no-control-socket")
INFO: apport (pid 4057560) 2026-11-08 13:50:09,876: wrote report /var/crash/_usr_bin_python3.12.0.crash
INFO: apport (pid 4057560) 2026-11-08 13:50:09,887: writing core dump to /var/lib/apport/coredump/core._usr_bin_python3_12.0.f56f3fd2-7a72-48fe-a8f8-303b4cb54273.3844822.212961837 (limit: -1)
INFO: apport (pid 4057560) 2026-11-08 13:50:18,592: writing core dump /var/lib/apport/coredump/core._usr_bin_python3_12.0.f56f3fd2-7a72-48fe-a8f8-303b4cb54273.3844822.212961837 of size 2725670912
INFO: apport (pid 4078820) 2026-11-08 13:52:19,438: called for global pid 4063523, signal 6, core limit 18446744073709551615, dump mode 1
ERROR: apport (pid 4078820) 2026-11-08 13:52:19,439: ignoring implausibly big core limit, treating as unlimited
INFO: apport (pid 4078820) 2026-11-08 13:52:19,442: executable: /usr/bin/python3.12 (command line "python -m gunicorn immich_ml.main:app -k immich_ml.config.CustomUvicornWorker -c /usr/src/immich_ml/gunicorn_conf.py -b [::]:3003 -w 1 -t 900 --log-config-json /usr/src/immich_ml/log_conf.json --keep-alive 2 --graceful-timeout 10 --no-control-socket")
ERROR: apport (pid 4078820) 2026-11-08 13:52:19,445: this executable already crashed 2 times, ignoring
INFO: apport (pid 4078820) 2026-11-08 13:52:19,456: writing core dump to /var/lib/apport/coredump/core._usr_bin_python3_12.0.f56f3fd2-7a72-48fe-a8f8-303b4cb54273.4063523.221699316 (limit: -1)
```

Fields that carry the identification, one by one:

| field | value |
|---|---|
| executable | `/usr/bin/python3.12` (host path; the container's python binds to the same build id) |
| command line | `python -m gunicorn immich_ml.main:app … -c /usr/src/immich_ml/gunicorn_conf.py -b [::]:3003 -w 1 …` |
| app module | `immich_ml.main:app` — Immich's machine-learning service, not AItelier |
| config path | `/usr/src/immich_ml/gunicorn_conf.py`, `/usr/src/immich_ml/log_conf.json` — the ML image's own layout |
| worker class | `immich_ml.config.CustomUvicornWorker` |
| listen | `[::]:3003` (Immich ML's in-container port) |
| signal | **6** (SIGABRT) on both events ⚠ — not SIGSEGV |
| pids | 3844822 (13:49:39) and 4063523 (13:52:19) |
| core limit seen by apport | `18446744073709551615` (unlimited; apport logs "ignoring implausibly big core limit") |

### 1.2 The container those lines belong to

`docker ps` (2026-11-08T20:28:16Z) lists exactly one Immich machine-learning container:

```
9aabaeb93898   ghcr.io/immich-app/immich-machine-learning:v3.3.1-rocm   "tini -- python -m i…"   3 weeks ago   Up 4 days (healthy)   immich_machine_learning
```

`docker inspect --format 'StartedAt={{.State.StartedAt}} Status={{.State.Status}} RestartCount={{.RestartCount}} Image={{.Config.Image}} ImageID={{.Image}} Health={{.State.Health.Status}}'`
(2026-11-08T20:28:32Z):

| container | StartedAt | Status | RestartCount | Image | ImageID | Health |
|---|---|---|---|---|---|---|
| `immich_machine_learning` | 2026-11-04T09:47:22.262357742Z | running | **0** | `ghcr.io/immich-app/immich-machine-learning:v3.3.1-rocm` | `sha256:0936fa0414903164315c9c80a9f2c2a6ed9b8387ce05decbfe606d1d54e7a2ca` | healthy |
| `immich_server` | 2026-11-04T09:47:22.340060083Z | running | 0 | `ghcr.io/immich-app/immich-server:v3.3.1` | `sha256:db996e352359771c6a3db121a0ed8761516b22f727fc092a0f46dcfef82c1bc1` | healthy |
| `aitelier` (AItelier backend) | 2026-11-08T16:42:30.81503308Z | running | 0 | `aitelier:latest` | `sha256:e3291d5e7660d34f27eb95989bb3c0bfb2a1a74ed75f6d8ae860793871f76849` | healthy |

Image identity, `docker image inspect --format 'RepoTags={{.RepoTags}} RepoDigests={{.RepoDigests}} Id={{.Id}} Created={{.Created}}'`
(2026-11-08T20:28:32Z):

```
ghcr.io/immich-app/immich-machine-learning@sha256:0936fa0414903164315c9c80a9f2c2a6ed9b8387ce05decbfe606d1d54e7a2ca
RepoTags=[ghcr.io/immich-app/immich-machine-learning:v3.3.1-rocm]  Created=2026-10-08T21:58:57.013916375Z
aitelier@sha256:e3291d5e7660d34f27eb95989bb3c0bfb2a1a74ed75f6d8ae860793871f76849
RepoTags=[aitelier:latest aitelier:rollback-17b02488]  Created=2026-11-08T16:36:38.956222431Z
```

So: container `immich_machine_learning`, tag `v3.3.1-rocm`, digest `sha256:0936fa04…a2ca`,
`running`/`healthy`, `RestartCount 0`, up since 2026-11-04T09:47:22Z.

### 1.3 How "this container, not AItelier" was established

Positive evidence (measured):

- The logged command line is a **gunicorn master for `immich_ml.main:app`** with the ML image's
  own config paths under `/usr/src/immich_ml/` and its port `3003`. No AItelier process runs that;
  AItelier's backend container command, from `docker ps`, is `/usr/local/bin/pyth…` (uvicorn), its
  image is `aitelier:latest` (created 2026-11-08T16:36:38Z, digest `e3291d5e…`), and its own
  `StartedAt` is 2026-11-08T16:42:30Z — i.e. **after** both crashes on the same day, so the
  AItelier container that exists now was not even running when they happened.
- Only one container on this host ships `immich_ml` — `immich_machine_learning`.
- `RestartCount 0` with `health=healthy` is exactly what a *respawned gunicorn worker* looks like:
  the master replaces the dead worker, the container never exits, so Docker never counts a restart
  and never marks it unhealthy. The crash is therefore invisible to the container's own health.

Where the attribution is **weaker**, stated plainly:

- Both apport events are 2026-11-08. The other three artifacts (11-06, two on 11-07) have no log
  lines left to read (⚠ log rotated at 00:00:01 on 11-08), and their filenames
  `core._usr_bin_python3_12.0.f56f3fd2-…` encode only *executable* `+ /usr/bin/python3.12` and build
  id `f56f3fd2`. Every host python3.12 crash would produce that same name. For those three files the
  Immich attribution is **pattern inference** (same executable, same store, same daily window),
  not direct evidence.
- I did not read any core, did not read any live process argv, did not `docker exec`, and did not
  read the container's logs — all excluded by this task's read scope.
- **Attribution strength: strong for 2026-11-08 (the log line names the service), inferential for
  11-06/11-07.** It is not the strongest available evidence.

What would make it stronger (each is an owner decision I did not take):

1. read `/var/log/apport.log.1` / `.2.gz` (or the journal) so the 11-06/11-07 events carry their
   own command line — cheapest, no service effect;
2. `docker logs immich_machine_learning` (read-only) to line up worker respawns with 13:26/13:34;
3. `docker inspect` `HostConfig.Ulimits` / `Config.Cmd` to show the in-container core limit;
4. the definitive one: the core's `NT_PRPSINFO` note records the crashed process's argv — root,
   and it means reading a core file, which this task forbids.

## 2. Recurrence

Artifacts by date (stat/`ls -l` only; store `/var/lib/apport/coredump`, mode `0400 root:root`,
dir mode `drwxr-xr-x`; report dir `/var/crash`, mode `drwxrwsrwt`, file `0640 root:root`):

| # | date (UTC) | artifact | bytes | what else is known |
|---|---|---|---|---|
| 1 | 2026-11-06 20:59:56 | `core._usr_bin_python3_12.0.…3229610.204196992` | **0** ⚠ | a core write that produced nothing; no log line survives |
| 2 | 2026-11-07 13:26:18–13:26:24 | `core.…2158310.206996586` | 2,648,535,040 | no log line survives |
| 3 | 2026-11-07 13:33:40–13:34:09 | `core.…3831101.212945710` | 5,481,521,152 | 7m50s after #2; no log line survives |
| 4 | 2026-11-08 13:49:39 | `core.…3844822.212961837` | **2,147,479,552** on disk ⚠ (apport logged 2,725,670,912) | pid 3844822, signal 6, command line above |
| 5 | 2026-11-08 13:50:09 | `/var/crash/_usr_bin_python3.12.0.crash` | 694,695,161 | apport's report for #4 (same pid 3844822) |
| 6 | 2026-11-08 13:52:19–13:52:34 | `core.…4063523.221699316` | 5,790,728,192 | pid 4063523, signal 6; apport refused a *second report* ("this executable already crashed 2 times, ignoring") but still wrote the core |

Store sizes at 2026-11-08T20:28:10Z, unchanged at 20:28:54Z and 20:31:05Z:

- `/var/lib/apport/coredump` — **16,068,284,416 B** (14.96 GiB) in 5 files; `du` agrees with the
  sizes above (e.g. the 13:50 file holds 4,194,304 allocated blocks of 512 B = 2,147,483,648 B for a
  2,147,479,552 B file).
- `/var/crash` — 694,706,176 B (1 file, 694,695,161 B).
- `/var/lib/apport` total — 16,068,288,512 B.

⚠ The one number that moved and is not explained: apport logged `of size 2725670912` for pid
3844822 at 13:50:18, but the file measures 2,147,479,552 B with `Modify: 2026-11-08
13:50:18.596011334 +0000` — 578,191,360 B short. Under this task's read scope I cannot tell whether
that was a truncated write, a sparse/partial dump or a cap; I record the discrepancy rather than
smoothing it. (The 11-07 sizes match the previously recorded figures exactly: 2,648,535,040 and
5,481,521,152.)

### Old transient, or ongoing failure?

**Ongoing.** The basis:

- Four non-zero cores plus one 0-byte core across **three consecutive dates** (11-06, 11-07, 11-08).
- On both complete days the crashes come in a **pair minutes apart inside the 13:2x–13:5x window**
  (11-07: 13:26 and 13:34; 11-08: 13:49 and 13:52). That is the shape of a recurring workload
  burst hitting the same failure, not a one-off.
- The most recent crash is **13:52:19Z**, i.e. **6 h 35 m before** this measurement began
  (20:27:50Z), and no further apport line appeared through 20:31:05Z. A ~7-hour quiet gap after a
  pair is consistent with the daily-window pattern; it is not evidence of repair.
- Nothing was upgraded or reconfigured in between: the container has been up since
  2026-11-04T09:47:22Z with `RestartCount 0`, and its image was created 2026-10-08T21:58:57Z — so
  the crashes follow no change to that container.
- The container looks healthy throughout *because* gunicorn respawns the worker. "Healthy" is not
  counter-evidence; it is the reason nobody noticed.

What would show it had stopped: no new core file and no new apport line naming that command line
across at least the observed recurrence interval (≥ 3–7 days, i.e. through ~2026-11-15), including
the 13:2x–13:5x window.

## 3. Headroom and store facts

- Root filesystem `df -h /` (2026-11-08T20:28:10Z): `/dev/mapper/ubuntu--vg-ubuntu--lv`,
  **197 G size, 120 G used, 69 G available, 64 % used**. `df -B1 /`: 210,779,168,768 total /
  127,857,930,240 used / 73,219,416,064 available. Headroom is not tight; the store is ~8 % of the
  volume (16.07 GB of 197 GB) but ~23 % of what is currently free (16.07 GB of 69 GB).
- Kernel core handler: `sysctl` is **not installed** (`bash: sysctl: command not found`, exit 127,
  20:28:10Z), so the value was read from `/proc/sys/kernel/core_pattern`:
  `|/usr/share/apport/apport -p%p -s%s -c%c -d%d -P%P -u%u -g%g -F%F -- %E` — every core on this
  host is piped to apport, which is why the store is `/var/lib/apport/coredump`.
- Core size limit readable in this shell: `ulimit -c` → **0** (`ulimit -a`: `core file size
  (blocks, -c) 0`). That is *this* session's shell, not the crashing process; the crashed process
  had `core limit 18446744073709551615` (unlimited) per apport's own log line, which is why
  2–5.8 GB cores exist at all.
- apport: `systemctl is-active apport` → **active**; `is-enabled` → **enabled**;
  `systemctl status apport` → `active (exited) since Tue 2026-10-13 22:00:55 UTC; 3 weeks 4 days
  ago`, `Main PID: 1045 (code=exited, status=0/SUCCESS)`, plus systemd's own note that "journal has
  been rotated since unit was started, output may be incomplete". `apport-autoreport` →
  **inactive**. `systemd-coredump.socket` → **inactive**.
- Ownership/modes of what I could stat: cores `-r-------- root:root` (0400), report
  `-rw-r----- root:root` (0640), `/var/crash` `drwxrwsrwt root:root` (sticky, setgid, world
  writable — apport's normal drop box), `/var/lib/apport` `drwxr-xr-x root:root`.

## 4. What could NOT be measured, and why (unmeasured — not clean)

| not measured | why |
|---|---|
| contents of any core file | `head -c 16 <13:50 core>` at 2026-11-08T20:28:48Z → `head: cannot open '…' for reading: Permission denied` (exit 1). Files are 0400 root:root; I am uid 1000. Also out of scope by instruction. |
| contents of `/var/crash/_usr_bin_python3.12.0.crash` | `wc -c …` → `wc: /var/crash/_usr_bin_python3.12.0.crash: Permission denied` (2026-11-08T20:28:48Z). So apport's own stack-trace analysis is unread. |
| the 11-06 / 11-07 apport log lines | current `/var/log/apport.log` was **born 2026-11-08 00:00:01** (daily rotation). Rotated files were outside my allowed read set and were not opened. |
| `coredumpctl` view of the crashes | binary **absent** (`command -v coredumpctl` → nothing, exit 1); `systemd-coredump.socket` inactive. There is no coredumpctl record to consult. |
| journald coverage of the crash | not read at all; `systemctl status apport` itself warns its journal was rotated since the unit started. Journal coverage of 11-06/11-07 is therefore unknown, not "absent". |
| in-container core limit / compose `ulimits` | would need `docker inspect` fields outside the allowed named-field set, or `docker exec` (forbidden). |
| the crashed process's own argv beyond apport's log | reading live process argv is out of scope; the processes are gone anyway. |
| whether the crashing host pid actually sat in that container's cgroup | would need `/proc/<pid>/cgroup` (process gone) or a core read. |
| GPU vendor/model and the ROCm stack version inside the image | no allowed command exposes it; only the image tag `-rocm` is measured. |
| whether apport's rate limit ("already crashed 2 times") is suppressing further *reports* | the apport state file is not in my read set. Cores keep being written, so suppression of reports would not hide recurrence. |
| sudo-based inspection | `sudo -n true` → `sudo: a password is required` (exit 1). No passwordless sudo. |

## 5. Remedy

### 5.1 Versions, measured

- **Installed:** `ghcr.io/immich-app/immich-machine-learning:v3.3.1-rocm`, digest
  `sha256:0936fa0414903164315c9c80a9f2c2a6ed9b8387ce05decbfe606d1d54e7a2ca`, image created
  **2026-10-08T21:58:57Z**, container started 2026-11-04T09:47:22Z. (The sibling server container is
  `immich-server:v3.3.1`.)
- **Latest official stable release, checked 2026-11-08T20:29Z** via
  `https://api.github.com/repos/immich-app/immich/releases/latest`:
  **v3.3.6**, published **2026-11-04T17:24:55Z** —
  <https://github.com/immich-app/immich/releases/tag/v3.3.6>. Its only change is
  "fix(ml): disable onnxruntime telemetry" (PR #32956). So the installed line is **four releases
  behind stable** (v3.3.3, v3.3.4, v3.3.5, v3.3.6), and none of them names a crash fix.
- **Newer, pre-release:** v3.4.0-rc.3 published 2026-11-06T20:44:16Z
  (<https://github.com/immich-app/immich/releases/tag/v3.4.0-rc.3>) — not a stable target.
  Its notes say: "Set `MACHINE_LEARNING_MODEL_REVISION=v2` to take full advantage of the memory
  usage and performance improvements in this release", and v3.4.0-rc.1 (2026-10-30T17:53:15Z,
  <https://github.com/immich-app/immich/releases/tag/v3.4.0-rc.1>) carries
  "fix(ml): preloaded model loaded twice" (PR #32610).

### 5.2 Is this crash known upstream, and is it fixed?

What I could establish (release notes for v3.3.1, v3.3.3, v3.3.4, v3.3.5, v3.3.6 and
v3.4.0-rc.1..rc.3 were read):

- **No release note between the installed v3.3.1 and the latest stable v3.3.6 names any ML or
  python worker crash.** v3.3.3 = two non-ML fixes; v3.3.4 = a translation fix + a server query
  optimisation; v3.3.5 = dependency/security pins only; v3.3.6 = onnxruntime telemetry.
- A **matching symptom family does exist**: issue #27387 "ROCm ML image crashes (SIGSEGV) on AMD
  Phoenix iGPU (gfx1103) with MIGraphXExecutionProvider" (immich v2.6.3, ROCm 7.2.0, ONNX Runtime
  1.24.2 — SIGSEGV during MIGraphX model compile, container crash-looping, CPU image as the
  workaround) — <https://github.com/immich-app/immich/issues/27387>. It is **closed**, fixed by
  the PR that serialises MIGraphX sessions (<https://github.com/immich-app/immich/pull/28444>).
  Related: discussion #26953 "WARNING Worker (pid:35) was sent SIGSEGV!"
  (<https://github.com/immich-app/immich/discussions/26953>).
- **That fix is older than the installed version**: PR #28444 predates the v3.3.x PR range (~#32xxx
  in the v3.3.0 notes), so v3.3.1 should already contain it. Therefore "upgrade to get the known
  ROCm fix" is **not** an established remedy here.
- ⚠ And the signal differs: these events are **SIGABRT (6)**, not SIGSEGV — consistent with a
  native library calling `abort()` (ROCm/MIGraphX/ORT are the usual suspects inside that image),
  but that is a hypothesis, not a measurement. I found **no upstream issue that matches
  "immich_ml gunicorn worker dies with SIGABRT on v3.3.1-rocm"**, and I could not read the core or
  the .crash report to identify the aborting frame. Treat the upstream state as **not established**
  in both directions: not shown to be a known open bug, not shown to be fixed.

### 5.3 Options (all PROPOSAL — nothing applied)

| # | option | status | bounded cost | what it does / does not do |
|---|---|---|---|---|
| R1 | Owner deletes the accumulated artifacts: 5 files in `/var/lib/apport/coredump` + `/var/crash/_usr_bin_python3.12.0.crash` | **PROPOSAL** | one `rm` as root; frees **16.76 GB** (16,068,284,416 + 694,706,176 B) | reclaims ~23 % of current free space. Destroys the only evidence: if any diagnosis is wanted, capture the apport report's metadata (or set one small core aside) **first**. Does not stop the crashes. |
| R2 | Cap core dumps for that one service only — `ulimits: {core: 0}` (or a small value) on the `immich_machine_learning` compose service, then recreate *that* container | **PROPOSAL** | one service recreate; the crash behaviour is unchanged | stops the growth at the source — measured events wrote 0 B, 2.15 GB, 2.65 GB, 5.48 GB and 5.79 GB; gunicorn still respawns the worker, so the service keeps working exactly as now (it already survives these crashes). Also removes the artifact that recurrence is measured from — replace it with a counter (R5). Requires editing the owner's compose file, which is outside this task. |
| R3 | Narrow the trigger: run the same ML image with the CPU provider (or drop the MIGraphX/ROCm provider) for a few days | **PROPOSAL** | owner must edit env and recreate one container; slower ML | a controlled experiment that would separate "ROCm/MIGraphX path" from "any ML worker". Not a fix; it is the cheapest way to make the cause falsifiable. |
| R4 | Upgrade — but **only if a release that names this crash exists** | **PROPOSAL, conditional** | v3.3.1 → v3.3.6 is a four-release jump (server + ML together); v3.4.x is pre-release | per 5.2 there is **no such release note** today, so R4 is "upgrade only if a fixed release exists", not "upgrade because v3.3.6 is newer". The v3.4.0-rc ML memory notes are a plausible mitigation, not a fix claim. |
| R5 | Record the crash count per day (one line/day from `ls /var/lib/apport/coredump`, or apport log matches) so the next measurement is a comparison, not a rediscovery | **PROPOSAL** | read-only, no service effect | keeps recurrence observable after R2 removes the cores. This document is the baseline: 5 artifacts / 3 dates as of 2026-11-08T20:31Z. |
| R6 | Do nothing to the service; escalate to the owner with this document | **PROPOSAL** | zero | defensible: the service is healthy, the host has 69 GB free, and the failure is the owner's to triage. The only real cost is up to ~5.8 GB per event plus unread cores. |

What would falsify "fixed" (any of these, not the absence of one): for **≥ 3–7 consecutive days**
(through at least 2026-11-15) no new file in `/var/lib/apport/coredump` and no new apport line
naming the `immich_ml` command line, **including** the 13:2x–13:5x window, with the ML container's
uptime unbroken. Container `health` and `RestartCount` **cannot** falsify anything here — gunicorn
respawn keeps both clean through every crash (measured: `RestartCount 0` across at least four
crashes). Conversely, an upstream release note or commit that names this failure, plus one
post-upgrade week of quiet, is what would establish "fixed" positively.

## 6. What AItelier is not responsible for

AItelier's backend runs `/usr/local/bin/python` (uvicorn) **inside its own container**
`aitelier`, image `aitelier:latest`, digest `sha256:e3291d5e…f76849`, and that container's
`StartedAt` is 2026-11-08T16:42:30Z — later than both crashes recorded on 2026-11-08
(13:49:39 and 13:52:19). The crash command line is a gunicorn master for `immich_ml.main:app` from
`/usr/src/immich_ml/`, an image AItelier neither builds nor contains. No AItelier process appears in
any artifact measured here, and this diagnostic session itself runs with `ulimit -c 0`, so it
produces no cores of its own. The coredump stream on this host belongs to Immich's machine-learning
service.

## Appendix — commands actually run (read-only)

```
date -u                                   # 2026-11-08T20:27:50Z … 20:31:05Z
wc -l /var/log/apport.log ; tail -n 60 /var/log/apport.log ; tail -n 3 /var/log/apport.log
stat /var/log/apport.log
ls -l /var/crash ; ls -l /var/lib/apport/coredump ; ls -ld /var/crash /var/lib/apport /var/lib/apport/coredump
du -s -B1 /var/crash /var/lib/apport/coredump /var/lib/apport
stat /var/crash/_usr_bin_python3.12.0.crash ; stat /var/lib/apport/coredump/<each core>
head -c 16 <13:50 core> >/dev/null          # permission-denied probe, recorded above
wc -c /var/crash/_usr_bin_python3.12.0.crash
df -h / ; df -B1 /
sysctl kernel.core_pattern                 # not installed → exit 127
read /proc/sys/kernel/core_pattern
ulimit -c ; ulimit -a
systemctl is-active apport ; systemctl is-enabled apport ; systemctl status apport --no-pager -l
systemctl is-active apport-autoreport ; systemctl is-active systemd-coredump.socket
command -v coredumpctl ; id ; sudo -n true
docker ps ; docker ps -a --filter name=immich
docker inspect --format '<the six named fields>' immich_machine_learning immich_server aitelier
docker image inspect --format 'RepoTags/RepoDigests/Id/Created' <ml image> aitelier:latest
```

Web (2026-11-08T20:29–20:31Z): GitHub API `releases/latest`, `releases?per_page=…`,
release pages v3.3.4/v3.3.5, issues #27387, PR #28444, discussion #26953.
