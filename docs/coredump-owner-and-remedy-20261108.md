# Who owns the python3.12 coredumps on this host, and what may be done about them

Measurement window: **2026-11-08T20:27:50Z – 2026-11-08T20:31:05Z** (host clock, UTC).
Operator identity for every command below: `uid=1000(linxuhao)`, sudo group member but **no
passwordless sudo** (`sudo -n true` → `sudo: a password is required`, exit 1).
State issue pointer (not amended by this document): `iss-4f3485436afc4e8e`.

Everything under "Remedy" is **PROPOSAL**. No option was applied, no container was touched, no
core or crash file was deleted, moved or re-permissioned. Effects on a service this operator does
not own are the owner's decision.

Repaired **2026-11-08T21:05Z–21:12Z** against the rotated apport logs, the kernel journal, the
`immich_machine_learning` container log and the upstream tracker. The measurement window above and
every measured figure that is still true are unchanged by the repair; what changed, marked ⚠ below:

1. the recorded signal on **every** one of the 46 retained serving-worker apport events — both 2026-11-08 events
   included — is **6 (SIGABRT)**, not SIGSEGV;
2. the 13:50 core on disk is **578,191,360 B smaller** than the size apport logged for it;
3. ⚠ **CORRECTED:** the 11-06/11-07 events *do* have log lines. `/var/log/apport.log*` rotates daily,
   but the rotated files are `0640 root:adm` and readable by uid 1000 through group `adm`. All eight
   retained files carry a **census of 46 serving-worker `immich_ml` events across 8 dates** — 52
   `immich_ml` SIGABRTs in total (§1.3) — (2026-10-31 01:23:34 → 2026-11-08 13:52:19), so attribution to the ML container is **direct for every event**, not
   pattern inference (§1.3);
4. ⚠ the five-file core store is apport's **cap**, not a crash count (§2), and the 0-byte core has a
   recorded cause — apport itself crashed while writing it (§2);
5. ⚠ the crash is a **measured** correlation with a kernel GPU reset plus a matching open upstream
   issue, #32046, not a hypothesis (§5.2). The remedy is re-ranked accordingly (§5.3).

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
| signal | **6** (SIGABRT) on all 46 recorded events ⚠ — not SIGSEGV |
| pids | 3844822 (13:49:39) and 4063523 (13:52:19) in the live log; 44 more in the rotated logs (§1.3) |
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

### 1.3 (continued) The 46-event census — attribution is direct for every event

The 11-06/11-07 events are **not** weaker evidence. The earlier version of this section said "no log
line survives" for them and called their attribution `pattern inference`; both statements were
wrong. `/var/log/apport.log` rotates daily, but the rotated files are mode `0640 root:adm`, and this
operator is in group `adm`, so all eight retained files are readable. Reading each one (`zcat` for
the `.gz`, plain read for `.1` and the live file) and matching apport's
`INFO: apport (pid …) <time>: called for global pid …, signal …` header against the run of lines
that follows it (re-measured 2026-11-08T21:06Z):

| log file | events | first | last |
|---|---|---|---|
| `/var/log/apport.log.7.gz` | 17 | 2026-10-31 01:23:34 | 2026-10-31 08:10:39 |
| `/var/log/apport.log.6.gz` | 9 | 2026-11-01 02:56:52 | 2026-11-01 05:01:10 |
| `/var/log/apport.log.5.gz` | 4 | 2026-11-02 07:45:25 | 2026-11-02 23:42:36 |
| `/var/log/apport.log.4.gz` | 4 | 2026-11-04 02:20:46 | 2026-11-04 03:27:57 |
| `/var/log/apport.log.3.gz` | 3 | 2026-11-05 15:38:05 | 2026-11-05 15:42:44 |
| `/var/log/apport.log.2.gz` | 5 | 2026-11-06 12:54:10 | 2026-11-06 20:58:25 |
| `/var/log/apport.log.1` | 2 | 2026-11-07 13:26:18 | 2026-11-07 13:33:40 |
| `/var/log/apport.log` (live) | 2 | 2026-11-08 13:49:39 | 2026-11-08 13:52:19 |
| **total** | **46** | **2026-10-31 01:23:34** | **2026-11-08 13:52:19** |

