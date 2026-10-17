"""Second host process over an existing native round world (process-loss fault).

Reopens the durable world under ``root`` as a restarted host would, binds the
optimizer ports into that host, freezes the given round as its coordinator and
exits. Its live round binding ends with the process; the round's engine run
stays durable.
"""
import json
import sys
from pathlib import Path

from contracts.rsi.optimizer.native_fixture import NativeRoundFixture


def main(root: str, request_path: str) -> int:
    fixture = NativeRoundFixture(root=Path(root))
    payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
    view = fixture.round.invoke("freeze", payload, actor="coordinator")
    print(json.dumps({"child_phase": view.phase, "child_run": fixture.round.run_id}))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
