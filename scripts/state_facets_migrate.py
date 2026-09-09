#!/usr/bin/env python3
"""Migrate one chain of a legacy State DAG to facets, through the authorized API.

    python scripts/state_facets_migrate.py --project wuxia-myth --chain coop            # dry-run
    python scripts/state_facets_migrate.py --project wuxia-myth --chain coop --apply

Dry-run reads the live graph, prints the plan (facet labels, new contract/test
nodes, re-pointed edges) and runs the same facet rules the engine enforces over
the RESULTING graph, so a plan that the engine would refuse is refused here first.
`--apply` replays the plan bottom-up through /api/state/commands/* with the CLI
admin token — the same path and the same authorization the director uses. It
refuses to touch a node with an active attempt. Every write is idempotent enough
to re-run after a partial failure: a facet already set is skipped, a node that
already exists is skipped, a revision is skipped when the edges already match.

Recipe and rationale: design/state_facets.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.state_attempts import ACTIVE as ACTIVE_STATUSES  # noqa: E402
from core.state_graph import facet_violations  # noqa: E402

# A generated contract node is a PLACEHOLDER and must say so in a way that
# blocks its own acceptance. The first template said "the interface compiles"
# and "a read-back agrees with this contract" — a blind read-back of it (2026-09-09,
# director/reports/readback-audit-coop-20260909.md) could name no type, signal or
# method from the text, and found the second clause circular: it validated the
# read-back rule itself, so any read-back could claim agreement. `inventory` is
# what makes the placeholder refuse to pass, and `readback` now compares against
# that inventory instead of against itself.
CONTRACT_ACCEPTANCE = [
    {"id": "inventory", "kind": "artifact",
     "description": "本契约须在验收前被修订为逐项列出接口清单：类型/信号/方法签名及其参数、返回与错误约定，并写明用于判定「编译通过」的确切命令。清单为空即不可验收；本条不得由交付方自证"},
    {"id": "interface", "kind": "test",
     "description": "接口与 stub/fake 按 inventory 清单逐项实现并通过该命令；随附示例测试在只挂载本节点产物的隔离环境中全部通过（证明下游仅凭本节点即可对 fake 编写并运行测试）"},
    {"id": "readback", "kind": "review",
     "description": "独立 read-back（只读本契约，不读 goal 原文与设计文档）复述出的接口清单与交付的接口定义逐项比对：缺项/多项/签名不符各计一处偏差，偏差为 0 方可通过；记录为 note readback@r<revision>"},
]
TEST_ACCEPTANCE = [
    {"id": "locatable", "kind": "test",
     "description": "测试按 node key 可定位（tests/<domain>/<key>/ 或 playtest 场景前缀 <key>）并可执行；实现未交付前对着 fake 为红或 skip"},
    {"id": "covers", "kind": "review", "description": "导演认证测试覆盖对应契约的每条验收项"},
]


def admin_token():
    token = os.environ.get("AITELIER_ADMIN_TOKEN")
    if not token:
        env = Path(__file__).resolve().parents[1] / ".env"
        for line in env.read_text().splitlines() if env.exists() else []:
            m = re.match(r"^(?:export\s+)?AITELIER_ADMIN_TOKEN=(.*)$", line.strip())
            if m:
                token = m.group(1).strip().strip('"').strip("'")
    if not token:
        raise SystemExit("AITELIER_ADMIN_TOKEN not set and not found in .env")
    return token


class Api:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token

    def _call(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-AItelier-Admin-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{path} -> HTTP {exc.code}: {exc.read().decode(errors='replace')[:600]}")

    def query(self, action, **args):
        return self._call(f"/api/state/query/{action}", args)

    def command(self, action, **args):
        return self._call(f"/api/state/commands/{action}", args)


def matches(node_key, selectors):
    return any(node_key == s or node_key.startswith(s + ".") for s in selectors)


def plan(graph_nodes, chain, sample_tests, integration=(), frozen=()):
    """Compute the target graph. Returns (steps, facets, edges, skipped) for lint + apply.

    `chain` is a node-key prefix, or "all" for every non-design node — the whole
    project must migrate at once once dependencies cross chains, which they do
    as soon as more than one domain is faceted. `integration` nodes keep their
    edges: composing real implementations is precisely their job. `frozen` nodes
    (an attempt is in flight) are never revised or labelled — retargeting a
    running attempt's dependency snapshot is the one thing this must not do —
    but their contract IS created, so everything downstream can still migrate.
    """
    nodes = {n["node_key"]: n for n in graph_nodes}
    facets = {k: n.get("facet") for k, n in nodes.items()}
    edges = {k: list(n["dependencies"]) for k, n in nodes.items()}
    steps = []

    def contract_of(dep):
        # A design node is buildable as is, and a contract node IS the contract —
        # appending the suffix to one invents `x.contract.contract`. Anything
        # else is reached through its contract.
        if facets.get(dep) in ("design", "contract") or dep.startswith("design."):
            return dep
        return dep + ".contract"

    # Design nodes depend on design nodes, and a label is checked against the
    # node's edges, so they too are labelled dependencies-first.
    def label_design(k):
        if not k.startswith("design.") or facets.get(k) is not None:
            return
        for d in edges[k]:
            label_design(d)
        steps.append(("set_facet", k, "design"))
        facets[k] = "design"
    for k in sorted(nodes):
        label_design(k)
    members = [k for k in sorted(nodes)
               if not k.startswith("design.") and not k.endswith((".contract", ".test"))
               and (chain == "all" or k.startswith(chain + "."))]
    # Bottom-up so every contract exists before an edge points at it.
    order, seen = [], set()

    def visit(k):
        if k in seen or k not in members:
            return
        seen.add(k)
        for d in edges[k]:
            visit(d)
        order.append(k)
    for k in members:
        visit(k)
    skipped = []
    for k in order:
        own_siblings = {k + ".contract", k + ".test"}
        foreign = [d for d in edges[k] if d not in members and d not in own_siblings
                   and not d.startswith("design.") and facets.get(d) not in ("contract", "design")]
        if foreign:
            skipped.append((k, "depends outside the migrated set on " + ", ".join(foreign)))
            continue
        base = nodes[k]
        if matches(k, integration):
            # Edges stay: an integration node is where real implementations meet.
            if facets.get(k) is None:
                steps.append(("set_facet", k, "integration"))
                facets[k] = "integration"
            continue
        title = base["goal"].splitlines()[0][:120]
        ck, tk = k + ".contract", k + ".test"
        # A node's own contract/test are added below as `own`; passing them
        # through contract_of would ask for `<k>.test.contract`.
        cdeps = sorted({contract_of(d) for d in edges[k] if d not in own_siblings})
        if ck not in nodes:
            steps.append(("add", {"key": ck, "facet": "contract", "dependencies": cdeps,
                                  "goal": f"「{title}」的接口契约：下游可依赖的类型/信号/方法签名与 stub/fake，不含实现。\n原目标：{base['goal'][:600]}",
                                  "acceptance": CONTRACT_ACCEPTANCE, "priority": base.get("priority", 0) + 1}))
            facets[ck], edges[ck], nodes[ck] = "contract", cdeps, {"goal": title}
        own = [ck]
        if k in sample_tests and tk not in nodes:
            steps.append(("add", {"key": tk, "facet": "test", "dependencies": sorted({ck, *cdeps}),
                                  "goal": f"「{title}」的验收测试：对着 {ck} 的 fake 可运行，实现未交付前为红/跳过；按 key 可定位。",
                                  "acceptance": TEST_ACCEPTANCE, "priority": base.get("priority", 0)}))
            facets[tk], edges[tk], nodes[tk] = "test", sorted({ck, *cdeps}), {"goal": title}
            own.append(tk)
        elif tk in nodes:
            own.append(tk)
        if k in frozen:
            # Its contract now exists for everything downstream; the node itself
            # is left legacy (and therefore exempt) until its attempt settles.
            skipped.append((k, "attempt in flight: contract created, node left legacy until it settles"))
            continue
        new_deps = sorted({*own, *cdeps})
        if new_deps != sorted(edges[k]):
            steps.append(("revise", k, base["revision"], new_deps))
            edges[k] = new_deps
        if facets.get(k) is None:
            steps.append(("set_facet", k, "content"))
            facets[k] = "content"
    return steps, facets, edges, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--chain", required=True, help='node-key prefix, e.g. coop, or "all"')
    ap.add_argument("--sample-test", action="append", default=[], help="node key that gets a .test node now")
    ap.add_argument("--integration", action="append", default=[],
                    help="node key or prefix whose nodes compose real implementations (e.g. validation)")
    ap.add_argument("--base", default=os.environ.get("AITELIER_URL", "http://127.0.0.1:4444"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    api = Api(args.base, admin_token())
    graph = api.query("get_graph", project_id=args.project)["nodes"]
    frozen = {a["node_key"] for a in api.query("project_attempts", project_id=args.project, limit=100)["attempts"]
              if a["status"] in ACTIVE_STATUSES}
    steps, facets, edges, skipped = plan(graph, args.chain, set(args.sample_test), args.integration, frozen)
    problems = facet_violations(facets, edges)
    print(f"project {args.project} chain {args.chain}: {len(graph)} nodes now, {len(facets)} after; {len(steps)} steps")
    for step in steps:
        if step[0] == "add":
            print(f"  add     {step[1]['key']:40} facet={step[1]['facet']:9} deps={step[1]['dependencies']}")
        elif step[0] == "revise":
            print(f"  revise  {step[1]:40} r{step[2]} deps -> {step[3]}")
        else:
            print(f"  facet   {step[1]:40} -> {step[2]}")
    for k, why in skipped:
        print(f"  SKIP    {k:40} {why}")
    print("facet_lint on the resulting graph:", "clean" if not problems else "")
    for p in problems:
        print("  VIOLATION", p)
    if problems:
        return 2
    if not args.apply:
        print("dry-run only; add --apply to execute")
        return 0
    touched = {step[1] if step[0] != "add" else step[1]["key"] for step in steps}
    if touched & frozen:
        raise SystemExit("refusing: attempt in flight on " + ", ".join(sorted(touched & frozen)))
    live = {n["node_key"]: n for n in api.query("get_graph", project_id=args.project)["nodes"]}
    for step in steps:
        if step[0] == "add":
            if step[1]["key"] in live:
                print("  exists ", step[1]["key"]); continue
            api.command("add_nodes", project_id=args.project, nodes=[step[1]])
            print("  added  ", step[1]["key"])
        elif step[0] == "revise":
            _, k, rev, deps = step
            current = api.query("get_node", project_id=args.project, node_key=k)["node"]
            if sorted(current["dependencies"]) == deps:
                print("  same   ", k); continue
            api.command("revise_node", project_id=args.project, node_key=k, expected_revision=current["revision"],
                        reason="facets migration: build on contracts, not implementations (design/state_facets.md)",
                        dependencies=deps)
            print("  revised", k)
        else:
            _, k, value = step
            api.command("set_node_facet", project_id=args.project, node_key=k, facet=value)
            print("  facet  ", k, value)
    lint = api.query("facet_lint", project_id=args.project)
    print("live facet_lint:", json.dumps({k: lint[k] for k in ("violations", "faceted", "legacy")}, ensure_ascii=False))
    return 0 if not lint["violations"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