Per date: 2026-10-31 ×17, 11-01 ×9, 11-02 ×4, 11-04 ×4, 11-05 ×3, 11-06 ×5, 11-07 ×2, 11-08 ×2.
**Every one of the 46 is signal 6**, and every one names the same command line
`python -m gunicorn immich_ml.main:app -k immich_ml.config.CustomUvicornWorker -c
/usr/src/immich_ml/gunicorn_conf.py -b [::]:3003 -w 1 …`.

The census is **scoped to that command line**. The same eight files hold **six further `immich_ml`
SIGABRTs** from the image's one-shot entrypoint `python -m immich_ml.sessions.prepare` (11-01 ×4 at
03:05–03:59, 11-02 ×2 at 13:08 and 23:40) — same `/usr/bin/python3.12`, same signal 6, same core
store — so the image's total is **52 `immich_ml` SIGABRTs across the same eight dates**. Only the 46
above serve models on port 3003; the six change the recurrence count, not the owner. A re-run that
drops this filter reproduces 52, not 46.

What that settles, and what it does not:

- Attribution to `immich_machine_learning` is **direct for all 46 events**, not inference for three
  of them: same executable, same ML-image config paths under `/usr/src/immich_ml/`, same port 3003,
  same signal. The container identity in §1.2 is unchanged.
- 2026-11-06 had **five** crashes (12:54, 12:56, 12:58, 13:12, 20:58), not one; 2026-11-07 had two;
  2026-11-08 had two.
- The **first observed** crash is 2026-10-31 01:23:34, not 2026-11-06 20:59:56. Retention *bounds*
  that: the oldest retained apport log is `.7.gz` and the journal's first line is
  `2026-10-30T06:13:52+00:00` (`journalctl -o short-iso | head -1`). "First on 2026-10-31" is
  therefore a statement about **log retention**, not proof that it was the first occurrence.
- **Attribution strength: direct for every recorded event.** Still not the strongest conceivable
  evidence — that would be the core's `NT_PRPSINFO` note or the `.crash` report's own stack analysis,
  both unread (§4) — but it is no longer inferential.
- This repair *did* read the container's own log (`docker logs`, read-only, §5.2). It did not read
  any core, did not read live process argv, and did not `docker exec`.

What would still make it stronger (each is an owner decision I did not take):

1. `docker inspect` `HostConfig.Ulimits` / `Config.Cmd` to show the in-container core limit;
2. the definitive one: the core's `NT_PRPSINFO` note records the crashed process's argv and the
   `.crash` report records its stack — both need root and mean reading a core/report file, which
   this task forbids.

## 2. Recurrence

### 2.1 What the five surviving files are — and why they are not the crash count

⚠ **The store holds at most five cores by design.** `max_corefiles_per_uid = 5` at
`/usr/lib/python3/dist-packages/apport/fileutils.py:36`, and
`apport.fileutils.clean_core_directory(report_owner.uid)` is called at `/usr/share/apport/apport:304`
— immediately before the new file is created by `os.open(… O_CREAT | O_EXCL, mode=0o400)` at
`apport:305-307` — i.e. **before every core write**. `clean_core_directory`
(`fileutils.py:655-664`) rotates the oldest file out as soon as the directory already holds
`max_corefiles_per_uid - 1`. The store therefore only
ever holds the five most recent writes and never the crash count: with the 46 events of §1.3, at most
five cores can exist at any moment, and these five are simply the last five writes. There is also a
`systemd-tmpfiles` age rule (`/usr/lib/tmpfiles.d/apport.conf:2`,
`d /var/lib/apport/coredump 0755 root root 3d`), but the five-file cap fires first and is the binding
limit here. The recurrence basis is the **census (46 events / 8 dates)**, not this table.

Artifacts by date (stat/`ls -l` only; re-measured 2026-11-08T21:08Z; store
`/var/lib/apport/coredump`, mode `0400 root:root`, dir mode `drwxr-xr-x`; report dir `/var/crash`,
mode `drwxrwsrwt`, file `0640 root:root`):

