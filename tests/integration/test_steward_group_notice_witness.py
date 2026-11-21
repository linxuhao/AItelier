"""Step 25.2 C11's one-off witness (design-steward 7.9.4 tests, last item): a reminder to the
family group, end to end over a real socket - the real backend (write gate ON, a bound group),
the host's real reminder clock (integrations/dsh-driver/schedules.mjs, run by node with the
steward's own token) and a stub bridge (the `sendGroup` the plugin hands the clock, recording
what it is given and answering as the bridge would):

1. due: the group gets ONE message, the owner's exact words, and the steward is not woken;
   the backend holds one outbound 'notice' row and wrote no State event for it;
2. a restart (a new clock over the same backend) sends nothing again;
3. the stub bridge refuses the send: nothing reaches the group, the steward gets exactly one
   owner wake saying why, and a restart neither resends nor wakes it again."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import state_channels, state_steward
from core.state_commands import execute
from tests.integration.test_steward_group_witness import http, served  # noqa: F401
from tests.unit.test_steward_channels import world  # noqa: F401
from tests.unit.test_steward_groups import ROOM, bound_group

ROOT = Path(__file__).resolve().parents[2]
SCHEDULES = ROOT / "integrations" / "dsh-driver" / "schedules.mjs"
CLOCK = """
const { ScheduleTimer } = await import(process.env.SCHEDULES);
const base = process.env.AITELIER_URL, token = process.env.TOKEN, mode = process.env.MODE, now = Date.parse(process.env.NOW);
const request = async (path, body) => {
  const r = await fetch(new URL(path, base), { method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', 'X-AItelier-Driver-Token': token },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text()}`);
  return r.json();
};
const out = { runs: [] };
for (const run of [1, 2]) {  // 2: a restart, a new clock over the same backend
  const sent = [], woken = [], logged = [];
  const timer = new ScheduleTimer({ request, now: () => now, log: line => logged.push(line),
    deliver: async (text, id) => { woken.push({ id, text }); },
    // The stub bridge: records the line and answers as the bridge would (taken, or refused).
    sendGroup: async (channel, groupRef, text) => { sent.push({ channel, groupRef, text }); return mode === 'ok'; },
    setTimer: () => ({}), clearTimer: () => {} });
  await timer.refresh();
  timer.close();
  out.runs.push({ run, sent, woken, logged });
}
console.log(JSON.stringify(out));
"""


class Clock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


def _run_clock(url, token, mode, now):
    node = shutil.which("node")
    assert node, "the witness needs node (the shipped image has it)"
    directory = Path(tempfile.mkdtemp(prefix="s252-notice-"))
    try:
        script = directory / "clock.mjs"
        script.write_text(CLOCK, encoding="utf-8")
        env = {**os.environ, "SCHEDULES": SCHEDULES.as_uri(), "AITELIER_URL": url, "TOKEN": token, "MODE": mode,
               "NOW": now.isoformat()}
        done = subprocess.run([node, str(script)], env=env, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.parametrize("mode", ["ok", "bridge_refuses"])
def test_a_group_reminder_goes_out_once_without_a_turn_and_a_failed_send_wakes_the_steward_once(
        world, http, served, monkeypatch, mode):
    url, _recorder = served
    clock = Clock(datetime(2027, 1, 1, 8, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(state_steward, "_now", clock)
    monkeypatch.setattr(state_channels, "_now", clock)
    bound_group(world)
    made = execute(world.steward, "create_steward_schedule",
                   {"request_key": "w-c1", "title": "吃药", "instruction": "该吃药了，别忘了带水杯",
                    "schedule": {"kind": "once", "at": "2027-01-01T08:10:00+00:00"}, "target": "family_group"},
                   allow_write=True)
    assert made["target"] == "family_group"
    clock.moment = datetime(2027, 1, 1, 8, 10, 5, tzinfo=timezone.utc)
    with world.db.get_connection() as conn:
        events = conn.execute("SELECT COUNT(*) FROM state_events").fetchone()[0]
    out = _run_clock(url, world.tokens["steward"], "ok" if mode == "ok" else "refuse", clock.moment)
    first, restart = out["runs"]
    print("WITNESS", json.dumps(out, ensure_ascii=False))
    assert first["sent"] == [{"channel": "wechat", "groupRef": ROOM, "text": "该吃药了，别忘了带水杯"}]
    with world.db.get_connection() as conn:
        notices = conn.execute("SELECT body FROM state_group_messages WHERE direction='out' AND kind='notice'").fetchall()
        # The fire's own steward_schedule_changed (quiet for the steward: SCHEDULE_EVENTS) is the
        # only event; the notice writes none.
        new_events = conn.execute("SELECT event_type FROM state_events ORDER BY rowid DESC LIMIT ?",
                                  (conn.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] - events,)).fetchall()
    assert [tuple(r) for r in notices] == [("该吃药了，别忘了带水杯",)]
    assert {r[0] for r in new_events} <= {"steward_schedule_changed"}
    if mode == "ok":
        assert first["woken"] == [], "a group reminder is no model turn"
    else:
        assert [w["id"] for w in first["woken"]] == [f"group-notice:{made['schedule_id']}:{made['next_run_at']}:not-sent"]
        assert "send_failed" in first["woken"][0]["text"] and "吃药" in first["woken"][0]["text"]
    # A restart: the row moved on, nothing is sent again and nobody is woken again.
    assert restart["sent"] == [] and restart["woken"] == []
