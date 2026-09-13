"""The shipped shell entry reaches the worker's server-only command boundary.

All subprocesses, Git checkouts and fake zg state are confined to tmp_path.
No test starts a production daemon or accesses its home.
"""
import json
import os
import subprocess
from pathlib import Path

from core.semantic_index_control import IndexControl

ROOT = Path(__file__).parents[2]
ENTRYPOINT = ROOT / "docker/zvec-grep-entrypoint.sh"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def setup(tmp_path):
    source = tmp_path / "projects" / "source"
    source.mkdir(parents=True)
    git(source, "init", "-qb", "main")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "config", "user.name", "fixture")
    (source / "source.py").write_text("visible source\n")
    git(source, "add", "source.py")
    git(source, "commit", "-qm", "seed")
    trees = tmp_path / "worktrees"
    trees.mkdir()
    records = []
    for rid in ("live-one", "live-two", "history"):
        path = trees / rid
        git(source, "worktree", "add", "-qb", rid, str(path))
        records.append({"run_id": rid, "worktree_path": str(path), "source_repo": str(source)})
    control = IndexControl(tmp_path / "semantic-index-control", trees)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "zg"
    fake.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
with Path(os.environ['ZG_TEST_LOG']).open('a') as out: out.write(json.dumps(args)+'\\n')
root=Path(args[1]); cache=root/'.zvec-grep'
assert root.parent == Path(os.environ['AITELIER_WORKTREES_DIR'])
assert '--mode' in args and args[args.index('--mode')+1] == 'server'
if os.environ.get('ZG_TEST_FAIL') == '1': raise SystemExit(7)
if args[0] == 'status':
    raise SystemExit(0 if (cache/'manifest.json').exists() else 4)
if '--drop' in args:
    for name in ('manifest.json', 'files.zvec', 'index.zvec'):
        p=cache/name
        if p.exists(): p.unlink()
else:
    cache.mkdir(exist_ok=True)
    for name in ('manifest.json', 'files.zvec', 'index.zvec'):
        (cache/name).write_text('fixture storage')
''')
    fake.chmod(0o755)
    env = {**os.environ, "PATH": str(bindir)+":"+os.environ["PATH"],
           "AITELIER_PROJECTS_DIR": str(tmp_path/"projects"),
           "AITELIER_WORKTREES_DIR": str(trees), "ZG_TEST_LOG": str(tmp_path/"calls.jsonl"),
           "AITELIER_ZG_INDEXER_LIB_ONLY": "1",
           "AITELIER_ZG_WORKER_SCRIPT": str(ROOT/"core/semantic_index_control.py")}
    return control, records, env


def consume(env):
    return subprocess.run(["sh", "-c", f'. "{ENTRYPOINT}"; process_index_requests'],
                          env=env, text=True, capture_output=True, check=True)


def test_only_declared_roots_are_indexed_through_the_actual_worker(tmp_path):
    ctl, records, env = setup(tmp_path)
    for rec in records[:2]:
        ctl.request(rec, "ready")
    consume(env)
    commands = [json.loads(line) for line in Path(env["ZG_TEST_LOG"]).read_text().splitlines()]
    assert len(commands) == 4
    indexed = [c for c in commands if c[0] == "index"]
    assert {c[1] for c in indexed} == {r["worktree_path"] for r in records[:2]}
    assert all("--hidden" in c and "!**/.zvec-grep/**" in c for c in indexed)
    for rec in records[:2]:
        assert ctl.settled(ctl.get(rec["run_id"]), "ready")
        assert git(rec["worktree_path"], "status", "--porcelain") == ""
    assert not (Path(records[2]["worktree_path"])/".zvec-grep").exists()
    assert not (Path(records[0]["source_repo"])/".zvec-grep").exists()
    consume(env)
    assert len(Path(env["ZG_TEST_LOG"]).read_text().splitlines()) == 4


def test_release_drains_via_server_drop_and_never_reindexes_history(tmp_path):
    ctl, records, env = setup(tmp_path)
    rec = records[0]
    ctl.request(rec, "ready")
    consume(env)
    ctl.request(rec, "released")
    consume(env)
    assert ctl.settled(ctl.get(rec["run_id"]), "released")
    assert Path(rec["worktree_path"]).is_dir()
    assert not (Path(rec["worktree_path"])/".zvec-grep").exists()
    calls = Path(env["ZG_TEST_LOG"]).read_text().splitlines()
    assert json.loads(calls[-1]) == ["index", rec["worktree_path"], "--drop", "--yes", "--mode", "server"]
    consume(env)
    assert Path(env["ZG_TEST_LOG"]).read_text().splitlines() == calls


def test_command_failure_never_marks_ready_and_has_retry_backoff(tmp_path):
    ctl, records, env = setup(tmp_path)
    rec = records[0]
    ctl.request(rec, "ready")
    consume({**env, "ZG_TEST_FAIL": "1"})
    failed = ctl.get(rec["run_id"])
    assert failed["outcome"] == "error" and failed["done_revision"] != failed["revision"]
    assert failed["retry_after"] > 0
    with ctl.connection() as conn:
        conn.execute("UPDATE indexes SET retry_after=0")
    consume(env)
    assert ctl.settled(ctl.get(rec["run_id"]), "ready")


def test_compose_enables_host_lifecycle_and_image_copies_the_exact_worker():
    docker = (ROOT/"docker/zvec-grep.Dockerfile").read_text()
    compose = (ROOT/"docker-compose.yml").read_text()
    shell = ENTRYPOINT.read_text()
    assert "git python3" in docker
    assert "COPY core/semantic_index_control.py /usr/local/lib/semantic_index_control.py" in docker
    assert 'AITELIER_ZVEC_LIFECYCLE: "1"' in compose
    assert "index_new_repos" not in shell and "next_repo" not in shell
    assert 'wait "$INDEXER"' in shell and 'trap stop_children EXIT' in shell
    subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True)