| # | date (UTC) | artifact | bytes | its own apport log line |
|---|---|---|---|---|
| 1 | 2026-11-06 20:59:56 | `core._usr_bin_python3_12.0.…3229610.204196992` | **0** ⚠ | yes — pid 3229610, signal 6, 20:58:25; apport then crashed writing it (§2.2) |
| 2 | 2026-11-07 13:26:18–13:26:24 | `core.…2158310.206996586` | 2,648,535,040 | yes — pid 2158310, signal 6, 13:26:18, same command line |
| 3 | 2026-11-07 13:33:40–13:34:09 | `core.…3831101.212945710` | 5,481,521,152 | yes — pid 3831101, signal 6, 13:33:40, 7m22s after #2 |
| 4 | 2026-11-08 13:49:39 | `core.…3844822.212961837` | **2,147,479,552** on disk ⚠ (apport logged 2,725,670,912) | yes — pid 3844822, signal 6, command line above |
| 5 | 2026-11-08 13:50:09 | `/var/crash/_usr_bin_python3.12.0.crash` | 694,695,161 | apport's report for #4 (same pid 3844822) |
| 6 | 2026-11-08 13:52:19–13:52:34 | `core.…4063523.221699316` | 5,790,728,192 | yes — pid 4063523, signal 6; apport refused a *second report* ("this executable already crashed 2 times, ignoring") but still wrote the core |

Every one of the five filenames' `<pid>.<serial>` field matches the `called for global pid` line of a
retained apport event, so no file in the store has to be matched by mtime or by pattern.

Store sizes at 2026-11-08T20:28:10Z, unchanged at 20:28:54Z, 20:31:05Z and 21:08Z:

- `/var/lib/apport/coredump` — **16,068,284,416 B** (14.96 GiB) in 5 files; `du` agrees with the
  per-file allocations (each non-empty file is its size plus one 4,096 B block; e.g. the 13:50 file
  holds 4,194,304 allocated blocks of 512 B = 2,147,483,648 B for a 2,147,479,552 B file, and the
  0-byte file has 0 blocks).
- `/var/crash` — 694,706,176 B (1 file, 694,695,161 B).
- `/var/lib/apport` — 16,068,288,512 B in total: the store above plus the directory's own 4,096 B.
  (`du` counts each inode once, so asking it for the store *and* its parent in one invocation reports
  only `4096` for the parent.)


### 2.2 The 0-byte core of 2026-11-06 — apport crashed while writing it

It is not "a write that produced nothing" and it is no longer open: the rotated log carries the
traceback (`zcat /var/log/apport.log.2.gz`):

```
INFO: apport (pid 2133446) 2026-11-06 20:59:56,024: wrote report /var/crash/_usr_bin_python3.12.0.crash
INFO: apport (pid 2133446) 2026-11-06 20:59:56,288: writing core dump to /var/lib/apport/coredump/core._usr_bin_python3_12.0.f56f3fd2-…3229610.204196992 (limit: -1)
ERROR: apport (pid 2133446) 2026-11-06 20:59:56,303: Unhandled exception:
Traceback (most recent call last):
  …
  File "/usr/share/apport/apport", line 319, in write_user_coredump
    core_size = len(r["CoreDump"])
                ~^^^^^^^^^^^^
KeyError: 'CoreDump'
```

So: apport wrote the report first, then re-loaded it for the core write, and the **re-loaded report
had no `CoreDump` key** — `len(r["CoreDump"])` at `/usr/share/apport/apport:319` raised, and the
`O_CREAT | O_EXCL`-created file stayed behind at 0 bytes. The kernel-side dump itself still happened;
this is the apport-side write that aborted, not a crash that produced nothing.

### 2.3 The short core of 2026-11-08 13:50 — shape measured, reason NOT established

The discrepancy the first version of this document recorded as unexplained is now measured precisely
(`stat`, re-measured 2026-11-08T21:08Z):
`size=2147479552 blocks=4194304 blksize=512` → `blocks × 512 = 2,147,483,648 B`, i.e. the file is
**fully allocated and NOT sparse**. Its length is 2 GiB − 4,096 B, one page short of 2 GiB, while
apport logged `of size 2725670912` (2.54 GiB) for the same write at 13:50:18 with
`mtime 2026-11-08 13:50:18.596011334`: the write stopped **578,191,360 B short of what apport
intended**. That is a **truncated write**, not a sparse or partial dump.

Ruled out, each by measurement:

