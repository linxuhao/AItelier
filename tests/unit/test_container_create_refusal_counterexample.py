"""Actual owned_container plus an inert CLI daemon stand-in; never real Docker."""
import json
from pathlib import Path
import sys
import pytest
from core import repository_executor as executor

def test_refused_create_preserves_preexisting_container(tmp_path, record_property):
    peer = tmp_path / "protected-peer"
    peer.write_bytes(b"existing unrelated execution")
    calls_path = tmp_path / "calls.jsonl"
    cli = tmp_path / "docker-stand-in"
    cli.write_text(
        "#!" + sys.executable + "\n"
        "import json,sys\nfrom pathlib import Path\n"
        "args=sys.argv[1:]\n"
        f"peer=Path({str(peer)!r})\n"
        f"with open({str(calls_path)!r},'a') as out: out.write(json.dumps(args)+'\\n')\n"
        "if args[0]=='create':\n"
        " print('Error response from daemon: Conflict. The container name is already in use',file=sys.stderr)\n"
        " sys.exit(125)\n"
        "if args[:2]==['rm','-f'] and args[2]=='protected-peer':\n"
        " peer.unlink(missing_ok=True)\n"
        " print(args[2])\n"
        " sys.exit(0)\n"
        "sys.exit(2)\n"
    )
    cli.chmod(0o755)
    with pytest.raises(executor.IsolationUnavailable) as raised:
        with executor.owned_container(
            [str(cli), "create", "--name", "protected-peer", "inert-image"],
            str(cli), "protected-peer",
        ):
            pytest.fail("a conclusively refused CREATE must not start")
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    record_property("actual_cli_calls", json.dumps(calls))
    record_property("peer_present_after", str(peer.exists()))
    assert not isinstance(raised.value, executor.CleanupUnconfirmed)
    assert peer.exists(), f"refused CREATE erased existing peer; actual calls={calls!r}"
    assert peer.read_bytes() == b"existing unrelated execution"
    assert [call[0] for call in calls] == ["create"], calls
