import sys, os, json, shutil, subprocess, re, concurrent.futures as cf
WT = "/home/linxuhao/.AItelier/dsh-drivers/dsh-c61eed4a13ba4aaaae96/work/c7-catalog-run-20261108"
BASE = "/tmp/mc-20261108-base"
WORK = "/tmp/mc-20261108-m"
RAW = os.path.join(WT, "evidence/mutation-catalog-run-20261108/raws")
os.makedirs(RAW, exist_ok=True)
sys.path.insert(0, os.path.join(WT, "tools/mutation_catalog"))
from mutations import MUTATIONS

def safe(n):
    return re.sub(r"[^A-Za-z0-9_]", "_", n)

def prep(name, spec):
    scr = os.path.join(WORK, safe(name))
    shutil.rmtree(scr, ignore_errors=True)
    shutil.copytree(BASE, scr, symlinks=False)
    counts = []
    for e in spec["edits"]:
        p = os.path.join(scr, e["file"])
        txt = open(p, encoding="utf-8").read()
        c = txt.count(e["anchor"])
        counts.append({"file": e["file"], "count": c})
        if c != 1:
            return scr, counts, False
        txt = txt.replace(e["anchor"], e["replacement"], 1)
        open(p, "w", encoding="utf-8").write(txt)
    return scr, counts, True

def parse(raw_path):
    reds = []
    summary = ""
    for line in open(raw_path, encoding="utf-8", errors="replace"):
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            nid = line.split(" ", 1)[1].split(" - ")[0].strip()
            reds.append(line.split(" ", 1)[0] + " " + nid)
        if re.match(r"^=+ .*(passed|failed|error|no tests)", line) or re.search(r"\d+ (passed|failed|error)", line) and line.startswith("="):
            summary = line.strip()
    return reds, summary

def run_one(name):
    spec = MUTATIONS[name]
    scr, counts, ok = prep(name, spec)
    raw = os.path.join(RAW, f"{safe(name)}.mutant.txt")
    rec = {"name": name, "kind": spec.get("kind", "behaviour"), "targeted": spec["targeted"],
           "edit_counts": counts, "anchor_ok": ok}
    if not ok:
        open(raw, "w").write("ANCHOR-ERROR: edit counts=%r; no pytest run\n" % counts)
        rec.update({"rc": None, "reds": [], "summary": "anchor-error"})
        return rec
    cname = "mc20261108-" + safe(name).lower()[:40]
    out = subprocess.run(["/tmp/mc-20261108/arm.sh", cname, scr, raw] + spec["targeted"],
                         capture_output=True, text=True)
    rc_line = [l for l in out.stdout.splitlines() if l.startswith("RC=")]
    rc = int(rc_line[0][3:]) if rc_line else None
    reds, summary = parse(raw)
    rec.update({"rc": rc, "reds": reds, "summary": summary, "arm_stdout": out.stdout.strip(), "arm_stderr": out.stderr.strip()[-500:]})
    return rec

if __name__ == "__main__":
    names = sys.argv[2:] if len(sys.argv) > 2 else list(MUTATIONS)
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    os.makedirs(WORK, exist_ok=True)
    results = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_one, n): n for n in names}
        for f in cf.as_completed(futs):
            n = futs[f]
            try:
                r = f.result()
            except Exception as exc:
                r = {"name": n, "error": repr(exc)}
            results[n] = r
            print(n, r.get("rc"), len(r.get("reds", [])), r.get("summary", r.get("error")), flush=True)
    json.dump(results, open("/tmp/mc-20261108/mutant-results.json", "w"), indent=1, sort_keys=True)
    print("DONE", len(results))