- **not apport's own size limit.** `core_size_limit = usable_ram() * 3 / 4`
  (`/usr/share/apport/apport:1049`), and `usable_ram()` is `(MemFree + Cached - Writeback) * 1024`
  (`apport:357-370`) — a *moving* figure, ≈ **4.05 GiB** on this host at 2026-11-08T21:08Z, and
  17.12 GiB even if it were computed from `MemTotal` (23,930,388 kB × 3/4). The write for this file
  is logged with **`(limit: -1)`**, i.e. unlimited, and the `0 < limit` check at `apport:320` cannot
  fire on that. 2.54 GiB is below the limit either way.
- **not a cap leaving a short file.** When a limit *is* hit, apport closes and **unlinks** the file
  (`/usr/share/apport/apport:320-325` in the report path, `:340-348` in the streamed path) instead of
  leaving a truncated one.
- **not tmpfiles.** That rule removes whole files by age (§2.1); it never truncates.

What the source permits: in the report-file path apport writes with a **single unchecked `os.write`**
— `/usr/share/apport/apport:328`, `os.write(core_file, r["CoreDump"])` — and discards the return
value, so a short write leaves exactly this shape (the streamed path at `:344-348` *does* compare the
return value, so the unchecked call is the one that ran here).

**NOT established:** *why* the write stopped one page short of 2 GiB. The shape is measured; the reason
is not, and this document does not substitute a guess for it.

(The 11-07 sizes match the previously recorded figures exactly: 2,648,535,040 and 5,481,521,152.)


### 2.4 Old transient, or ongoing failure?

**Ongoing.** The basis is the census, not the store:

- **46 serving-worker events across 8 dates** — **52 `immich_ml` SIGABRTs** once the six one-shot
  `sessions.prepare` events are counted (§1.3) — 2026-10-31 01:23:34 → 2026-11-08 13:52:19, an 8.5-day span
  whose only event-free calendar day, **2026-11-03**, is also the one day with no retained apport log
  to check (§4).
- It is not a clock slot. The earlier "pair minutes apart in the 13:2x–13:5x window" reading came from
  the two days the live log could show; with the rotated logs the same service also crashed 17 times
  on 10-31 between 01:23 and 08:10, and on 11-02 between 07:45 and 23:42. What recurs is the
  trigger, not the hour. (The longest observed gap between events is **1 d 12 h 10 m**, at
  2026-11-04 03:27:57 → 2026-11-05 15:38:05; 7 of the 45 gaps exceed 12 h.)
- The most recent event is **13:52:19Z**, 7 h 16 m before the repair measurement (21:08:36Z) — shorter
  than the longest observed gap, so that quiet stretch is not evidence of anything.
- Nothing was upgraded or reconfigured in between: the current container has been up since
  2026-11-04T09:47:22Z with `RestartCount 0`, image created 2026-10-08T21:58:57Z. Note what the census
  adds: crashing begins 2026-10-31, i.e. **before** the container that exists now was started, and
  continues after it — the failure survives a container start.
- The container looks healthy throughout *because* gunicorn respawns the worker. "Healthy" is not
  counter-evidence; it is the reason nobody noticed.
- The recurrence also has a kernel-side and a container-side half, both measured: every 11-06…11-08
  event has a `GPU reset begin!` at the same second, and the container log names the GPU Hang (§5.2).

What would show it had stopped: no new apport line naming that command line, and no new core write,
for longer than the whole observed span — **≥ 8.5 days of quiet, i.e. through ~2026-11-17** — with the
container's uptime unbroken. A shorter quiet stretch proves nothing when a 36-hour gap already exists
inside the failing period.


## 3. Headroom and store facts

