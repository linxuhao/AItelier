"""Owned SQLite lifecycle scope regressions. No native engine or production data."""
import gc,importlib.util,json,os,sqlite3,subprocess,threading,urllib.error,urllib.request
from contextlib import contextmanager
from pathlib import Path
import pytest
HARNESS=Path(__file__).resolve().parents[2]/"docker/godot/godot_harness.py"
def load(tmp_path,monkeypatch):
    for key,name in [("GODOT_LIFECYCLE_DB","owners.sqlite3"),("GODOT_DEPLOYMENT_LOCK","deploy.lock"),("GODOT_RENDER_EFFECT_LOCK","effect.lock"),("GODOT_EVIDENCE_ROOT","evidence")]:monkeypatch.setenv(key,str(tmp_path/name))
    spec=importlib.util.spec_from_file_location("scope_harness",HARNESS);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
def db_fds(m):
    out={}
    for fd in os.listdir("/proc/self/fd"):
        try:target=os.readlink("/proc/self/fd/"+fd)
        except FileNotFoundError:continue
        if target.startswith(str(m.LIFECYCLE_DB)):out[fd]=target
    return out
@contextmanager
def no_gc():
    was=gc.isenabled();gc.disable()
    try:yield
    finally:
        if was:gc.enable()
def test_connection_is_closed_with_retained_reference_without_gc(tmp_path,monkeypatch):
    m=load(tmp_path,monkeypatch)
    with no_gc():
        before=db_fds(m)
        with m._lifecycle_connection() as conn:assert conn.execute("SELECT 1").fetchone()[0]==1
        assert db_fds(m)==before
        with pytest.raises(sqlite3.ProgrammingError):conn.execute("SELECT 1")
def test_connection_commits_body_and_closes_before_followup(tmp_path,monkeypatch):
    m=load(tmp_path,monkeypatch)
    with no_gc():
        with m._lifecycle_connection() as conn:
            conn.execute("CREATE TABLE owned_probe(value INTEGER)")
            conn.execute("INSERT INTO owned_probe VALUES(7)")
        assert not db_fds(m)
        with m._lifecycle_connection() as fresh:assert fresh.execute("SELECT value FROM owned_probe").fetchone()[0]==7
        assert not db_fds(m)
def test_connection_rolls_back_original_exception_and_closes(tmp_path,monkeypatch):
    m=load(tmp_path,monkeypatch)
    with no_gc():
        with m._lifecycle_connection() as conn:
            conn.execute("CREATE TABLE owned_probe(value INTEGER)");conn.commit()
            with pytest.raises(ValueError):
                # Separate scope so its SQLite transaction gets the exception.
                with m._lifecycle_connection() as failed:
                    failed.execute("INSERT INTO owned_probe VALUES(9)")
                    raise ValueError("owned causal rollback")
            with pytest.raises(sqlite3.ProgrammingError):failed.execute("SELECT 1")
            assert conn.execute("SELECT COUNT(*) FROM owned_probe").fetchone()[0]==0
        assert not db_fds(m)
def test_connection_initialization_failure_closes_allocated_fd(tmp_path,monkeypatch):
    m=load(tmp_path,monkeypatch);actual=m.sqlite3.connect;refs=[]
    class InitFailure(sqlite3.Connection):
        def execute(self,sql,*a,**kw):
            if "CREATE TABLE IF NOT EXISTS render_owners" in sql:raise sqlite3.OperationalError("owned schema fault")
            return super().execute(sql,*a,**kw)
    def connect(*a,**kw):
        conn=actual(*a,factory=InitFailure,**kw);refs.append(conn);return conn
    monkeypatch.setattr(m.sqlite3,"connect",connect)
    with no_gc():
        with pytest.raises(sqlite3.OperationalError,match="owned schema fault"):
            with m._lifecycle_connection():pass
        assert refs and not db_fds(m)
        with pytest.raises(sqlite3.ProgrammingError):refs[0].execute("SELECT 1")
def test_owner_read_write_refusal_and_release_close_all_scopes(tmp_path,monkeypatch):
    m=load(tmp_path,monkeypatch)
    with no_gc():
        row=m.acquire_render_owner("owned-p","owned-r","owned-op")
        assert not db_fds(m)
        with pytest.raises(m.RenderOwnerConflict):m.acquire_render_owner("other-p","other-r","other-op")
        assert not db_fds(m)
        m.heartbeat_render_owner(row["owner_id"],row["generation"]);assert not db_fds(m)
        assert m.render_owner_snapshot()[0]["status"]=="active";assert not db_fds(m)
        m.release_render_owner(row["owner_id"],row["generation"]);assert not db_fds(m)
        assert m.render_owner_snapshot()[0]["status"]=="released";assert not db_fds(m)
@pytest.mark.parametrize("outcome",["pass","exception"])
def test_fresh_normal_http_closes_ownerdb_fds_without_warmup_or_gc(tmp_path,monkeypatch,outcome):
    m=load(tmp_path,monkeypatch);proj=tmp_path/"project";proj.mkdir();(proj/"project.godot").write_text('[application]\nconfig/name="OwnedFD"\n');(proj/"tests").mkdir();(proj/"tests/a.gd").write_text("extends SceneTree\n")
    homes=[]
    def boundary(argv,timeout,extra_env=None,render=False):
        home=Path(extra_env["HOME"]);homes.append(home)
        f=home/".local/share/godot/app_userdata/OwnedFD/report.json";f.parent.mkdir(parents=True);f.write_text('{"owned":true}')
        if outcome=="exception" and "--import" not in argv:raise RuntimeError("owned engine boundary exception")
        return subprocess.CompletedProcess(argv,0,"PASS owned\n","")
    monkeypatch.setattr(m,"_run",boundary);done=threading.Event();release=m.release_render_owner
    def observed(*a,**kw):
        try:return release(*a,**kw)
        finally:done.set()
    monkeypatch.setattr(m,"release_render_owner",observed)
    with no_gc():
        before=db_fds(m)
        server=m.ThreadingHTTPServer(("127.0.0.1",0),m._Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        req=urllib.request.Request(f"http://127.0.0.1:{server.server_port}/script",data=json.dumps({"project_dir":str(proj),"scripts":["res://tests/a.gd"],"retain":{"files":["report.json"]}}).encode(),headers={"Content-Type":"application/json"})
        try:
            try:
                with urllib.request.urlopen(req,timeout=30) as response:code=response.status;body=json.loads(response.read())
            except urllib.error.HTTPError as exc:code=exc.code;body=json.loads(exc.read())
            assert done.wait(5)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)
        assert db_fds(m)==before
        assert all(not home.exists() for home in homes)
        if outcome=="pass":assert code==200 and body["passed"] and body["retention"]["ok"]
        else:assert code==500 and "owned engine boundary exception" in body["error"]
