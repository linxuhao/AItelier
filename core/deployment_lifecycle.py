"""Static audit of supported deployment entry points; never executes source.

The contract covers literal shell commands and Python argv (including local
constant bindings), not arbitrary generated programs. Unknown Compose commands
are refused. Only the two reviewed lifecycle call sites may dispatch mutation;
behavioral gate tests separately prove their clearance precedes dispatch.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import textwrap

LIFECYCLE_COMMANDS = frozenset({
    "create", "start", "run", "up", "down", "restart", "stop", "kill", "rm",
    "pause", "unpause", "scale", "watch",
})
READ_ONLY_COMMANDS = frozenset({
    "build", "config", "convert", "cp", "events", "exec", "images", "logs",
    "ls", "port", "ps", "pull", "push", "top", "version", "wait", "help",
})
# These commands do not mutate container lifecycle (exec/cp still have other
# effects). This is a deployment audit, not a general Docker permissions policy.
GUARDED_CALLS = {("cli/server.py", "_compose_up"): {"up"},
                 ("cli/server.py", "restart_server"): {"up"}}
DOCKER_VALUES = {"--config", "--context", "-c", "--host", "-H", "--log-level", "-l",
                 "--tlscacert", "--tlscert", "--tlskey"}
DOCKER_FLAGS = {"--debug", "-D", "--tls", "--tlsverify"}
COMPOSE_VALUES = {"--ansi", "--env-file", "--file", "-f", "--parallel", "--profile",
                  "--progress", "--project-directory", "--project-name", "-p"}
COMPOSE_FLAGS = {"--all-resources", "--compatibility", "--dry-run", "--verbose"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}


def _options(argv, index, values, flags):
    dry_run = False
    while index < len(argv):
        value = argv[index]
        if value == "--":
            return index + 1, dry_run
        if value == "--dry-run" or value == "--dry-run=true":
            dry_run = True
            index += 1
        elif value == "--dry-run=false":
            dry_run = False
            index += 1
        elif value in flags or (value.startswith("--") and value.split("=", 1)[0] in flags):
            index += 1
        elif value in values:
            index += 2
        elif any(value.startswith(option + "=") for option in values if option.startswith("--")):
            index += 1
        elif any(value.startswith(option) and value != option for option in values if len(option) == 2):
            index += 1
        else:
            return index, dry_run
    return index, dry_run


def compose_action(argv):
    """Return a denied verb/unknown, or None for non-lifecycle/dry-run argv."""
    index, dry_run = _options(argv, 0, COMPOSE_VALUES, COMPOSE_FLAGS)
    if index == len(argv):
        return None
    if index > len(argv):
        return "unknown"
    verb = argv[index]
    if verb in {"--help", "-h", "--version"}:
        return None
    if dry_run:
        return None
    if verb in LIFECYCLE_COMMANDS:
        return verb
    return None if verb in READ_ONLY_COMMANDS else "unknown"


def _command_actions(argv):
    if not argv:
        return []
    while argv and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]) or argv[0] in {"!", "then", "do", "else", "if", "elif"}):
        argv = argv[1:]
    if not argv:
        return []
    head = Path(argv[0]).name
    rest = argv[1:]
    if head in {"echo", "printf", "true", "false", ":"}:
        return []
    if head in {"command", "exec", "nohup"}:
        if head == "command" and any(value in {"-v", "-V"} for value in rest[:1]):
            return []
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if head == "exec" and rest[0] == "-a" else rest[1:]
        return _command_actions(rest)
    if head == "env":
        while rest and (rest[0].startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", rest[0])):
            if rest[0] in {"-u", "--unset", "-C", "--chdir"}:
                rest = rest[2:]
            elif rest[0] in {"-S", "--split-string"}:
                return shell_actions(rest[1]) if len(rest) > 1 else ["unknown"]
            else:
                rest = rest[1:]
        return _command_actions(rest)
    if head == "sudo":
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in {"-u", "-g", "-h", "-p", "-C", "-T", "--user", "--group"} else rest[1:]
        return _command_actions(rest)
    if head == "nice":
        if rest[:1] in (["-n"], ["--adjustment"]):
            rest = rest[2:]
        elif rest and (re.fullmatch(r"-\d+", rest[0]) or rest[0].startswith("--adjustment=")):
            rest = rest[1:]
        return _command_actions(rest)
    if head == "timeout":
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in {"-s", "--signal", "-k", "--kill-after"} else rest[1:]
        return _command_actions(rest[1:])
    if head in SHELLS:
        index = 0
        while index < len(rest):
            value = rest[index]
            if value == "--" or not value.startswith(("-", "+")):
                break
            if value in {"-o", "+o", "-O", "+O"}:
                index += 2
                continue
            if value.startswith("-") and not value.startswith("--") and "c" in value[1:]:
                return shell_actions(rest[index + 1]) if index + 1 < len(rest) else ["unknown"]
            index += 1
        return []
    if head == "docker":
        index, _ = _options(rest, 0, DOCKER_VALUES, DOCKER_FLAGS)
        if index >= len(rest) or rest[index] != "compose":
            return []
        rest = rest[index + 1:]
    elif head != "docker-compose":
        return []
    action = compose_action(rest)
    return [action] if action else []


def shell_actions(source):
    # Whole-block POSIX lexing handles quoted words, escapes and continuations.
    # Protect literal punctuation before shlex removes quoting; otherwise a
    # quoted semicolon becomes indistinguishable from a command separator.
    source = source.replace("\\\n", "")
    source = re.sub(r"(['\"])([;&|()]+)\1", lambda m: m[1] + "\ue000" + m[2] + m[1], source)
    source = re.sub(r"\\([;&|()])", lambda m: "\ue000" + m[1], source)
    lexer = shlex.shlex(source, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    actions, command = [], []
    try:
        for token in lexer:
            if token and all(c in ";&|()\n" for c in token):
                actions.extend(_command_actions(command))
                command = []
                continue
            command.append(token.removeprefix("\ue000"))
        actions.extend(_command_actions(command))
    except ValueError:
        # A malformed executable block mentioning the protected dispatcher is
        # unauditable, never silently treated as a reviewed lifecycle route.
        if "docker" in source or "_compose" in source:
            actions.append("unknown")
    return actions


@dataclass(frozen=True)
class Finding:
    line: int
    action: str
    function: str = ""
    sink: str = ""


def _literal(node, bindings):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, (ast.List, ast.Tuple)):
        result = []
        for element in node.elts:
            value = _literal(element.value if isinstance(element, ast.Starred) else element, bindings)
            if isinstance(element, ast.Starred):
                result.extend(value if isinstance(value, list) else ["<dynamic>"])
            else:
                result.append(value if isinstance(value, str) else "<dynamic>")
        return result
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left, bindings), _literal(node.right, bindings)
        if type(left) is type(right) and isinstance(left, (str, list)):
            return left + right
    return None


def python_findings(source):
    findings = []
    tree = ast.parse(source)
    aliases = {"subprocess": "subprocess", "os": "os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = node.module + "." + alias.name

    def name(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return name(node.value) + "." + node.attr
        return ""

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.bindings, self.function = {}, ""

        def visit_FunctionDef(self, node):
            old, prior = self.bindings, self.function
            self.bindings, self.function = dict(old), node.name
            for statement in node.body:
                self.visit(statement)
            self.bindings, self.function = old, prior

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node):
            value = _literal(node.value, self.bindings)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.bindings[target.id] = value
            self.generic_visit(node)

        def visit_Call(self, node):
            called = name(node.func)
            actions = []
            if called.split(".")[-1] == "_compose":
                argv = _literal(ast.List(elts=node.args), self.bindings)
                action = compose_action(argv)
                actions = [action] if action else []
            elif called in {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "os.system", "os.popen", "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell"}:
                arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in {"args", "command", "cmd"}), None)
                value = _literal(arg, self.bindings)
                if called == "asyncio.create_subprocess_exec":
                    value = _literal(ast.List(elts=node.args), self.bindings)
                if isinstance(value, list):
                    actions = _command_actions(value)
                elif isinstance(value, str):
                    # Python's shell=False string is an executable filename;
                    # only shell=True/os.system/shell API interprets shell text.
                    shell = called in {"os.system", "os.popen", "asyncio.create_subprocess_shell"} or any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords)
                    if shell:
                        actions = shell_actions(value)
            for action in actions:
                findings.append(Finding(node.lineno, action, self.function, "_compose" if called.split(".")[-1] == "_compose" else called))
            self.generic_visit(node)

    Visitor().visit(tree)
    return findings


def _fenced_findings(language, block, first):
    text = "\n".join(block)
    if language in {"", "sh", "shell", "bash", "zsh", "console", "shell-session"}:
        text = "\n".join(re.sub(r"^\s*\$ ", "", item) for item in block)
        return [Finding(first, action) for action in shell_actions(text)]
    if language in {"python", "py"}:
        try:
            items = python_findings(textwrap.dedent(text))
        except SyntaxError:
            items = [Finding(1, "unknown")] if "docker" in text or "_compose" in text else []
        return [Finding(first + item.line - 1, item.action, item.function, item.sink) for item in items]
    return []


def source_findings(path, source):
    """Audit executable content only; Markdown prose is never shell source."""
    suffix = Path(path).suffix
    if suffix == ".py":
        return python_findings(source)
    if suffix in {".sh", ".bash", ".zsh"} or source.startswith("#!/bin/sh") or source.startswith("#!/usr/bin/env bash"):
        return [Finding(1, action) for action in shell_actions(source)]
    if suffix != ".md":
        return []
    findings, block, fence, first = [], [], None, 0
    for number, line in enumerate(source.splitlines(), 1):
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if marker:
            if fence is None:
                fence = (marker[1][0], len(marker[1]), marker[2].strip().split()[0] if marker[2].strip() else "")
                first = number + 1
            elif marker[1][0] == fence[0] and len(marker[1]) >= fence[1]:
                findings.extend(_fenced_findings(fence[2], block, first))
                block, fence = [], None
            continue
        if fence:
            block.append(line)
        else:
            for literal in re.findall(r"`([^`\n]+)`", line):
                findings.extend(Finding(number, action) for action in shell_actions(literal))
    # CommonMark allows a fenced block to extend to EOF without a closer.
    if fence:
        findings.extend(_fenced_findings(fence[2], block, first))
    return findings


def unguarded_findings(path, source):
    return [finding for finding in source_findings(path, source)
            if not (finding.sink == "_compose" and finding.action in GUARDED_CALLS.get((str(path), finding.function), set()))
            and not (str(path) == "cli/server.py" and finding.function == "_compose"
                     and finding.sink == "subprocess.run" and finding.action == "unknown")]