- Root filesystem `df -h /` (2026-11-08T20:28:10Z): `/dev/mapper/ubuntu--vg-ubuntu--lv`,
  **197 G size, 120 G used, 69 G available, 64 % used**. `df -B1 /`: 210,779,168,768 total /
  73,219,416,064 available (127,857,930,240 used). Re-measured at 2026-11-08T21:08:36Z, unchanged in
  substance: 127,859,322,880 used / 73,218,023,424 available, still 69 G / 64 %. Headroom is not
  tight; the store is ~8 % of the volume (16.07 GB of 197 GB) but ~23 % of what is currently free
  (16.07 GB of 69 GB).
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
| anything about **2026-11-03** | the retained apport logs step straight from `.5.gz` (2026-11-02) to `.4.gz` (2026-11-04): no retained file carries an 11-03 timestamp, so any event that day is unrecoverable. |
| the **true first** crash, or any event before 2026-10-31 | bounded by retention, not known: the oldest retained apport log is `.7.gz` and the journal's first line is `2026-10-30T06:13:52+00:00` (`journalctl -o short-iso \| head -1`). |
| `coredumpctl` view of the crashes | binary **absent** (`command -v coredumpctl` → nothing, exit 1); `systemd-coredump.socket` inactive. There is no coredumpctl record to consult. |
| journald coverage of the crash | now read: the kernel journal is readable and was read for the repair (§5.2 gives the reset correlation). What is *not* available there is anything the file log does not already carry — the crashed process's argv and registers. |
| in-container core limit / compose `ulimits` | would need `docker inspect` fields outside the allowed named-field set, or `docker exec` (forbidden). |
| the crashed process's own argv beyond apport's log | reading live process argv is out of scope; the processes are gone anyway. |
| whether the crashing host pid actually sat in that container's cgroup | would need `/proc/<pid>/cgroup` (process gone) or a core read. |
| GPU vendor/model and the ROCm / MIGraphX / onnxruntime **versions inside the image** | the only measured identity is the tag `-rocm` plus the digest (§1.2). The bounded probe available for this (`docker run --rm --network none --entrypoint python3 <ml image> -c "import onnxruntime; print(onnxruntime.__version__)"`) was **not run** — this repair may not `docker run` — so versions stay **unmeasured**, not inferred. What *is* measured is a file name from the container log, `migraphx_execution_provider.cc` (§5.2); a version string is not. |
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

### 5.2 The crash is matched upstream (#32046) and the mechanism is measured locally

⚠ **CORRECTED.** The first version of this section said "I found **no** upstream issue that matches …
Treat the upstream state as **not established** in both directions", and called the native-abort
explanation "a hypothesis, not a measurement". Both were wrong, and R3/R4 were scoped against them.
The mechanism is measured on this host, and the upstream tracker carries an open issue matching it
line for line.

**Local half — the kernel resets the GPU at every crash time.** Re-measured 2026-11-08T21:06Z with
`journalctl -k --since '2026-11-06 00:00' --until '2026-11-09 00:00' | grep -E 'GPU reset begin|GPU reset\(|MODE2 reset'`:

| apport event (signal 6) | kernel, same second |
|---|---|
| 2026-11-06 12:54:10 | `GPU reset begin!` 12:54:09 → `MODE2 reset` → `GPU reset(130) succeeded!` |
| 2026-11-06 12:56:01 | `GPU reset begin!` → `MODE2 reset` → `GPU reset(131) succeeded!` |
| 2026-11-06 12:58:09 | `GPU reset begin!` 12:58:08 → `MODE2 reset` → `GPU reset(132) succeeded!` |
| 2026-11-06 13:12:57 | `GPU reset begin!` → `MODE2 reset` → `GPU reset(133) succeeded!` |
| 2026-11-06 20:58:25 | `GPU reset begin!` → `MODE2 reset` → `GPU reset(134) succeeded!` |
| 2026-11-07 13:26:18 | `GPU reset begin!` → `MODE2 reset` → `GPU reset(135) succeeded!` |
| 2026-11-07 13:33:40 | `GPU reset begin!` → `MODE2 reset` → `GPU reset(136) succeeded!` |
| 2026-11-08 13:49:39 | `GPU reset begin!` → `MODE2 reset` → `GPU reset(137) succeeded!` |
| 2026-11-08 13:52:19 | `GPU reset begin!` → `MODE2 reset` → `GPU reset(138) succeeded!` |

Nine events, nine resets, one-to-one. Over the whole retained journal
(`journalctl -k --since '2026-10-01'`) there are **50** `GPU reset(` successes and **494**
`MES failed to response` lines — the GPU instability both pre-dates and outlives the 46 apport
events, so a reset alone is not the trigger; the container log supplies that half.

