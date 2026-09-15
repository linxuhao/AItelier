"""Independent-shaped identity and executable-surface regression controls."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from cli import server
from core import deployment_lifecycle as lifecycle

IDS = {service: str(index) * 64 for index, service in enumerate(server._COMPOSE_SERVICES, 1)}


def observation(service):
    return {"ID": IDS[service], "Name": server._COMPOSE_CONTAINERS[service],
            "Service": service, "State": "running", "Health": "healthy"}


def container(service):
    return {"Id": IDS[service], "Name": "/" + server._COMPOSE_CONTAINERS[service],
            "Config": {"Labels": {"com.docker.compose.project": "review-project",
                                   "com.docker.compose.service": service, "com.docker.compose.oneoff": "False"}},
            "State": {"Running": True, "Status": "running", "Paused": False,
                      "Restarting": False, "Dead": False, "Health": {"Status": "healthy"}}}


@pytest.fixture
def inventories(monkeypatch):
    rows = {service: observation(service) for service in IDS}
    current = {ident: container(service) for service, ident in IDS.items()}
    calls = []
    def compose(*args, **kwargs):
        calls.append(args)
        if args == ("config", "--format", "json"):
            return SimpleNamespace(returncode=0, stdout=json.dumps({"name": "review-project"}))
        return SimpleNamespace(returncode=0, stdout=json.dumps([rows[args[-1]]]))
    def run(argv, **kwargs):
        calls.append(tuple(argv))
        assert argv[:4] == ["docker", "inspect", "--type", "container"]
        assert kwargs["env"] == server._compose_env()
        assert kwargs["timeout"] == 15
        if argv[-1] not in current:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=json.dumps([current[argv[-1]]]))
    monkeypatch.setattr(server, "_compose", compose)
    monkeypatch.setattr(server.subprocess, "run", run)
    return rows, current, calls


def test_full_identity_needs_two_correlated_current_authorities(inventories):
    rows, current, calls = inventories
    assert server._guarded_service_errors() == []
    assert calls[0] == ("config", "--format", "json")
    assert [call[-1] for call in calls if call[0] == "docker"] == list(IDS.values())


@pytest.mark.parametrize("value", [None, "", "a" * 12, "a" * 63, "a" * 65,
                                   "A" * 64, "g" * 64, "a" * 64 + "\n", 12, True])
def test_bad_census_id_never_reaches_inspect(inventories, value):
    rows, current, calls = inventories
    rows["zvec-grep"]["ID"] = value
    assert server._guarded_service_errors()
    assert not any(call[0] == "docker" and call[-1] == value for call in calls)


def test_syntactically_valid_fabricated_or_stale_id_is_refused(inventories):
    rows, current, calls = inventories
    rows["zvec-grep"]["ID"] = "a" * 64
    assert any("corroboration" in error for error in server._guarded_service_errors())


@pytest.mark.parametrize("path,value", [
    (("Id",), "a" * 64), (("Name",), "/aitelier-zg-old"),
    (("Config", "Labels", "com.docker.compose.project"), "other-project"),
    (("Config", "Labels", "com.docker.compose.service"), "godot-builder"),
    (("State", "Running"), False), (("State", "Running"), 1),
    (("State", "Status"), "exited"), (("State", "Paused"), True),
    (("State", "Restarting"), True), (("State", "Dead"), True),
    (("State", "Health", "Status"), "starting"),
    (("State", "Health", "Status"), "unhealthy"),
    (("Config", "Labels"), None), (("State", "Health"), None),
])
def test_inspect_identity_labels_and_live_state_fail_closed(inventories, path, value):
    rows, current, calls = inventories
    target = current[IDS["zvec-grep"]]
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    assert any("zvec-grep container corroboration" in error for error in server._guarded_service_errors())


@pytest.mark.parametrize("payload", ["", "null", "{}", "[]", "[{},{}]", "[true]", "{"])
def test_malformed_inspect_is_not_identity(inventories, monkeypatch, payload):
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload))
    assert server._guarded_service_errors()


@pytest.mark.parametrize("failure", [PermissionError("denied"), subprocess.TimeoutExpired("docker inspect", 15)])
def test_inspect_unavailable_is_not_identity(inventories, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(server.subprocess, "run", fail)
    assert server._guarded_service_errors()


def test_duplicate_and_cross_service_id_refused(inventories):
    rows, current, calls = inventories
    rows["godot-builder"]["ID"] = IDS["zvec-grep"]
    assert any("reuses container ID" in error for error in server._guarded_service_errors())
    rows["zvec-grep"]["ID"], rows["godot-builder"]["ID"] = IDS["godot-builder"], IDS["zvec-grep"]
    assert len(server._guarded_service_errors()) == 2


@pytest.mark.parametrize("project", [None, "", "UPPER", "../other", 42])
def test_project_resolution_must_be_authoritative(inventories, monkeypatch, project):
    monkeypatch.setattr(server, "_compose", lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps({"name": project})))
    assert any("project identity unavailable" in error for error in server._guarded_service_errors())


@pytest.mark.parametrize("verb", sorted(lifecycle.LIFECYCLE_COMMANDS))
@pytest.mark.parametrize("template", [
    'docker compose {verb} aitelier',
    'docker-compose -f config.yml --project-name=review {verb}',
    'sudo -u root env X=1 bash -lc "docker --context=desktop compose -fconfig.yml {verb}"',
    'docker compose \\\n --profile production \\\n {verb} aitelier',
    'command /usr/bin/docker compose {verb}',
    'exec -a worker docker compose {verb}',
    'nice -5 bash -o pipefail -ec "docker compose {verb}"',
])
def test_all_lifecycle_verbs_and_shell_forms(verb, template):
    assert lifecycle.shell_actions(template.format(verb=verb)) == [verb]


@pytest.mark.parametrize("source", [
    "echo ';' docker compose up", "printf '%s' '|' docker compose up",
    "echo 'docker compose up -d'", "printf '%s' 'docker compose restart'",
    'docker compose --dry-run up -d', 'docker compose --dry-run=true restart',
    'echo docker compose up', "printf '%s' 'a; docker compose up'",
    'echo "docker compose stop | docker compose start"',
    'command -v docker compose up',
    'docker compose --help', 'docker compose',
    'docker compose logs --follow', 'docker compose ps --all',
])
def test_shell_data_and_readonly_controls(source):
    assert lifecycle.shell_actions(source) == []


@pytest.mark.parametrize(("source", "expected"), [
    ("printf x | sed 'e docker compose up'", ["up"]),
    ("sed -e '1e docker compose restart' input.txt", ["restart"]),
    ("sed 's/x/docker compose up/e' input.txt", ["up"]),
    ("sed --expression='s|x|docker compose restart|e' input.txt", ["restart"]),
])
def test_gnu_sed_shell_execution_is_inventory_visible(source, expected):
    assert lifecycle.shell_actions(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ("sed '/x/s/x/docker compose up/e' input", ["up"]),
    ("sed '1,2s/x/docker compose restart/e' input", ["restart"]),
    ("sed '/a/,/b/!s|x|docker compose up|e' input", ["up"]),
    ("sed '{e docker compose up\n}' input", ["up"]),
    ("sed -e '/x/{\ne docker compose restart\n}' input", ["restart"]),
    ("sed 'e echo harmless; docker compose up' input", ["up"]),
    ("sed -- 'e docker compose restart' input", ["restart"]),
    ("sed -e 's/x/y/' -e 'e docker compose up' input", ["up"]),
    ("sed -f rules.sed -e 's/x/docker compose restart/e' input", ["restart"]),
    ("sed -frules.sed -ne 's/x/docker compose up/e' input", ["up"]),
    ("sed -n -es/x/docker\\ compose\\ up/e input", ["up"]),
    (r"sed -e 's|x\|y|docker compose up|e' input", ["up"]),
    ("sed -f <(printf 'e docker compose up') input", ["unknown"]),
    ("sed --file=<(printf 'e docker compose restart') input", ["unknown"]),
])
def test_gnu_sed_structural_execution_forms_are_visible(source, expected):
    assert lifecycle.shell_actions(source) == expected


@pytest.mark.parametrize("source", [
    "sed 's/docker compose up/safe/' README.md",
    "sed -n '/docker compose up/p' README.md",
    "sed -e 's/x/docker compose up/' input.txt",
    "sed 's/x/value e docker compose up/' input.txt",
    "sed 's/x/;e docker compose up/' input.txt",
    "sed 's/x/; e docker compose up/' input.txt",
    "sed 's/;e docker compose up/safe/' input.txt",
    "sed '/;e docker compose up/p' input.txt",
    "sed 'y|docker compose up|safe text here|' input.txt",
    "sed 'a docker compose up' input.txt",
    "sed 'r docker compose up' input.txt",
    "sed -f docker-compose-up.sed input.txt",
    "sed -frules.sed -e 's/x/;e docker compose up/' input.txt",
    "sed 'q 3' input.txt",
    "sed 'v 4.9' input.txt",
    "sed 'e echo docker compose up' input.txt",
])
def test_gnu_sed_compose_data_does_not_claim_execution(source):
    assert lifecycle.shell_actions(source) == []


@pytest.mark.parametrize("source", [
    "sed '1, s/x/docker compose up/e' input.txt",
    "sed 's/x/docker compose up' input.txt",
    "sed '? docker compose up' input.txt",
    "sed 'q docker compose up' input.txt",
    "sed 'v docker compose up' input.txt",
])
def test_unclassifiable_sed_program_with_compose_fails_closed(source):
    assert lifecycle.shell_actions(source) == ["unknown"]


@pytest.mark.parametrize("source", [
    "sed '1, s/x/ordinary data/e' input.txt",
    "sed 's/x/unterminated' input.txt",
    "sed '? ordinary data' input.txt",
])
def test_unclassifiable_sed_without_compose_does_not_claim_lifecycle(source):
    assert lifecycle.shell_actions(source) == []


@pytest.mark.parametrize("source", [
    'echo harmless; docker compose up', 'echo harmless\ndocker compose up',
    'printf "%s" harmless | docker compose up',
    'docker compose --dry-run=false up',
    'env -u FOO timeout 5 sh -ec "docker compose up"',
    'do"cker" compose up', 'docker com\\pose up',
])
def test_command_heads_and_controls_are_not_data(source):
    assert lifecycle.shell_actions(source) == ['up']


@pytest.mark.parametrize("source", [
    '_compose("down")',
    'server._compose("start", "aitelier")',
    'subprocess.run(["docker", "compose", "up", "-d"])',
    'subprocess.run(["docker", "compose",\n "up", "-d"])',
    'from subprocess import Popen as launch\nlaunch(["docker-compose", "pause"])',
    'import subprocess as sp\ncmd = ["docker", "compose"] + ["stop"]\nsp.run(cmd)',
    'cmd = ["docker", "compose"]\nsubprocess.check_call([*cmd, "scale", "aitelier=2"])',
    'subprocess.run("docker compose kill", shell=True)',
    'asyncio.create_subprocess_exec("docker", "compose", "watch")',
])
def test_python_call_structure_detects_lifecycle(source):
    assert lifecycle.unguarded_findings("scripts/probe.py", source)


@pytest.mark.parametrize("source", [
    'print("docker compose up")', 'subprocess.run(["echo", "docker compose up"])',
    'text = ["docker", "compose", "up"]',
    'subprocess.run("docker compose up", shell=False)',
    'subprocess.run(["docker", "compose", "--dry-run", "up"])',
    '"""docker compose up"""',
])
def test_python_data_is_not_execution(source):
    assert lifecycle.python_findings(source) == []


@pytest.mark.parametrize("source,expected", [
    ('Prose says docker compose up is unsafe.', False),
    ('```text\ndocker compose up\n```', False),
    ('```sh\ndocker compose \\\n up\n```', True),
    ('Run `docker compose start aitelier`.', True),
    ('Print `echo "docker compose up"`.', False),
    ('```python\nprint("docker compose up")\n```', False),
    ('```python\nsubprocess.run(["docker", "compose", "up"])\n```', True),
    ('```\ndocker compose start\n```', True),
    ('trailing marker\n```', False),
    ('```sh\ndocker compose start', True),
    ("```sh\necho 'docker compose up'", False),
    ('```sh\nprintf "%s" "docker compose up"\n```', False),
])
def test_markdown_executable_regions_only(source, expected):
    assert bool(lifecycle.source_findings('README.md', source)) is expected


@pytest.mark.parametrize("verb", sorted(lifecycle.LIFECYCLE_COMMANDS - {"up"}))
def test_guard_allowlist_is_exact_route_and_verb(verb):
    assert lifecycle.unguarded_findings('cli/server.py', f'def restart_server():\n _compose("{verb}")')
    assert lifecycle.unguarded_findings('cli/server.py', f'def unrelated():\n _compose("up")')
    assert lifecycle.unguarded_findings('other/server.py', f'def restart_server():\n _compose("up")')
    assert lifecycle.unguarded_findings('cli/server.py', 'def restart_server():\n _require_deployment_authority(clearance)\n _compose("up", capability=token)') == []



def test_raw_subprocess_cannot_borrow_guarded_helper_allowlist():
    source = 'def restart_server():\n subprocess.run(["docker", "compose", "up"])'
    assert lifecycle.unguarded_findings('cli/server.py', source)


def test_lifecycle_contract_verbs_are_complete_independent_of_implementation():
    required = {"create", "start", "run", "up", "down", "restart", "stop", "kill", "rm", "pause", "unpause", "scale", "watch", "exec", "cp"}
    assert lifecycle.LIFECYCLE_COMMANDS == required


@pytest.mark.parametrize('value', [True, False, 'True', 'true', 'false', '1', '', None, 0])
def test_oneoff_label_must_be_exact_resident_false(inventories, value):
    inventories[1][IDS['zvec-grep']]['Config']['Labels']['com.docker.compose.oneoff'] = value
    assert server._guarded_service_errors()


def test_missing_oneoff_label_is_unknown(inventories):
    del inventories[1][IDS['zvec-grep']]['Config']['Labels']['com.docker.compose.oneoff']
    assert server._guarded_service_errors()


@pytest.mark.parametrize('source,expected', [
    ('eval "docker compose up -d"', ['up']),
    ('time docker compose restart aitelier', ['restart']),
    ('{ docker compose stop; }', ['stop']),
    ('(docker compose pause)', ['pause']),
    ('busybox sh -ec "docker compose start"', ['start']),
    ('>deploy.log docker compose down', ['down']),
    ('docker --unknown compose kill', ['unknown']),
    ('docker compose --future-option up', ['unknown']),
    ('echo "$(docker compose stop)"', ['stop']),
    ('cat <(docker compose restart)', ['restart']),
    ('if true; then\n docker compose up\nfi', ['up']),
    ('docker compose exec aitelier kill -TERM 1', ['exec']),
    ('docker compose cp payload aitelier:/app/core/authority.py', ['cp']),
    ('eval "$COMMAND"', ['unknown']),
    ('cat <<\'DATA\'\ndocker compose up\nDATA', []),
    ('printf "%s" "{ docker compose restart; }"', []),
    ('echo "$(printf docker) compose up"', []),
    ('busybox sh -c \'echo "docker compose up"\'', []),
    ('eval \'printf "%s" "docker compose up"\'', []),
    ('docker compose --dry-run exec aitelier kill -TERM 1', []),
])
def test_bash_syntax_execution_boundaries(source, expected):
    assert lifecycle.shell_actions(source) == expected


@pytest.mark.parametrize('source,expected', [
    ('subprocess.getoutput("docker compose up")', True),
    ('subprocess.getstatusoutput("docker compose restart")', True),
    ('subprocess.run("docker compose stop", shell=1)', True),
    ('subprocess.run("docker compose stop", shell=0)', False),
    ('subprocess.getoutput("echo docker compose up")', False),
    ('from subprocess import getoutput as launch\nlaunch("docker compose up")', True),
])
def test_python_shell_api_boundaries(source, expected):
    assert bool(lifecycle.unguarded_findings('scripts/x.py', source)) is expected


@pytest.mark.parametrize('source,expected', [
    ('    docker compose up -d\n', True),
    ('>     docker compose restart\n', True),
    ('    echo "docker compose up"\n', False),
    ('```text\ndocker compose up\n```', False),
    ('Prose mentions docker compose restart without a command literal.', False),
])
def test_commonmark_execution_boundaries(source, expected):
    assert bool(lifecycle.source_findings('README.md', source)) is expected


@pytest.mark.parametrize('source', [
    'builtin eval "docker compose up"',
    'xargs docker compose restart',
    'xargs -I {} -- docker compose restart {}',
    'find . -exec docker compose stop ;',
    'find . -execdir docker compose stop {} +',
    'python -c "import os; os.system(\'docker compose up\')"',
    'python3.12 -Ic "import subprocess; subprocess.getoutput(\'docker compose up\')"',
    'unmodelled-launcher "docker compose up"',
    'unmodelled-launcher docker --context review compose up',
    'xargs --unknown-option future docker compose up',
    'fish -c "docker compose up"',
])
def test_r25_literal_executors_and_unknown_routes_are_refused(source):
    assert lifecycle.shell_actions(source)


@pytest.mark.parametrize('source', [
    'import os\nos.execvp("docker", ["docker","compose","up"])',
    'import os\nos.spawnvp(os.P_WAIT,"docker",["docker","compose","restart"])',
    'os.execlp("docker", "docker", "compose", "up")',
    'os.execlp("docker", "alias", "compose", "up")',
    'os.spawnlp(os.P_WAIT, "docker", "alias", "compose", "up")',
    'os.spawnlp(os.P_WAIT, "docker", "docker", "compose", "up")',
    '@_compose("up")\ndef harmless():\n pass',
    'def harmless(value=_compose("up")):\n pass',
    '@subprocess.run(["docker","compose","up"])\ndef harmless():\n pass',
    'def harmless(value=subprocess.run(["docker","compose","restart"])):\n pass',
    '@subprocess.getoutput("docker compose stop")\ndef harmless():\n pass',
    'def harmless(*, value=os.system("docker compose up")):\n pass',
    'def harmless(value: os.system("docker compose up")):\n pass',
    'def harmless() -> os.system("docker compose up"):\n pass',
    'class C(os.system("docker compose up")):\n pass',
    'launch=subprocess.run\nlaunch(["docker","compose","up"])',
    'unmodelled_launch("docker compose up")',
    'unmodelled_launch(command="docker compose up")',
    'os.execvp(file="docker", args=["alias","compose","up"])',
    'subprocess.run(["alias","compose","up"], executable="docker")',
])
def test_r25_python_definition_and_exec_surfaces(source):
    assert lifecycle.unguarded_findings('scripts/x.py', source)


@pytest.mark.parametrize('body', [
    'callback=lambda: _compose("up", capability=token)',
    'callback=lambda arg=_compose("up", capability=token): None',
    'callback=(_compose("up", capability=token) for _ in [1])',
    'callback=[_compose("up", capability=token) for _ in [1]]',
    'def nested(arg=_compose("up", capability=token)):\n  pass',
])
def test_nested_expressions_do_not_inherit_dispatch_exception(body):
    source = 'def restart_server():\n _require_deployment_authority(clearance)\n ' + body + '\n'
    assert lifecycle.unguarded_findings('cli/server.py', source)


@pytest.mark.parametrize('source', [
    '```fish\ndocker compose up\n```',
    '\tpython -c "import os; os.system(\'docker compose up\')"\n',
    '    xargs docker compose stop\n',
])
def test_r25_markdown_executable_regions(source):
    assert lifecycle.source_findings('README.md', source)


@pytest.mark.parametrize('source', [
    'builtin printf "%s" "docker compose up"',
    'python -c "print(\'docker compose up\')"',
    'fish -c \'echo "docker compose up"\'',
    'xargs docker compose --dry-run up',
    'find . -exec docker compose --dry-run up ;',
    'echo "docker compose up"',
    "cat <<'EOF'\ndocker compose up\nEOF",
])
def test_known_data_and_dryrun_remain_safe(source):
    assert lifecycle.shell_actions(source) == []


@pytest.mark.parametrize('source', [
    "cat 'docker compose up'",
    "grep 'docker compose restart' README.md",
    "sed 's/docker compose up/safe/' README.md",
    "find . -name docker -a -name compose",
    "test -f 'docker-compose up'",
])
def test_known_nonexecuting_data_consumers_remain_safe(source):
    assert lifecycle.shell_actions(source) == []


@pytest.mark.parametrize('source', [
    "unknown-launcher 'docker compose up'",
    "unknown-launcher docker --context review compose restart",
    "find . -exec unknown-launcher 'docker compose up' ';'",
])
def test_unknown_literal_launchers_still_fail_closed(source):
    assert lifecycle.shell_actions(source) == ['unknown']
