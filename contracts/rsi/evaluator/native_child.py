"""Second evaluator process over an existing native world (process-loss fault).

Reopens the durable world under ``root`` exactly as a restarted host would
(ordinary registration of the generated targets and seals), then evaluates the
given request with a transport that holds the first physical call open. The
parent kills this process with SIGKILL while that call is in flight.
"""
import json
import sys
from pathlib import Path

from contracts.rsi.evaluator.contract import EvaluationRequest
from contracts.rsi.evaluator.native_fixture import NativeWorld
from contracts.rsi.evaluator.native_transport import DeterministicProvider
from core.rsi_evaluator import NativeEvaluator


def main(root: str, request_path: str) -> int:
    world = NativeWorld(Path(root), fresh=False)
    public = json.loads((Path(root) / "public-package" / "public.json").read_text())
    provider = DeterministicProvider(Path(root) / "provider-log.jsonl", public)
    request = EvaluationRequest.model_validate_json(Path(request_path).read_text())
    provider.first_cell = (request.cells[0].case_id, request.cells[0].arm)
    provider.fault = "unknown_execution"
    result = NativeEvaluator(world.host(provider)).evaluate(request)
    print(json.dumps({"unexpected_return": type(result).__name__}))
    return 3


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
