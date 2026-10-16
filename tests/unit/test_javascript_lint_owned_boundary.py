"""Closed profile validation plus the actual baked syntax parser entry."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest
from core import repository_executor as ex, repository_host_launcher as host


@pytest.fixture
def owned(tmp_path):
    repo = tmp_path / "projects/repo";repo.mkdir(parents=True)
    (repo / "file.js").write_bytes(b"const value = 1;\n")
    home = tmp_path / "home";home.mkdir()
    run = "owned-lint"
    ticket = ex.node_report_root(home, run) / "lint-owned";ticket.mkdir(parents=True)
    (ticket / "input.js").write_bytes((repo / "file.js").read_bytes())
    db = tmp_path / "fixture.sqlite"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE run_isolation(run_id TEXT,mode TEXT,worktree_path TEXT,source_repo TEXT,project_id TEXT)")
        c.execute("INSERT INTO run_isolation VALUES(?,?,?,?,?)",(run,"direct","",str(repo),"fixture"))
    cfg={"allowed_roots":[str(tmp_path / "projects")],"aitelier_home":str(home),"state_db":str(db),
         "executor_entry":str(Path(host.__file__).with_name("repository_executor_entry.py")),
         "allowed_uids":[os.getuid()],"test_image":"sha256:"+"a"*64,"node_executor_image":"sha256:"+"b"*64}
    req={"op":"launch","repo":str(repo),"run_id":run,"profile":ex.LINT_PROFILE,"args":["file.js"],
         "timeout":120,"writable_dirs":[str(ticket)],"workdir":"","npm_cache":""}
    return cfg,req,repo,ticket


def test_closed_lint_payload_mounts_only_private_readonly_input(owned, monkeypatch):
    cfg,req,repo,ticket=owned
    fields=host.validate_request(cfg,req)
    monkeypatch.setattr(host.shutil,"which",lambda name:"/usr/bin/docker")
    argv=host.docker_command(cfg,fields,"own-container")
    mounts=[argv[i+1] for i,v in enumerate(argv) if v=="-v"]
    assert mounts==[f"{ticket}:/lint-input:ro",f"{cfg['executor_entry']}:/executor.py:ro"]
    assert str(repo) not in mounts and argv[argv.index("--network")+1]=="none"
    payload=json.loads(argv[-1])
    assert payload["args"]==[ex.LINT_NODE,ex.LINT_ESLINT,"--no-config-lookup","--no-inline-config","--stdin","--stdin-filename","input.js"]
    assert payload["input_sha256"]==hashlib.sha256((repo/"file.js").read_bytes()).hexdigest()
    assert payload["repo"]=="/tmp"


@pytest.mark.parametrize("attack",["missing-run","foreign-run","foreign-root","foreign-ticket","snapshot-change","source-change","ticket-alias","report-alias","shell","extra-arg","stdin-option","relay","writable-overlap"])
def test_malicious_binding_or_profile_is_refused(owned,tmp_path,attack):
    cfg,req,repo,ticket=owned
    if attack=="missing-run": req["run_id"]=""
    elif attack=="foreign-run": req["run_id"]="other"
    elif attack=="foreign-root": req["repo"]=str(tmp_path)
    elif attack=="foreign-ticket": req["writable_dirs"]=[str(ticket.parent.parent/"foreign/lint-owned")]
    elif attack=="snapshot-change": (ticket/"input.js").write_text("different")
    elif attack=="source-change": (repo/"file.js").write_text("changed source")
    elif attack=="ticket-alias":
        alias=ticket.parent/"lint-alias";alias.symlink_to(ticket,target_is_directory=True);req["writable_dirs"]=[str(alias)]
    elif attack=="report-alias":
        original=ticket.parent;other=tmp_path/"elsewhere";original.rename(other);original.symlink_to(other,target_is_directory=True)
    elif attack=="shell": req["args"]=["bash","-c","anything"]
    elif attack=="extra-arg": req["args"].append("--fix")
    elif attack=="stdin-option": req["args"]=["--stdin.js"]
    elif attack=="relay": req["relay_socket"]="/outside.sock"
    else: req["writable_dirs"]=[str(repo)]
    with pytest.raises(host.LaunchRefused): host.validate_request(cfg,req)


@pytest.fixture
def entry():
    path=Path(ex.__file__).with_name("repository_executor_entry.py")
    spec=importlib.util.spec_from_file_location("owned_lint_entry",path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("suffix,code,rc",[("js","await Promise.resolve(42);",0),
    ("mjs","export const café=42;",0),("cjs","module.exports = require('node:path');",0),
    ("cjs","import x from 'node:path';",1),("js","const broken = ;",1),("js","",0),
    ("js","/* eslint no-undef: 'error' */ missingName;",0)])
def test_actual_baked_cli_reads_exact_stdin_with_extension_semantics(entry,tmp_path,suffix,code,rc):
    fp=tmp_path/("input."+suffix);fp.write_text(code)
    payload={"repo":"/tmp","args":[ex.LINT_NODE,ex.LINT_ESLINT,"--no-config-lookup","--no-inline-config","--stdin","--stdin-filename",fp.name],"lint_input":str(fp),"input_sha256":hashlib.sha256(fp.read_bytes()).hexdigest(),"timeout":10}
    result=entry.run(payload)
    assert result["returncode"]==rc,result
    assert not result.get("unavailable") and not result["timed_out"]
    if rc: assert "Parsing error" in result["stdout"]


def test_entry_refuses_missing_pinned_tool_before_input_execution(entry,tmp_path,monkeypatch):
    fp=tmp_path/"input.js";fp.write_text("let value=1")
    monkeypatch.setattr(entry.subprocess,"run",lambda *a,**k:subprocess.CompletedProcess([],0,"v0.0.0",""))
    monkeypatch.setattr(entry.subprocess,"Popen",lambda *a,**k:pytest.fail("unavailable executed"))
    result=entry.run({"repo":"/tmp","args":[ex.LINT_NODE,ex.LINT_ESLINT],"lint_input":str(fp),"input_sha256":hashlib.sha256(fp.read_bytes()).hexdigest(),"timeout":10})
    assert result["returncode"]==-1 and "unavailable" in result