**Container half — the worker's own log names the abort.** `docker logs --since
2026-11-05T00:00:00Z immich_machine_learning`, bounded by grep, shows the same three lines per event:

```
[W:onnxruntime:Default, migraphx_execution_provider.cc:1270 compile_program] Model Compile: Begin
HW Exception by GPU node-1 (Agent handle: 0x…) reason :GPU Hang
WARNING Worker (pid:…) was sent SIGABRT!
```

e.g. compile 2026-11-07 13:24:33 → HW Exception → `Worker (pid:71702) was sent SIGABRT!` 13:26:25;
compile 13:31:57 → SIGABRT 13:34:09; compile 2026-11-08 13:48:00 → SIGABRT 13:50:24; compile
13:50:46 → SIGABRT 13:52:35. The worker then respawns and the container stays `healthy` with
`RestartCount 0` — §1.3's argument, now measured from the worker's side instead of excluded from it.

So the chain is: **MIGraphX model compile hits the GPU → the GPU hangs → the kernel resets it → the
worker is SIGABRTed (signal 6) → apport dumps the host-side core.** The aborting *frame* is still
unread (§4), but no link in that chain is a hypothesis any more.

**Upstream: issue #32046, open and matching.**
<https://github.com/immich-app/immich/issues/32046> — *"ROCm / MIGraphX causes reproducible GPU wedge
on AMD Hawk Point / gfx1103 during model compilation"*, created 2026-10-03T10:07:48Z. Fields fetched
from `https://api.github.com/repos/immich-app/immich/issues/32046` at 2026-11-08T21:07Z:
**state `open`**, `state_reason: reopened`, **13 comments**, assignee **`mertalev`**, label
**`🧠machine-learning`**. Its body reports the same sequence — MIGraphX `Model Compile: Begin` →
`HW Exception by GPU node-1 … reason :GPU Hang` → `WARNING Worker (pid:39) was sent SIGABRT!`, with
kernel `MES failed to respond to msg=REMOVE_QUEUE`, `MES might be in unrecoverable state`, `Failed to
quiesce KFD`, `GPU reset begin!. Source: 3`, `MODE2 reset`, `GPU reset(1) succeeded!`, and a note that
the `[drm]` GPU coredump under `/sys/class/drm/card1/device/devcoredump/data` was gone later.

The thread's **tested partial mitigation**, which is not a fix:

- maintainer `mertalev` (2026-10-03) asked the reporter to try `IMMICH_VERSION=v3.3.0-rc.2` together
  with **`MACHINE_LEARNING_MODEL_REVISION=v2`**;
- reporter (2026-10-05): with that pair, Smart Search compiled (`Model Compile: Complete`), "no GPU
  hang and no kernel-side AMDGPU/KFD/MES errors or GPU reset"; the maintainer closed it as resolved;
- the same reporter later reopened it in substance: OCR/face models then died with
  `Worker (pid:39) was sent SIGSEGV!` plus a real wedge/reset and a userspace
  `python[…] segfault … in libamdhip64.so.7.2.70200`; reverted ML to the **CPU image**, where
  `buffalo_l` reports `Setting execution providers to ['CPUExecutionProvider']` and runs normally;
- maintainer asked whether `HSA_OVERRIDE_GFX_VERSION` / `HSA_USE_SVM` are set; reporter: neither;
- independent report (`davidvv`, gfx1150): `v2` fixes the hang (10 OCR runs, clean kernel log), but a
  **first-seen input shape took 237 s versus 3.2 s on CPU** — per-shape MIGraphX recompile — so OCR
  stays on CPU there;
- other users confirm the OCR "dimension errors" are a *different* problem (resolution must be a
  multiple of 32) and that fixing that does not remove the wedge.

