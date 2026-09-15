"""Static audit of supported deployment entry points; never executes source.

Maintained Bash/CommonMark parsers and Python AST identify executable source
forms, including literal wrappers and local constant argv. This regression audit
does not authorize arbitrary generated programs. Internal Compose primitives
separately require live journal-backed authority at runtime; only up is supported.
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
    "pause", "unpause", "scale", "watch", "exec", "cp",
})
READ_ONLY_COMMANDS = frozenset({
    "build", "config", "convert", "events", "images", "logs",
    "ls", "port", "ps", "pull", "push", "top", "version", "wait", "help",
})
# exec/cp can replace code or terminate PID 1, so they carry lifecycle capability
# even when their command spelling does not say restart. Unknown capability is denied.
GUARDED_CALLS = {("cli/server.py", "_compose_up"): {"up"},
                 ("cli/server.py", "restart_server"): {"up"},
                 ("cli/server.py", "_ensure_docker_backend"): {"up"}}
DOCKER_VALUES = {"--config", "--context", "-c", "--host", "-H", "--log-level", "-l",
                 "--tlscacert", "--tlscert", "--tlskey"}
DOCKER_FLAGS = {"--debug", "-D", "--tls", "--tlsverify"}
COMPOSE_VALUES = {"--ansi", "--env-file", "--file", "-f", "--parallel", "--profile",
                  "--progress", "--project-directory", "--project-name", "-p"}
COMPOSE_FLAGS = {"--all-resources", "--compatibility", "--dry-run", "--verbose"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "fish"}
# Shell argv cannot contain NUL, so literal source cannot spoof this parser marker.
_DYNAMIC_COMPOSE = "\0dynamic-compose"


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


def _command_words(value):
    """Tokenize an ambiguous executable fragment without executing or reparsing it."""
    words = []
    current = []
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char in {"'", '"'}:
            # Quoting cannot make a dynamic executable field inert. Ignore the
            # quote itself while retaining word boundaries inside its content.
            continue
        elif char.isspace() or char in ";|&(){}<>?,:#":
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        words.append("".join(current))
    return words


def _has_compose_route(values):
    """Recognize exact Compose command identities in an ambiguous executable field."""
    words = []
    for value in values:
        if type(value) is str:
            words.extend(_command_words(value))
    names = [Path(word).name for word in words]
    if any(name == "docker-compose" for name in names):
        return True
    for index, name in enumerate(names):
        if name != "docker":
            continue
        command_at, _ = _options(words, index + 1, DOCKER_VALUES, DOCKER_FLAGS)
        if command_at < len(words) and words[command_at] == "compose":
            return True
        # An unmodelled executor may accept Docker options newer than this
        # inventory.  Preserve the unresolved exact command identity instead
        # of declaring it inert merely because our option grammar stopped.
        if (command_at < len(words) and words[command_at].startswith("-")
                and "compose" in words[command_at + 1:]):
            return True
    return False


def _unknown_compose_literal(argv):
    """Unmodelled executable fields carrying exact Compose identity fail closed."""
    return ["unknown"] if _has_compose_route(argv) else []


def _sed_field(program, start, delimiter):
    """Return one sed field and its end without interpreting its contents."""
    result = []
    escaped = False
    for index in range(start, len(program)):
        value = program[index]
        if escaped:
            result.extend(("\\", value))
            escaped = False
        elif value == "\\":
            escaped = True
        elif value == delimiter:
            return "".join(result), index + 1
        else:
            result.append(value)
    return None, len(program)


def _sed_address_end(program, start):
    """Return an address end, start for no address, or None if malformed."""
    if start >= len(program):
        return start
    value = program[start]
    if value.isdigit():
        end = start + 1
        while end < len(program) and program[end].isdigit():
            end += 1
        if end < len(program) and program[end] == "~":
            end += 1
            number = end
            while end < len(program) and program[end].isdigit():
                end += 1
            if end == number:
                return None
        return end
    if value == "$":
        return start + 1
    if value in {"+", "~"}:
        end = start + 1
        while end < len(program) and program[end].isdigit():
            end += 1
        return end if end > start + 1 else start
    if value == "/":
        _, end = _sed_field(program, start + 1, value)
        return end if end <= len(program) and program[end - 1:end] == value else None
    if value == "\\" and start + 1 < len(program):
        delimiter = program[start + 1]
        _, end = _sed_field(program, start + 2, delimiter)
        return end if end <= len(program) and program[end - 1:end] == delimiter else None
    return start


def _sed_skip_addresses(program, start):
    end = _sed_address_end(program, start)
    if end is None or end == start:
        return end
    while end < len(program) and program[end] in " \t":
        end += 1
    if end < len(program) and program[end] == ",":
        end += 1
        while end < len(program) and program[end] in " \t":
            end += 1
        second = _sed_address_end(program, end)
        if second is None or second == end:
            return None
        end = second
    while end < len(program) and program[end] in " \t":
        end += 1
    return end


def _sed_boundary(program, start, *, line_only=False):
    positions = [position for position in (program.find("\n", start),)
                 if position >= 0]
    if not line_only:
        positions.extend(position for position in (program.find(";", start),)
                         if position >= 0)
    return min(positions, default=len(program))


def _sed_replacement_for_shell(replacement, delimiter):
    """Undo only escapes required to embed the active delimiter in a field."""
    return replacement.replace("\\" + delimiter, delimiter)


def _sed_program_actions(program):
    """Walk literal GNU sed commands without scanning regex/replacement data."""
    if program == _DYNAMIC_COMPOSE:
        return ["unknown"]
    if type(program) is not str or program == "<dynamic>":
        return []
    actions = []

    def ambiguous(*fields):
        if _has_compose_route(fields) and "unknown" not in actions:
            return [*actions, "unknown"]
        return list(actions)

    index = 0
    while index < len(program):
        while index < len(program) and program[index] in " \t;\n":
            index += 1
        if index >= len(program):
            break
        if program[index] == "#":
            index = _sed_boundary(program, index, line_only=True) + 1
            continue

        command_at = _sed_skip_addresses(program, index)
        if command_at is None:
            return ambiguous(program[index:])
        index = command_at
        while index < len(program) and program[index] in " \t":
            index += 1
        if index < len(program) and program[index] == "!":
            index += 1
            while index < len(program) and program[index] in " \t":
                index += 1
        if index >= len(program):
            return ambiguous(program)

        command = program[index]
        index += 1
        if command in "{}":
            continue
        if command == "e":
            end = _sed_boundary(program, index, line_only=True)
            shell = program[index:end].lstrip()
            actions.extend(shell_actions(shell) if shell else ["unknown"])
            index = end + 1
            continue
        if command == "s":
            if index >= len(program) or program[index] in "\\\n":
                return ambiguous(program[index:])
            delimiter = program[index]
            pattern, cursor = _sed_field(program, index + 1, delimiter)
            replacement, cursor = _sed_field(program, cursor, delimiter)
            if pattern is None or replacement is None:
                return ambiguous(program[index + 1:])
            execute = False
            while cursor < len(program):
                while cursor < len(program) and program[cursor] in " \t":
                    cursor += 1
                if cursor >= len(program) or program[cursor] in ";\n":
                    break
                if program[cursor].isdigit():
                    while cursor < len(program) and program[cursor].isdigit():
                        cursor += 1
                    continue
                flag = program[cursor]
                cursor += 1
                if flag in "gIpimM":
                    continue
                if flag == "e":
                    execute = True
                    continue
                if flag == "w":
                    # GNU sed consumes a w filename through physical newline,
                    # including semicolons and without requiring whitespace.
                    cursor = _sed_boundary(program, cursor, line_only=True)
                    break
                return ambiguous(replacement, program[cursor - 1:])
            if execute:
                actions.extend(shell_actions(
                    _sed_replacement_for_shell(replacement, delimiter)))
            if cursor < len(program) and program[cursor] == ";":
                index = cursor + 1
            elif cursor < len(program) and program[cursor] == "\n":
                index = cursor + 1
            else:
                index = cursor
            continue
        if command == "y":
            if index >= len(program) or program[index] in "\\\n":
                return ambiguous(program[index:])
            delimiter = program[index]
            source, cursor = _sed_field(program, index + 1, delimiter)
            target, cursor = _sed_field(program, cursor, delimiter)
            if source is None or target is None:
                return ambiguous(program[index + 1:])
            index = _sed_boundary(program, cursor) + 1
            continue
        if command in "aci":
            # Text commands consume data through the physical line.
            index = _sed_boundary(program, index, line_only=True) + 1
            continue
        if command in "rRwW":
            # GNU file operands consume through physical newline. A semicolon
            # is part of the filename, never a new sed command.
            index = _sed_boundary(program, index, line_only=True) + 1
            continue
        if command in ":btT":
            # Labels end at a sed command separator.
            index = _sed_boundary(program, index) + 1
            continue
        if command == "v":
            end = _sed_boundary(program, index)
            version = program[index:end].strip()
            if version and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version) is None:
                return ambiguous(program[index:end])
            index = end + 1
            continue
        if command in "dDgGhHlnNpPqQxz=":
            end = _sed_boundary(program, index)
            close = program.find("}", index, end)
            if close >= 0:
                end = close
            argument = program[index:end].strip()
            if argument and (command not in "lqQ" or not argument.isdigit()):
                return ambiguous(program[index:])
            index = end if end < len(program) and program[end] == "}" else end + 1
            continue
        return ambiguous(program[index - 1:])
    return actions


def _sed_actions(argv):
    programs = []
    index = 0
    explicit = False
    while index < len(argv):
        value = argv[index]
        if value in {"--help", "--version"}:
            return []
        if value == "--":
            index += 1
            if not explicit and index < len(argv):
                programs.append(argv[index])
            break
        if value in {"-e", "--expression"}:
            if index + 1 >= len(argv):
                return ["unknown"]
            programs.append(argv[index + 1])
            explicit = True
            index += 2
            continue
        if value.startswith("--expression="):
            programs.append(value.split("=", 1)[1])
            explicit = True
            index += 1
            continue
        if value in {"-f", "--file"}:
            if index + 1 >= len(argv):
                return ["unknown"]
            if argv[index + 1] == _DYNAMIC_COMPOSE:
                return ["unknown"]
            explicit = True
            index += 2
            continue
        if value.startswith("--file="):
            if (_DYNAMIC_COMPOSE in value
                    or index + 1 < len(argv) and argv[index + 1] == _DYNAMIC_COMPOSE):
                return ["unknown"]
            explicit = True
            index += 1
            continue
        if value in {"-l", "--line-length"}:
            if index + 1 >= len(argv) or not argv[index + 1].isdigit():
                return _unknown_compose_literal(["sed", *argv])
            index += 2
            continue
        if value.startswith("--line-length="):
            if not value.split("=", 1)[1].isdigit():
                return _unknown_compose_literal(["sed", *argv])
            index += 1
            continue
        if (value in {"-n", "--quiet", "--silent", "-E", "-r",
                      "--regexp-extended", "-s", "--separate", "-u",
                      "--unbuffered", "-z", "--null-data", "--sandbox",
                      "--debug", "--posix", "--binary", "--follow-symlinks",
                      "-b"}
                or value == "-i" or value.startswith("-i")
                or value.startswith("--in-place")):
            index += 1
            continue
        if value.startswith("-") and not value.startswith("--"):
            cluster = value[1:]
            position = 0
            valid = True
            while position < len(cluster):
                option = cluster[position]
                if option in "bnErsuz":
                    position += 1
                    continue
                if option == "l":
                    argument = cluster[position + 1:]
                    if not argument:
                        if index + 1 >= len(argv):
                            return _unknown_compose_literal(["sed", *argv])
                        index += 1
                        argument = argv[index]
                    if not argument.isdigit():
                        return _unknown_compose_literal(["sed", *argv])
                    position = len(cluster)
                    continue
                if option == "i":
                    position = len(cluster)  # the remainder is its backup suffix
                    continue
                if option in "ef":
                    argument = cluster[position + 1:]
                    if not argument:
                        if index + 1 >= len(argv):
                            return ["unknown"]
                        index += 1
                        argument = argv[index]
                    if option == "e":
                        programs.append(argument)
                    elif argument == _DYNAMIC_COMPOSE:
                        return ["unknown"]
                    explicit = True
                    position = len(cluster)
                    continue
                valid = False
                break
            if valid:
                index += 1
                continue
        if value.startswith("-"):
            return _unknown_compose_literal(["sed", *argv])
        if not explicit:
            programs.append(value)
        break
    actions = []
    for program in programs:
        actions.extend(_sed_program_actions(program))
    return actions


def _command_actions(argv):
    if not argv:
        return []
    while argv and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]) or argv[0] in {"!", "then", "do", "else", "if", "elif"}):
        argv = argv[1:]
    if not argv:
        return []
    head = Path(argv[0]).name
    rest = argv[1:]
    if head in {"echo", "printf", "true", "false", ":", "cat", "grep",
                "egrep", "fgrep", "test", "[", "[["}:
        return []
    if head == "sed":
        return _sed_actions(rest)
    if head == "eval":
        return ["unknown"] if "<dynamic>" in rest else shell_actions(" ".join(rest))
    if head == "busybox":
        return _command_actions(rest)
    if head == "time":
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in {"-f", "--format", "-o", "--output"} else rest[1:]
        return _command_actions(rest)
    if head in {"command", "builtin", "exec", "nohup"}:
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
    if head == "xargs":
        while rest and rest[0].startswith("-"):
            option = rest[0]
            rest = rest[2:] if option in {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter", "--eof", "--replace", "--max-lines", "--max-args", "--max-procs", "--max-chars"} else rest[1:]
            if option == "--":
                break
        return _command_actions(rest)
    if head == "find":
        actions = []
        for index, value in enumerate(rest):
            if value in {"-exec", "-execdir", "-ok", "-okdir"}:
                command = rest[index + 1:]
                end = next((i for i, arg in enumerate(command) if arg in {";", "+"}), len(command))
                actions.extend(_command_actions(command[:end]))
        return actions
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", head):
        for index, option in enumerate(rest):
            if option == "-c" or (option.startswith("-") and not option.startswith("--") and option.endswith("c")):
                if index + 1 == len(rest):
                    return ["unknown"]
                try:
                    return [item.action for item in python_findings(rest[index + 1])]
                except SyntaxError:
                    return _unknown_compose_literal(argv)
        return _unknown_compose_literal(argv)
    if head == "docker":
        index, _ = _options(rest, 0, DOCKER_VALUES, DOCKER_FLAGS)
        if index >= len(rest) or rest[index] != "compose":
            return ["unknown"] if "compose" in rest[index:] and rest[index].startswith("-") else []
        rest = rest[index + 1:]
    elif head != "docker-compose":
        return _unknown_compose_literal(argv)
    action = compose_action(rest)
    return [action] if action else []


def shell_actions(source):
    """Visit executable Bash syntax nodes, never re-lex whole source as words."""
    from tree_sitter import Language, Parser
    import tree_sitter_bash
    root = Parser(Language(tree_sitter_bash.language())).parse(source.encode()).root_node
    actions = []

    def literal(node):
        dynamic = {"command_substitution", "process_substitution", "expansion",
                   "simple_expansion"}
        if node.type in dynamic or any(child.type in dynamic
                                       for child in descendants(node)):
            raw = node.text.decode()
            return (_DYNAMIC_COMPOSE if _unknown_compose_literal([raw])
                    else "<dynamic>")
        try:
            words = shlex.split(node.text.decode().replace("\\\n", ""))
            return words[0] if len(words) == 1 else "<dynamic>"
        except ValueError:
            return "<dynamic>"

    def descendants(node):
        for child in node.named_children:
            yield child
            yield from descendants(child)

    for node in [root, *descendants(root)]:
        if node.type == "command":
            name = node.child_by_field_name("name")
            if name is not None:
                arguments = list(node.children_by_field_name("argument"))
                arguments.extend(
                    child for child in node.named_children
                    if child.type in {"command_substitution", "process_substitution"}
                    and child not in arguments)
                arguments.sort(key=lambda child: child.start_byte)
                argv = [literal(name), *(literal(arg) for arg in arguments)]
                actions.extend(_command_actions(argv))
        elif node.type == "ERROR" and ("docker" in node.text.decode() or "_compose" in node.text.decode()):
            actions.append("unknown")
    return actions


@dataclass(frozen=True)
class Finding:
    line: int
    action: str
    function: str = ""
    sink: str = ""
    guarded: bool = False


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
            self.bindings, self.function, self.guard_line = {}, "", None

        def visit_ClassDef(self, node):
            prior = self.function
            self.function = prior + "." + node.name if prior else node.name
            self.generic_visit(node)
            self.function = prior

        def visit_FunctionDef(self, node):
            old, prior, guard = self.bindings, self.function, self.guard_line
            # Decorators/defaults/annotations execute at definition time. Give
            # them no enclosing function's dispatch exception.
            self.function = (prior + "." if prior else "") + "<definition:" + node.name + ">"
            self.guard_line = None
            for expression in [*node.decorator_list, node.args, node.returns, *node.type_params]:
                if expression is not None:
                    self.visit(expression)
            self.bindings = dict(old)
            self.function = prior + "." + node.name if prior else node.name
            self.guard_line = None
            for statement in node.body:
                candidate = statement.value if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign)) else None
                if isinstance(candidate, ast.Call) and name(candidate.func) in {"_require_deployment_authority", "_require_deployment_clearance"}:
                    self.guard_line = statement.lineno
                self.visit(statement)
            self.bindings, self.function, self.guard_line = old, prior, guard

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            prior, guard = self.function, self.guard_line
            self.function = prior + ".<lambda>"
            self.guard_line = None
            self.visit(node.args)
            self.visit(node.body)
            self.function, self.guard_line = prior, guard

        def visit_GeneratorExp(self, node):
            prior, guard = self.function, self.guard_line
            self.function = prior + ".<comprehension>"
            self.guard_line = None
            self.generic_visit(node)
            self.function, self.guard_line = prior, guard

        visit_ListComp = visit_GeneratorExp
        visit_SetComp = visit_GeneratorExp
        visit_DictComp = visit_GeneratorExp

        def visit_Assign(self, node):
            value = _literal(node.value, self.bindings)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.bindings[target.id] = value
                    if isinstance(node.value, (ast.Name, ast.Attribute)):
                        aliases[target.id] = name(node.value)
            self.generic_visit(node)

        def visit_Call(self, node):
            called = name(node.func)
            actions = []
            if called.split(".")[-1] == "_compose_up":
                actions = ["up"]
            elif called.split(".")[-1] == "_compose":
                argv = _literal(ast.List(elts=node.args), self.bindings)
                action = compose_action(argv)
                actions = [action] if action else []
            elif called in {"os.execv", "os.execve", "os.execvp", "os.execvpe", "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe"}:
                index = 2 if called.startswith("os.spawn") else 1
                argument = node.args[index] if len(node.args) > index else next((k.value for k in node.keywords if k.arg in {"args", "argv"}), None)
                argv = _literal(argument, self.bindings)
                executable = node.args[index - 1] if len(node.args) >= index else next((k.value for k in node.keywords if k.arg in {"file", "path"}), None)
                target = _literal(executable, self.bindings)
                if isinstance(argv, list) and argv and isinstance(target, str):
                    argv = [target, *argv[1:]]
                actions = _command_actions(argv) if isinstance(argv, list) else []
            elif called in {"os.execl", "os.execle", "os.execlp", "os.execlpe", "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe"}:
                index = 2 if called.startswith("os.spawn") else 1
                argv = _literal(ast.List(elts=node.args[index:]), self.bindings)
                target = _literal(node.args[index - 1], self.bindings) if len(node.args) >= index else None
                if argv and isinstance(target, str):
                    argv = [target, *argv[1:]]
                actions = _command_actions(argv)
            elif called in {"subprocess.run", "subprocess.Popen", "subprocess.getoutput", "subprocess.getstatusoutput", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "os.system", "os.popen", "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell"}:
                arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in {"args", "command", "cmd"}), None)
                value = _literal(arg, self.bindings)
                if called == "asyncio.create_subprocess_exec":
                    value = _literal(ast.List(elts=node.args), self.bindings)
                if isinstance(value, list):
                    executable = next((_literal(k.value, self.bindings) for k in node.keywords if k.arg == "executable"), None)
                    if value and isinstance(executable, str):
                        value = [executable, *value[1:]]
                    actions = _command_actions(value)
                elif isinstance(value, str):
                    # Python's shell=False string is an executable filename;
                    # only shell=True/os.system/shell API interprets shell text.
                    shell = called in {"os.system", "os.popen", "subprocess.getoutput", "subprocess.getstatusoutput", "asyncio.create_subprocess_shell"} or any(k.arg == "shell" and isinstance(k.value, ast.Constant) and bool(k.value.value) for k in node.keywords)
                    if shell:
                        actions = shell_actions(value)
            elif called not in {"print", "str", "repr", "bytes"}:
                # Unknown callees with a literal Compose command are executable
                # source, not established data sinks. Require explicit review.
                for argument in [*node.args, *(k.value for k in node.keywords)]:
                    value = _literal(argument, self.bindings)
                    if isinstance(value, str):
                        actions.extend(_unknown_compose_literal([value]))
                    elif isinstance(value, list):
                        actions.extend(_unknown_compose_literal(value))
            if called == "subprocess.run" and self.function == "_compose" and not actions:
                actions = ["unknown"]
            for action in actions:
                findings.append(Finding(node.lineno, action, self.function, called.split(".")[-1] if called.split(".")[-1] in {"_compose", "_compose_up"} else called,
                                        (self.guard_line is not None and self.guard_line < node.lineno
                                         and any(k.arg == "capability" for k in node.keywords))))
            self.generic_visit(node)

    Visitor().visit(tree)
    return findings


def _fenced_findings(language, block, first):
    text = "\n".join(block)
    if language in {"", "sh", "shell", "bash", "zsh", "fish", "console", "shell-session"}:
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
    """Use the language parser for code, fences and CommonMark indented blocks."""
    suffix = Path(path).suffix
    if suffix == ".py":
        return python_findings(source)
    if suffix in {".sh", ".bash", ".zsh"} or source.startswith("#!/bin/sh") or source.startswith("#!/usr/bin/env bash"):
        return [Finding(1, action) for action in shell_actions(source)]
    if suffix != ".md":
        return []
    from markdown_it import MarkdownIt
    findings = []
    for token in MarkdownIt("commonmark").parse(source):
        first = (token.map or [0])[0] + 1
        if token.type in {"fence", "code_block"}:
            language = token.info.strip().split()[0] if token.info.strip() else ""
            findings.extend(_fenced_findings(language, token.content.splitlines(), first))
        elif token.type == "inline":
            for child in token.children or []:
                if child.type == "code_inline":
                    findings.extend(Finding(first, action) for action in shell_actions(child.content))
    return findings


def unguarded_findings(path, source):
    findings = source_findings(path, source)
    result = []
    for finding in findings:
        allowed = (finding.guarded and finding.sink in {"_compose", "_compose_up"}
                   and finding.action in GUARDED_CALLS.get((str(path), finding.function), set()))
        # The forwarding helper has no ambient guard; its sole authority is
        # the explicit keyword-only capability passed into the raw dispatcher.
        if str(path) == "cli/server.py" and finding.function == "_compose_up" and finding.sink == "_compose":
            function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "_compose_up")
            allowed = (finding.action in {"up", "unknown"}
                       and any(arg.arg == "capability" for arg in function.args.kwonlyargs)
                       and any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_compose"
                               and any(k.arg == "capability" and isinstance(k.value, ast.Name) and k.value.id == "capability" for k in node.keywords)
                               for node in ast.walk(function)))
        # The single raw dispatcher performs the same runtime capability check
        # before subprocess.run. It is not a wildcard for nested same-name code.
        dispatcher = (str(path) == "cli/server.py" and finding.function == "_compose"
                      and finding.sink == "subprocess.run" and finding.action == "unknown"
                      and any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                              and node.func.id == "_consume_deployment_command"
                              for node in ast.walk(next(node for node in ast.parse(source).body
                                  if isinstance(node, ast.FunctionDef) and node.name == "_compose"))))
        if not allowed and not dispatcher:
            result.append(finding)
    return result