What survives from the old conclusion: **no release note between the installed v3.3.1 and the latest
stable v3.3.6 names any ML or python worker crash** (§5.1's notes reading), so an upgrade is still
not, by itself, a remedy. And #27387 — SIGSEGV on gfx1103, closed by the PR that serialises MIGraphX
sessions (<https://github.com/immich-app/immich/pull/28444>) — is an older, different report: the
signal here is SIGABRT, and #32046 is the one that matches.


### 5.3 Options, re-ranked against the measured evidence (all PROPOSAL — nothing applied)

Ranking follows one question: *what can an owner actually act on, given that the crash is a GPU wedge
with an open upstream issue and a tested partial mitigation?* Nothing below was applied, and nothing
below is verified — no option has been tested on this host.

| # | option | status | bounded cost | what it does / does not do |
|---|---|---|---|---|
| R1 | Set **`MACHINE_LEARNING_MODEL_REVISION=v2`** on the existing `immich_machine_learning` service and recreate *that* container | **PROPOSAL** | one env edit + one service recreate; no image change | the only mitigation tested upstream for *this* mechanism (§5.2): with it, Smart Search compiled and the kernel log went clean in the reporter's test. It is **partial** — OCR/face models still crashed there (issue reopened), and a first-seen input shape cost **237 s vs 3.2 s on CPU**. Expect the wedge to stop for visual search, not a fix. Requires editing the owner's compose/env, outside this task. |
| R2 | Run ML on the **CPU image** (or force `CPUExecutionProvider`) instead of `-rocm` | **PROPOSAL** | one image change + recreate that service; ML slower on CPU | this is where the upstream reporter ended up, and it doubles as the controlled experiment the first version of this document wanted: with no GPU in the path the wedge cannot occur, so if crashes continue it falsifies the attribution. Cost is throughput, not correctness; the CPU path is measured to work upstream, not on this host. |
| R3 | Record recurrence from the **apport log**, one `grep -c` per day of `immich_ml.main:app` events in `/var/log/apport.log*` | **PROPOSAL** | read-only; no service effect | the store cannot be the counter — it is capped at five files by apport itself (§2.1). The log census is the measurement, and its baseline is 46 events / 8 dates, last at 2026-11-08 13:52:19 (§1.3). Do this **before** R5 if the owner wants the recurrence to stay visible. |
| R4 | Owner deletes the accumulated artifacts: 5 files in `/var/lib/apport/coredump` + `/var/crash/_usr_bin_python3.12.0.crash` | **PROPOSAL** | one `rm` as root; frees **16.76 GB** (16,068,284,416 + 694,706,176 B) | reclaims ~23 % of current free space (69 G) and is cheap *for this operator* for a reason that is now measured: the cores are `0400 root:root` and **unreadable here** (§4), and the recurrence is recorded in the apport logs, so nothing this document concludes depends on their contents. If an owner who *can* read them wants the aborting frame, capture the `.crash` report's stack first. Does not stop the crashes. |
| R5 | Cap core dumps for that one service — `ulimits: {core: 0}` on the `immich_machine_learning` compose service, then recreate *that* container | **PROPOSAL** | one service recreate; crash behaviour unchanged | stops the growth at the source — the five cores still on disk measured 0 B, 2.15 GB, 2.65 GB, 5.48 GB and 5.79 GB (earlier events' cores were rotated out by the cap, so their sizes are unmeasured). It does remove the store-level signal, but that signal is already bounded by apport's own five-file cap (§2.1) and the recurrence stays visible in the apport log (R3). Requires editing the owner's compose file, outside this task. |
| R6 | Do nothing to the service; escalate to the owner with this document | **PROPOSAL** | zero | defensible on disk and health grounds (69 G free, `healthy`, `RestartCount 0`), but weaker than it looked: recurrence is now a 46-event fact over 8 dates, every event has a kernel GPU reset, and upstream has an open issue with a tested partial mitigation. The cost of waiting is up to ~5.8 GB per event plus one GPU wedge each time. |

**Not ranked as an action: upgrading.** v3.3.1 → v3.3.6 is a four-release jump and no release note in
that range names this failure (§5.2), so a newer tag is not a remedy; v3.4.0-rc.x is pre-release. If
the owner upgrades anyway, carry R1's `MACHINE_LEARNING_MODEL_REVISION=v2` along — the v3.4.0-rc notes
recommend it for ML memory and performance reasons as well. "Upgrade to get the known ROCm fix" stays
**not established**: the serialised-MIGraphX fix (PR #28444) is older than the installed image and did
not prevent these events.

What would falsify "fixed" (any of these, not the absence of one): **no** new apport line naming that
command line and **no** new core write for **≥ 8.5 days — through ~2026-11-17** — with the container's
uptime unbroken. That window is the observed span itself; a 36-hour gap already exists inside the
failing period (§2.4), so anything shorter proves nothing. Container `health` and `RestartCount`
**cannot** falsify anything — gunicorn respawn keeps both clean through every crash (measured:
`RestartCount 0` on the container that has been up through the **12** events recorded since it started
at 2026-11-04T09:47:22Z). A positive test is available and cheap: after R1 or R2, the
kernel log should show **no** `GPU reset begin!` / `MES failed to respond` at crash times and apport
should stop logging that command line. An upstream release note or commit that names this failure,
plus one post-change week of quiet, is what would establish "fixed" positively.


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

### Repair pass — commands actually run (2026-11-08T21:05–21:12Z, read-only)

```
python3 (gzip/pathlib) over /var/log/apport.log.{7..2}.gz, .1 and the live file   # 46-event census
grep -n 'max_corefiles_per_uid' /usr/lib/python3/dist-packages/apport/fileutils.py  # :36, :655-664
sed -n '303,307p'  /usr/share/apport/apport        # clean_core_directory call — this window's SECOND line is :304 (read as :305 here; corrected in the second pass below), O_EXCL at :306-307
sed -n '314,330p'  /usr/share/apport/apport        # :319 len(r["CoreDump"]), :328 unchecked os.write
sed -n '340,350p'  /usr/share/apport/apport        # unlink-on-limit in the streamed path
sed -n '357,370p'  /usr/share/apport/apport        # usable_ram() = (MemFree+Cached-Writeback)*1024
sed -n '1045,1052p' /usr/share/apport/apport       # :1049 core_size_limit = usable_ram()*3/4
zcat /var/log/apport.log.2.gz | sed -n '24,45p'    # the KeyError: 'CoreDump' traceback
stat -c '%n %s %b %B' /var/lib/apport/coredump/* /var/crash/_usr_bin_python3.12.0.crash
du -s -B1 /var/lib/apport/coredump /var/crash /var/lib/apport ; df -h / ; df -B1 /
grep MemTotal /proc/meminfo                        # 23,930,388 kB
journalctl -k --since '2026-11-06 00:00' --until '2026-11-09 00:00' \
  | grep -E 'GPU reset begin|GPU reset\(|MODE2 reset'
journalctl -k --since '2026-10-01' | grep -cF 'GPU reset('          # 50
journalctl -k --since '2026-10-01' | grep -c 'MES failed to response'   # 494
journalctl -o short-iso | head -1                  # journal begins 2026-10-30T06:13:52+00:00
docker logs --since 2026-11-05T00:00:00Z immich_machine_learning \
  | grep -E 'Model Compile: Begin|HW Exception|was sent SIGABRT'
docker inspect --format 'StartedAt/Status/RestartCount/Health/Image' immich_machine_learning
curl -s https://api.github.com/repos/immich-app/immich/issues/32046   # state/state_reason/comments/assignee/labels
```

The repair pass used no `docker run`, no `docker exec`, no container restart or recreate, no
`sudo`, deleted no core or crash file, and changed no compose file, env var or service. The only
network access was the one GitHub API read above. The only file written is this document.

### Second repair pass (director, 2026-11-08T21:2xZ, read-only)

Two figures in this document came from the director's own facts file and do not reproduce. Both are
corrected above; the correction is recorded here rather than silently overwritten, because a number a
reader cannot re-derive is the defect this document exists to avoid.

1. **The census filter was unstated.** The 46-event table is right per row and per window, but the
   method paragraph did not say it keeps only the events whose logged cmdline is the serving worker.
   Re-deriving every `immich_ml` SIGABRT from the same eight files gives **52**: the extra six are
   `python -m immich_ml.sessions.prepare` (11-01 ×4 at 03:05–03:59, 11-02 ×2 at 13:08 and 23:40), same
   executable, same signal 6, same core store. Re-run: the block parse in §1.3 with and without the
   `immich_ml.main:app` filter.
2. **The call site is `:304`, not `:305`.** `clean_core_directory(report_owner.uid)` is the second line
   of a `sed -n '303,307p'` window and was counted as the first; `:305` is the `core_file = os.open(`
   that follows it. Re-run: `grep -n clean_core_directory /usr/share/apport/apport` → `304:`.

Both numbers appear in the same shape in the director's `REPAIR-FACTS.md`; the correction there is the
one that prevents the next copy of this document from repeating them.
