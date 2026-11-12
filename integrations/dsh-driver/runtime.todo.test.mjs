/**
 * Todo plans end to end: a real pinned DSH boots this driver with a fake model,
 * a fake MCP/State server and a todo CLI. The root lists the repository
 * templates, freezes two plans expanded from template `code_change` and
 * dispatches two native children; one walks every item, one stops after the
 * second and must be handed back by the runtime, not by its last message.
 *
 * The CLI is a minimal fake of the `core.todo_cli` contract (JSON files in a
 * temp store), reached exactly as production reaches it:
 * `$AITELIER_TODO_PYTHON -m core.todo_cli` in `$AITELIER_TODO_CWD`.
 * Set AITELIER_TODO_TEST_PYTHON and AITELIER_TODO_TEST_CWD to run the same
 * scenario against a real checkout instead.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm, stat } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutCallerMode } from './dsh-entry.mjs';
import { DriverRuntime, TodoCli, todoSummary, unfinishedWithoutChild, stateInjectionSource, runtimeEventSource, todoBriefSource, driverGoal } from './runtime.mjs';
import { watchProgress } from './progress.mjs';

const FAKE_CLI = String.raw`"""Fake core.todo_cli for runtime.todo.test.mjs: the CLI contract, minimally."""
import argparse, hashlib, json, os, signal, subprocess, sys, time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('command')
for flag in ('--store', '--attempt', '--item', '--session', '--note', '--out', '--template', '--params',
             '--node-key', '--context-hash', '--base-sha', '--workspace', '--goal'):
    parser.add_argument(flag)
parser.add_argument('--rebind', action='store_true')
args = parser.parse_args()
store = Path(args.store or os.path.join(os.environ['AITELIER_DSH_STATE_DIR'], 'todo'))
store.mkdir(parents=True, exist_ok=True)

def emit(value, code=0):
    print(json.dumps(value))
    sys.exit(code)

def git(plan, *argv):
    return subprocess.run(['git', *argv], cwd=plan['workspace'], capture_output=True, text=True, check=True).stdout.strip()

# One template, shaped like todo_templates/code_change.yaml: params are typed,
# predicate args reference a param by whole value ('$name'), titles by '$' '{name}' (split so this JS template stays literal).
TEMPLATES = {'code_change': {
    'name': 'code_change', 'version': 1, 'description': 'Change files within a scope, then pass a test command.',
    'params': {'paths': {'type': 'list[str]', 'required': True}, 'scope': {'type': 'list[str]', 'required': True},
               'test_argv': {'type': 'list[str]', 'required': True}, 'marker': {'type': 'str', 'required': False, 'default': None},
               'test_timeout': {'type': 'int', 'required': False, 'default': 1800}},
    'items': [
        {'id': 'implement', 'title': 'change $' '{paths}', 'needs': [], 'predicates': [
            {'predicate': 'files_changed', 'args': {'paths': '$paths', 'contains': '$marker'}},
            {'predicate': 'changes_within', 'args': {'scope': '$scope'}}]},
        {'id': 'test', 'title': 'run $' '{test_argv}', 'needs': ['implement'], 'predicates': [
            {'predicate': 'command_exit', 'args': {'argv': '$test_argv', 'expect': 0, 'timeout': '$test_timeout'}}]},
    ]}}
TYPES = {'str': lambda v: isinstance(v, str), 'int': lambda v: isinstance(v, int) and not isinstance(v, bool),
         'list[str]': lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v)}

def bind(value, params):
    return params[value[1:]] if isinstance(value, str) and value.startswith('$') else value

def evaluate(plan, item):
    results = []
    for binding in item['predicates']:
        name, bound = binding['predicate'], binding.get('args', {})
        if name == 'files_changed':
            ok = all(os.path.isfile(os.path.join(plan['workspace'], p)) for p in bound['paths'])
        elif name == 'changes_within':
            ok = True
        elif name == 'command_exit':
            ok = subprocess.run(bound['argv'], cwd=plan['workspace']).returncode == bound['expect']
        else:
            ok = git(plan, 'rev-parse', 'HEAD') != bound['base'] and not git(plan, 'status', '--porcelain')
        results.append({'predicate': name, 'outcome': 'pass' if ok else 'fail', 'detail': name})
    return results

def status(plan):
    done = plan['done']
    remaining = [i['id'] for i in plan['items'] if i['id'] not in done]
    upcoming = next((i['id'] for i in plan['items'] if i['id'] not in done and all(n in done for n in i['needs'])), None)
    brief = f"{plan['goal']}\nNext: {upcoming or '(none)'}\nRemaining {len(remaining)} item(s): {', '.join(remaining)}"
    return {'attempt_id': plan['attempt_id'], 'brief': brief, 'goal': plan['goal'], 'next': upcoming, 'done': done, 'remaining': remaining,
            'deferred': [], 'unmeasurable': [], 'child_session': plan['child_session'], 'rebound_from': plan.get('rebound_from'), 'items': plan['items'],
            'node_key': plan['node_key'], 'timeout_budget_seconds': {i['id']: 5 for i in plan['items']},
            'finish_budget_seconds': 15}

def save(plan):
    path = store / (plan['attempt_id'] + '.json')
    path.with_suffix('.tmp').write_text(json.dumps(plan))
    path.with_suffix('.tmp').replace(path)

if args.command == 'templates':
    emit({'templates': [{k: t[k] for k in ('name', 'version', 'description', 'params')} for t in TEMPLATES.values()]})
if args.command == 'create':
    template = TEMPLATES.get(args.template)
    if template is None:
        emit({'refused': 'unknown_template', 'reason': str(args.template)}, 2)
    given = json.loads(args.params)
    unknown = sorted(set(given) - set(template['params']))
    if unknown:
        emit({'refused': 'invalid_params', 'reason': 'unknown params ' + ', '.join(unknown)}, 2)
    params = {}
    for key, spec in template['params'].items():
        if key not in given:
            if spec['required']:
                emit({'refused': 'invalid_params', 'reason': 'missing ' + key}, 2)
            params[key] = spec['default']
        elif not TYPES[spec['type']](given[key]):
            emit({'refused': 'invalid_params', 'reason': key + ' must be ' + spec['type']}, 2)
        else:
            params[key] = given[key]
    items = [{'id': i['id'], 'title': i['title'].replace('$' '{paths}', ', '.join(params['paths'])).replace('$' '{test_argv}', ' '.join(params['test_argv'])),
              'needs': i['needs'], 'predicates': [{'predicate': b['predicate'], 'args': {k: bind(v, params) for k, v in b['args'].items()}}
                                                   for b in i['predicates']]} for i in template['items']]
    items.append({'id': 'deliver', 'title': 'deliver', 'needs': [i['id'] for i in items],
                  'predicates': [{'predicate': 'git_delivered', 'args': {'base': args.base_sha}}]})
    plan = {'attempt_id': args.attempt, 'node_key': args.node_key, 'context_hash': args.context_hash, 'base_sha': args.base_sha,
            'workspace': args.workspace, 'goal': args.goal, 'items': items, 'params': params,
            'template': {'name': template['name'], 'version': template['version']},
            'done': [], 'child_session': None, 'notes': [], 'results': {}}
    save(plan)
    emit({'plan_hash': hashlib.sha256(json.dumps(items).encode()).hexdigest()})
if args.command == 'list':
    plans = [status(json.loads(p.read_text())) for p in sorted(store.glob('*.json'))]
    emit({'plans': [{k: v for k, v in p.items() if k in ('attempt_id', 'node_key', 'next', 'done', 'remaining', 'child_session')} for p in plans]})

path = store / (args.attempt + '.json')
if not path.exists():
    emit({'refused': 'no_such_plan', 'reason': args.attempt}, 2)
plan = json.loads(path.read_text())
item = next((i for i in plan['items'] if i['id'] == args.item), None)
if args.command == 'status':
    emit(status(plan))
if args.command == 'handback':
    emit({**status(plan), 'reason': 'todo_unfinished', 'last_observations': {
        i: plan['results'].get(i, []) for i in status(plan)['remaining']}})
if args.command == 'done' and args.item in ('hang', 'stubborn'):
    # A predicate that outlives its bound, with a grandchild of its own; the
    # stubborn one (and its grandchild, by inheritance) ignores SIGTERM.
    if args.item == 'stubborn':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sleeper = subprocess.Popen(['sleep', '30'])
    (store / 'hang.pid').write_text(str(sleeper.pid))
    time.sleep(30)
if args.command == 'begin':
    if not all(n in plan['done'] for n in item['needs']):
        emit({'refused': 'unmet_needs', 'reason': ', '.join(item['needs'])}, 2)
    if plan['child_session'] and args.session and args.session != plan['child_session']:
        # The real CLI's one rebinding path: --rebind records who held the plan before.
        if not args.rebind:
            emit({'refused': 'bound_to_other_session', 'reason': 'bound to ' + plan['child_session'], 'bound_session': plan['child_session']}, 2)
        plan['rebound_from'] = plan['child_session']
    plan['child_session'] = args.session or plan['child_session']
    save(plan)
    emit({'begun': item['id'], 'child_session': plan['child_session'], 'rebound_from': plan.get('rebound_from')})
if args.command in ('done', 'report') and args.session and plan['child_session'] and args.session != plan['child_session']:
    emit({'refused': 'bound_to_other_session', 'reason': 'bound to ' + plan['child_session'], 'bound_session': plan['child_session']}, 2)
if args.command == 'done':
    results = evaluate(plan, item)
    outcome = 'pass' if all(r['outcome'] == 'pass' for r in results) else 'fail'
    if outcome == 'pass' and item['id'] not in plan['done']:
        plan['done'].append(item['id'])
    plan['results'][item['id']] = results
    save(plan)
    emit({'outcome': outcome, 'results': results, 'brief': status(plan)['brief']})
if args.command == 'report':
    plan['notes'].append({'item': args.item, 'note': args.note})
    save(plan)
    emit({'noted': args.item})
if args.command == 'finish':
    failing = [i['id'] for i in plan['items'] if any(r['outcome'] != 'pass' for r in evaluate(plan, i))]
    if failing:
        emit({'refused': 'unfinished', 'failing': failing}, 2)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    bundle = out / (args.attempt + '.bundle')
    git(plan, 'bundle', 'create', str(bundle), 'HEAD')
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    # The exact name the real CLI writes (todo_cli.cmd_finish names it after the
    # attempt). The runtime looks for this file to tell "nobody finished this plan"
    # apart from "its candidate already exists".
    report = out / (args.attempt + '-candidate.json')
    report.write_text(json.dumps({'status': 'candidate', 'artifact': git(plan, 'rev-parse', 'HEAD'),
                                  'git_bundle': {'path': str(bundle), 'sha256': digest}}))
    emit({'git_bundle': {'path': str(bundle), 'sha256': digest}, 'report': str(report)})
emit({'refused': 'usage', 'reason': args.command}, 2)
`;

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-todo-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.AITELIER_URL + path, { method: 'POST', body: JSON.stringify(body) });
const plans = JSON.parse(process.env.AITELIER_TEST_PLANS);
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const shell = (id, command) => tool(id, 'bash', { description: 'fixture work', command, run_in_background: false });
// implement writes the files; test only needs its command to pass.
const work = (role, plan, ids) => ids.flatMap(item => [
  tool(role + '-todo_begin-' + item, 'todo_begin', { attempt_id: plan.attempt_id, item_id: item }),
  ...(item === 'implement' ? [shell(role + '-shell-' + item, "cd '" + plan.workspace + "' && printf a > a.txt && printf b > b.txt")] : []),
  tool(role + '-todo_done-' + item, 'todo_done', { attempt_id: plan.attempt_id, item_id: item }),
]);
const commit = plan => shell('full-shell-commit', "cd '" + plan.workspace + "' && git add -A && git -c user.name=fixture -c user.email=fixture@example.invalid commit -q -m work");
const scripts = {
  full: [
    // The real server's shape: one state_graph_write tool, the operation in action.
    ...['record_evidence', 'verify_node', 'report_external_attempt'].map(action =>
      tool('full-judge-' + action, 'call_tool', { name: 'mcp__aitelier__state_graph_write', arguments: { action, arguments: { attempt_id: plans.a.attempt_id } } })),
    tool('full-write-report_issue', 'call_tool', { name: 'mcp__aitelier__state_graph_write', arguments: { action: 'report_issue', arguments: { title: 'worker observation' } } }),
    tool('full-write-resolve_issue', 'call_tool', { name: 'mcp__aitelier__state_graph_write', arguments: { action: 'resolve_issue', arguments: { issue_id: 'iss-1' } } }),
    tool('full-todo_status-start', 'todo_status', { attempt_id: plans.a.attempt_id }),
    ...work('full', plans.a, ['implement']),
    'COMPACT',
    tool('full-todo_status-after-compact', 'todo_status', { attempt_id: plans.a.attempt_id }),
    ...work('full', plans.a, ['test']),
    commit(plans.a),
    tool('full-todo_begin-deliver', 'todo_begin', { attempt_id: plans.a.attempt_id, item_id: 'deliver' }),
    tool('full-todo_done-deliver', 'todo_done', { attempt_id: plans.a.attempt_id, item_id: 'deliver' }),
    { type: 'text', text: 'FULL_CHILD_DONE' },
  ],
  // Re-dispatched after the handback: the partial child is dead, so its plan is rebound.
  resume: [
    // Before rebinding, the plan still belongs to the dead partial child: done and report are refused.
    tool('resume-todo_done-early', 'todo_done', { attempt_id: plans.b.attempt_id, item_id: 'deliver' }),
    tool('resume-todo_report-early', 'todo_report', { attempt_id: plans.b.attempt_id, item_id: 'deliver', note: '-starts with a dash' }),
    tool('resume-todo_begin-deliver', 'todo_begin', { attempt_id: plans.b.attempt_id, item_id: 'deliver' }),
    tool('resume-todo_report-bound', 'todo_report', { attempt_id: plans.b.attempt_id, item_id: 'deliver', note: '-starts with a dash' }),
    { type: 'text', text: 'RESUME_CHILD_STOPS' },
  ],
  partial: [
    tool('partial-todo_status-start', 'todo_status', { attempt_id: plans.b.attempt_id }),
    ...work('partial', plans.b, ['implement', 'test']),
    { type: 'text', text: 'PARTIAL_CHILD_STOPS' },
  ],
};
export function apply(ctx) {
  const roles = new Map(), steps = new Map(), root = process.env.AITELIER_DSH_SESSION;
  const done = new Set();
  let pressure = false, woke = false;
  const once = (key, block) => done.has(key) ? undefined : (done.add(key), block);
  class FixtureAdapter extends LlmAdapter {
    // 24000, not the 16000 this fixture used to declare. The director's request
    // envelope is ~27.7k chars of tool schemas plus a ~25k-char surface: ~13.3k
    // estimated tokens (dsh-token-meter prices 4 chars/token), so a 16000 window
    // at thresholdRatio 0.8 (12800) put the director permanently ABOVE its own
    // compaction threshold from its first turn, and every turn was pre-empted by
    // a compaction. On a loaded host one compaction outlasts the 3 s idle-review
    // interval, so reviews arrived during compactions and were compacted away
    // before the model saw them: the scenario looped (measured: 69 s of root
    // compaction turns, "root never finished") instead of merely running slowly -
    // a fixture that declares a window smaller than the surface it forces cannot
    // tell a slow runtime from a stuck one. 24000 keeps the ordinary surface below
    // the threshold while the 30000-token padding blocks still force exactly the
    // pressure compactions this scenario asserts on.
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 24000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      if (!roles.has(options.sessionId)) roles.set(options.sessionId, options.sessionId === root ? 'root'
        : text.includes('TODO_CHILD_FULL') ? 'full' : text.includes('TODO_CHILD_PARTIAL') ? 'partial'
        : text.includes('TODO_CHILD_RESUME') ? 'resume' : 'other');
      const role = roles.get(options.sessionId);
      await post('/test/request', { session: options.sessionId, role, purpose: options.purpose ?? null, messages: options.messages, tools: options.tools });
      let blocks, usage = 20;
      if (options.purpose === 'compaction') blocks = [{ type: 'text', text: 'Checkpoint: continue the plan; use todo_status for the current step.' }];
      else if (!options.tools?.length) blocks = [{ type: 'text', text: 'Fixture session' }];
      else if (role in scripts) {
        const step = steps.get(options.sessionId) ?? 0;
        steps.set(options.sessionId, step + 1);
        const next = scripts[role][step] ?? { type: 'text', text: role.toUpperCase() + '_CHILD_IDLE' };
        if (next === 'COMPACT') {
          usage = 30000;
          blocks = [{ type: 'text', text: 'CHILD_PADDING ' + 'worker context '.repeat(14000) },
            tool('full-todo_status-before-compact', 'todo_status', { attempt_id: plans.a.attempt_id })];
        } else blocks = [next];
      } else if (role === 'root') {
        const handed = text.includes('AItelier todo handback:'), completed = text.includes('FULL_CHILD_DONE');
        // The director waits until it has SEEN the runtime's idle review naming plan a
        // as ready_to_finish before it finishes that plan (the review is the reminder
        // that tells it to). Requiring the review to be in THIS request is what makes
        // the review payloads asserted below deterministic instead of a race: any
        // earlier review merely satisfied "some review arrived", so a slow child boot
        // let the director finish in the same turn as the handback and every review
        // then arrived after the candidate existed, when the plan has legitimately
        // dropped out. Waiting for the specific payload guarantees one exists.
        const reviewed = (options.messages ?? []).some(message => (message.content ?? [])
          .some(part => {
            if (typeof part.text !== 'string' || !part.text.startsWith('AItelier director review due:')) return false;
            try {
              return JSON.parse(part.text.split('\n').slice(1).join('\n')).todo_unfinished
                ?.some(plan => plan.attempt_id === plans.a.attempt_id && plan.ready_to_finish);
            } catch { return false; }
          }));
        const block = once('templates', tool('root-todo_templates', 'todo_templates', {}))
          ?? once('plan-items', tool('root-todo_plan-items', 'todo_plan', { ...plans.bad.input, items: [{ id: 'improvised', title: 'x', needs: [], predicates: [] }] }))
          ?? once('plan-typo', tool('root-todo_plan-typo', 'todo_plan', { ...plans.bad.input, params: { ...plans.bad.input.params, paths: 'a.txt' } }))
          ?? once('plan-a', tool('root-todo_plan-a', 'todo_plan', plans.a.input))
          ?? once('plan-b', tool('root-todo_plan-b', 'todo_plan', plans.b.input))
          ?? once('spawn-full', tool('root-spawn-full', 'subagent', { description: 'full todo child', prompt: 'TODO_CHILD_FULL attempt_id=' + plans.a.attempt_id + '; use todo_status, todo_begin and todo_done.' }))
          ?? once('spawn-partial', tool('root-spawn-partial', 'subagent', { description: 'partial todo child', prompt: 'TODO_CHILD_PARTIAL attempt_id=' + plans.b.attempt_id + '; use todo_status, todo_begin and todo_done.' }))
          ?? once('judge', tool('root-judge-record_evidence', 'call_tool', { name: 'mcp__aitelier__state_graph_write', arguments: { action: 'record_evidence', arguments: { attempt_id: plans.a.attempt_id } } }))
          ?? (handed ? once('spawn-resume', tool('root-spawn-resume', 'subagent', { description: 'resume todo child', prompt: 'TODO_CHILD_RESUME attempt_id=' + plans.b.attempt_id + '; continue with todo_begin.' })) : undefined)
          ?? (handed && completed && reviewed ? once('finish', tool('root-todo_finish-a', 'todo_finish', { attempt_id: plans.a.attempt_id })) : undefined)
          ?? (done.has('finish') ? once('pressure', { type: 'text', text: 'ROOT_PADDING ' + 'director context '.repeat(14000) }) : undefined)
          ?? (woke ? once('status-b', tool('root-todo_status-after-compact', 'todo_status', { attempt_id: plans.b.attempt_id })) : undefined)
          ?? { type: 'text', text: done.has('status-b') ? 'ROOT_DONE' : 'Director idle.' };
        if (block.text?.startsWith('ROOT_PADDING')) { pressure = true; usage = 30000; }
        if (block.text === 'ROOT_DONE') void post('/test/root-done', {});
        blocks = [block];
      } else blocks = [{ type: 'text', text: 'Unexpected session' }];
      for (const [index, block] of blocks.entries()) {
        yield { type: 'block-start', index, blockType: block.type };
        if (block.type === 'text') yield { type: 'text-delta', index, text: block.text };
        else yield { type: 'tool-call-delta', index, id: block.id, name: block.name, argumentsDelta: block.arguments };
        yield { type: 'block-end', index, block };
      }
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: usage } };
      yield { type: 'finish', reason: { kind: blocks.at(-1).type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  // The root's pressure reply leaves it idle; a State event wakes it into compaction.
  ctx.on('agent/status', ({ agent, status }) => {
    if (agent.session.id === root && status === 'idle' && pressure && !woke) { woke = true; void post('/test/change', {}); }
  });
  ctx.on('session/event', (session, event) => {
    if (['tool/result', 'user/message', 'compaction/end'].includes(event.type)) void post('/test/event', { session: session.id, event });
  });
}
`;

const git = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
async function workspace(path) {
  await mkdir(path, { recursive: true });
  git(path, 'init', '-q');
  await writeFile(join(path, 'README'), 'base\n');
  git(path, 'add', 'README');
  git(path, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '-m', 'base');
  return git(path, 'rev-parse', 'HEAD');
}
/** The exact todo_plan arguments: a template, its params, and the attempt binding. */
const planInput = (attempt_id, node_key, base_sha, path) => ({ template: 'code_change',
  params: { paths: ['a.txt', 'b.txt'], scope: ['*.txt'], test_argv: ['test', '-s', 'a.txt'], test_timeout: 60 },
  goal: 'Write a.txt and b.txt for ' + node_key, attempt_id, node_key, context_hash: 'c'.repeat(64), base_sha, workspace: path });
const createArgs = plan => ['create', '--template', plan.template, '--params', JSON.stringify(plan.params), '--attempt', plan.attempt_id,
  '--node-key', plan.node_key, '--context-hash', plan.context_hash, '--base-sha', plan.base_sha, '--workspace', plan.workspace, '--goal', plan.goal];
const toolText = event => event.data.message.content.map(block => block.text ?? '').join('');
const toolJson = event => JSON.parse(toolText(event));

async function fakeCli(directory) {
  await mkdir(join(directory, 'core'), { recursive: true });
  await writeFile(join(directory, 'core', '__init__.py'), '');
  await writeFile(join(directory, 'core', 'todo_cli.py'), FAKE_CLI);
  return { python: process.env.AITELIER_TODO_TEST_PYTHON ?? 'python3', cwd: process.env.AITELIER_TODO_TEST_CWD ?? directory };
}

// The crash arm of the CLI contract has to come from the interpreter it is
// handed, never from `core.todo_cli` being unimportable: this image installs
// AItelier editable (site-packages/__editable__.aitelier-*.pth -> /app), so
// `python -m core.todo_cli` from a directory without a local `core` reaches the
// DEPLOYED copy and answers a usage refusal (exit 2) instead of crashing. This
// fixture's module crashes on import, from any cwd.
async function crashCli(directory) {
  await mkdir(join(directory, 'core'), { recursive: true });
  await writeFile(join(directory, 'core', '__init__.py'), '');
  await writeFile(join(directory, 'core', 'todo_cli.py'), 'raise SystemExit(3)\n');
  return { python: process.env.AITELIER_TODO_TEST_PYTHON ?? 'python3', cwd: directory };
}

// This scenario is bounded by the progress it observes, not by the clock (see
// progress.mjs): the wait below is a watch over the plans' own milestones - a
// child's item passing, a handback, an idle review, the director's finish and
// post-compaction status - so a runtime that is merely slow finishes and passes
// whatever the host is doing, while a runtime whose watched progress stops
// arriving still fails. A harness timeout would be a second, silent wall budget
// on exactly the thing this replaces.
test('real pinned DSH: plans walked by native children, runtime handback, State-writer refusal, briefs across compaction', { timeout: Infinity }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;                       // nothing pinned is installed: reported unmeasured
  const directory = await mkdtemp(join(homedir(), 'dsh-todo-'));
  const here = dirname(fileURLToPath(import.meta.url));
  const requests = [], events = [], mcpCalls = [], activity = [], parked = new Set();
  let changed = false, rootDone = false, output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/mcp') {
      if (req.method === 'GET') { res.statusCode = 405; return res.end(); }
      if (req.method === 'DELETE') { res.statusCode = 200; return res.end(); }
      if (!Object.hasOwn(body, 'id')) { res.statusCode = 202; return res.end(); }
      let result;
      if (body.method === 'initialize') result = { protocolVersion: body.params.protocolVersion,
        capabilities: { tools: {} }, serverInfo: { name: 'fixture-aitelier', version: '1' } };
      else if (body.method === 'tools/list') result = { tools: [{ name: 'state_graph_write', description: 'Manage State facts',
        inputSchema: { type: 'object', properties: { action: { type: 'string' }, arguments: { type: 'object' } }, required: ['action', 'arguments'] } }] };
      else if (body.method === 'tools/call') { mcpCalls.push(body.params.name + ':' + body.params.arguments?.action); result = { content: [{ type: 'text', text: 'fixture_state_write_ok' }] }; }
      else result = {};
      return send({ jsonrpc: '2.0', id: body.id, result });
    }
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture', event_seq: 10,
      nodes: [{ name: 'n', status: changed ? 'VERIFIED' : 'PENDING' }], project_note: { index: [] },
      private_note: { index: [] }, driver_skill: 'fixture driver skill' });
    if (req.url.endsWith('/activity')) { activity.push(body); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/query/wait_for_state_change') {
      if (changed && body.after < 11) return send({ timed_out: false, next_after: 11, next_inbox_after: 0, events: [{ type: 'node_changed' }] });
      parked.add(res); res.on('close', () => parked.delete(res)); return;
    }
    if (req.url === '/test/request') { requests.push(body); return send({ ok: true }); }
    if (req.url === '/test/event') { events.push(body); return send({ ok: true }); }
    if (req.url === '/test/root-done') { rootDone = true; return send({ ok: true }); }
    if (req.url === '/test/change') {
      changed = true;
      for (const target of parked) { target.setHeader('content-type', 'application/json'); target.end(JSON.stringify({ timed_out: false, next_after: 11, next_inbox_after: 0, events: [{ type: 'node_changed' }] })); }
      parked.clear(); return send({ ok: true });
    }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const cli = await fakeCli(join(directory, 'cli'));
    const plans = {};
    for (const [key, node] of [['a', 'execution.todo-full'], ['b', 'execution.todo-partial']]) {
      const path = join(directory, 'work', key);
      const input = planInput('attempt-todo-' + key, node, await workspace(path), path);
      plans[key] = { attempt_id: input.attempt_id, workspace: path, input };
    }
    plans.bad = { input: { ...plans.a.input, attempt_id: 'attempt-todo-bad' } };
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-todo-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true }, { id: 'compaction-basic', config: { thresholdRatio: 0.8, retainTokens: 128, headroomTokens: 512 } },
      { id: 'tool-subagent', config: { provider: 'spawn', toolName: 'subagent', agentOptions: { provider: 'fixture', model: 'worker' } } },
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-todo-provider.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') },
        { id: 'mcp-aitelier', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'aitelier', transport: 'streamable-http', url: base + '/mcp', failOnStartupError: true } }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutCallerMode(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'fixture-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_DSH_EXECUTION: 'host', DSH_PERMISSION_MODE: 'danger-full-access', AITELIER_DSH_IDLE_REVIEW_MS: '3000',
      AITELIER_TODO_PYTHON: cli.python, AITELIER_TODO_CWD: cli.cwd, AITELIER_TEST_PLANS: JSON.stringify(plans) } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);
    // Each review carries its arrival position, the one ordering that spans both
    // the root's and a child's session (session seq numbers are per session).
    const reviews = () => events.map((e, index) => ({ index, e }))
      .filter(({ e }) => e.session === 'fixture-root' && e.event.type === 'user/message'
        && e.event.data.content?.[0]?.text?.startsWith('AItelier director review due:'))
      .map(({ index, e }) => ({ index, ...JSON.parse(e.event.data.content[0].text.split('\n').slice(1).join('\n')) }));
    const reviewedB = () => reviews().some(r => r.todo_unfinished?.some(p => p.attempt_id === plans.b.attempt_id));
    const handbackCount = () => events.filter(e => e.session === 'fixture-root' && e.event.type === 'user/message'
      && e.event.data.content?.[0]?.text?.startsWith('AItelier todo handback:')).length;
    const diagnostics = () => output + '\n' + JSON.stringify(requests.map(r => ({ role: r.role, purpose: r.purpose,
      last: JSON.stringify(r.messages.at(-1)).slice(0, 300) })), null, 1).slice(-12000);
    const resultOf = id => events.find(e => e.event.type === 'tool/result' && e.event.data.message.toolCallId === id);
    const passed = id => { const r = resultOf(id); return Boolean(r) && !r.event.data.message.isError && toolJson(r.event).outcome === 'pass'; };
    const sessionOfRole = role => requests.find(r => r.role === role)?.session;
    // Wait for the plans' OBSERVED PROGRESS, not for the wall clock. Every
    // milestone below is something this scenario then asserts on, so the wait
    // covers the assertions instead of racing them; under load the old 75 s
    // deadline expired on a runtime that was still walking the plans and said
    // only "scenario did not finish". Only forward signals are listed: the
    // runtime's idle traffic (activity posts, heartbeats, the 3 s idle review, a
    // repeated review naming the same plan) advances none of them, so a runtime
    // whose watched progress never arrives still reds - after `quietMs`, naming
    // the milestones it never reached.
    const progress = await watchProgress({
      what: 'the todo scenario to complete (both plans walked by native children, the runtime handbacks, the idle reviews, the director finish and its post-compaction status)',
      running: () => proc.exitCode === null,
      failed: () => activity.some(a => a.phase === 'failed'),
      milestones: [
        { name: 'root listed the todo templates', met: () => resultOf('root-todo_templates') },
        { name: 'root froze plan a from the template', met: () => resultOf('root-todo_plan-a') },
        { name: 'root froze plan b from the template', met: () => resultOf('root-todo_plan-b') },
        { name: 'full child session dispatched', met: () => sessionOfRole('full') },
        { name: 'partial child session dispatched', met: () => sessionOfRole('partial') },
        { name: 'root recorded the evidence through State', met: () => resultOf('root-judge-record_evidence') },
        { name: 'full child passed implement', met: () => passed('full-todo_done-implement') },
        { name: 'full child compacted its own context', met: () => events.some(e => e.session === sessionOfRole('full') && e.event.type === 'compaction/end' && !e.event.data.error) },
        { name: 'compacted child was given its plan brief', met: () => events.some(e => e.session === sessionOfRole('full') && e.event.type === 'user/message' && e.event.data.source?.kind === todoBriefSource) },
        { name: 'full child passed test', met: () => passed('full-todo_done-test') },
        { name: 'full child passed deliver, leaving plan a all-passed', met: () => passed('full-todo_done-deliver') },
        { name: 'partial child stopped with items left (first handback)', met: () => handbackCount() >= 1 },
        { name: 'idle review named the unfinished plan b', met: () => reviewedB() },
        { name: 'idle review named the all-passed plan a as ready to finish', met: () => reviews().some(r => r.todo_unfinished?.some(p => p.attempt_id === plans.a.attempt_id && p.ready_to_finish)) },
        { name: 'resume child dispatched for the handed-back plan', met: () => sessionOfRole('resume') },
        { name: 'resume child was handed back too (second handback)', met: () => handbackCount() >= 2 },
        { name: 'root finished the all-passed plan', met: () => resultOf('root-todo_finish-a') },
        { name: 'root compacted after the finish', met: () => events.some(e => e.session === 'fixture-root' && e.event.type === 'compaction/end' && !e.event.data.error) },
        { name: 'post-compaction State snapshot carried the plans', met: () => events.some(e => e.session === 'fixture-root' && e.event.type === 'user/message' && e.event.data.source?.kind === stateInjectionSource && e.event.data.source.compaction_seq > 0) },
        { name: 'root re-read plan b after that compaction', met: () => resultOf('root-todo_status-after-compact') },
        { name: 'root reported the walk finished (ROOT_DONE)', met: () => rootDone },
      ],
    });
    assert.equal(proc.exitCode, null, output);
    assert.ok(!activity.some(a => a.phase === 'failed'), JSON.stringify(activity.filter(a => a.phase === 'failed')) + output);
    // A red names the watch that expired and the last progress it saw, plus the
    // request diagnostics - the old "scenario did not finish" named neither.
    assert.ok(progress.done, `${progress.message}\n` + diagnostics());
    assert.ok(rootDone, 'the director never finished the walk\n' + diagnostics());
    const results = events.filter(e => e.event.type === 'tool/result').map(e => ({ session: e.session, id: e.event.data.message.toolCallId, event: e.event }));
    const result = id => results.find(r => r.id === id);
    const roles = new Map(requests.map(r => [r.session, r.role]));
    const sessionOf = role => [...roles].find(([, value]) => value === role)?.[0];
    const full = sessionOf('full'), partial = sessionOf('partial');
    assert.ok(full && partial && full !== partial && ![full, partial].includes('fixture-root'));

    // 0. Plans come from templates only: the listing is the CLI's, items cannot be
    // passed, and a wrongly typed param is a refusal result, not a throw.
    const templates = toolJson(result('root-todo_templates').event);
    assert.ok(templates.templates.some(t => t.name === 'code_change' && t.params.paths), JSON.stringify(templates));
    const planSchema = requests.find(r => r.role === 'root' && r.tools?.length).tools.find(t => t.name === 'todo_plan').parameters;
    assert.deepEqual(Object.keys(planSchema.properties).sort(),
      ['attempt_id', 'base_sha', 'context_hash', 'goal', 'node_key', 'params', 'template', 'workspace']);
    assert.notEqual(planSchema.additionalProperties, true);
    const improvised = result('root-todo_plan-items').event;
    assert.equal(toolJson(improvised).refused, 'unknown_arguments', 'todo_plan accepted free items: ' + toolText(improvised));
    const typo = result('root-todo_plan-typo').event;
    assert.equal(typo.data.message.isError, false, toolText(typo));
    assert.ok(toolJson(typo).refused, toolText(typo)); assert.match(toolText(typo), /paths/);
    await assert.rejects(stat(join(directory, 'todo', 'attempt-todo-bad.json')), { code: 'ENOENT' });

    // 1. The full child walked implement, test and deliver of the code_change plan;
    // every todo result carried the brief.
    for (const item of ['implement', 'test', 'deliver']) assert.equal(toolJson(result('full-todo_done-' + item).event).outcome, 'pass', item);
    const todoResults = results.filter(r => /^(full|partial)-todo_/.test(r.id));
    assert.ok(todoResults.length >= 12);
    for (const r of todoResults) {
      assert.equal(r.event.data.message.isError, false, r.id + toolText(r.event));
      assert.match(toolJson(r.event).brief, /Write a\.txt and b\.txt/, r.id + ' result lacks the brief');
    }
    assert.ok(todoResults.filter(r => r.id.startsWith('full-')).every(r => r.session === full));
    const finalA = JSON.parse(execFileSync(cli.python, ['-m', 'core.todo_cli', 'status', '--attempt', plans.a.attempt_id],
      { cwd: cli.cwd, env: { ...process.env, AITELIER_DSH_STATE_DIR: directory }, encoding: 'utf8' }));
    assert.deepEqual(finalA.remaining, []); assert.equal(finalA.child_session, full, 'todo_begin bound the root, not the calling child');

    // 2. The partial child stopped after item 2: the runtime, not the model, handed it back.
    // (The goal text itself names todo_unfinished, so match the runtime's message.)
    const handbacks = events.filter(e => e.session === 'fixture-root' && e.event.type === 'user/message'
      && e.event.data.content?.[0]?.text?.startsWith('AItelier todo handback:'));
    assert.equal(handbacks.length, 2, 'expected the partial and the resume child to be handed back\n' + diagnostics());
    assert.equal(handbacks[0].event.data.source.kind, runtimeEventSource);
    const handback = JSON.parse(handbacks[0].event.data.content[0].text.split('\n').slice(1).join('\n'));
    assert.equal(handback.reason, 'todo_unfinished'); assert.equal(handback.attempt_id, plans.b.attempt_id);
    assert.equal(handback.next, 'deliver'); assert.deepEqual(handback.done, ['implement', 'test']);
    assert.deepEqual(handback.remaining, ['deliver']);
    assert.deepEqual(Object.keys(handback.last_observations), ['deliver']);
    // The re-dispatched child took over the dead child's plan (CLI refusal, then --rebind)
    // and was handed back in turn; nothing else rebinds a plan.
    const resume = sessionOf('resume');
    for (const id of ['resume-todo_done-early', 'resume-todo_report-early']) {
      const early = result(id);
      assert.equal(early.event.data.message.isError, false, id + ' threw instead of refusing');
      assert.equal(toolJson(early.event).refused, 'bound_to_other_session', id + ': ' + toolText(early.event));
      assert.match(toolJson(early.event).brief, /Write a\.txt and b\.txt/);
    }
    assert.equal(toolJson(result('resume-todo_report-bound').event).refused, undefined, toolText(result('resume-todo_report-bound').event));
    const resumed = toolJson(result('resume-todo_begin-deliver').event);
    assert.equal(resumed.refused, undefined, JSON.stringify(resumed));
    assert.equal(result('resume-todo_begin-deliver').session, resume);
    const second = JSON.parse(handbacks[1].event.data.content[0].text.split('\n').slice(1).join('\n'));
    assert.equal(second.attempt_id, plans.b.attempt_id); assert.equal(second.next, 'deliver');

    // 3. A child cannot judge its own work through state_graph_write; the root can.
    // Workers get an allowlist: report_issue passes, resolve_issue does not.
    for (const action of ['record_evidence', 'verify_node', 'report_external_attempt']) {
      const refused = result('full-judge-' + action);
      assert.equal(refused.session, full); assert.equal(refused.event.data.message.isError, true);
      assert.match(toolText(refused.event), /workers do not judge their own work; the director records State evidence/);
    }
    const rootJudge = result('root-judge-record_evidence');
    assert.equal(rootJudge.event.data.message.isError, false); assert.match(toolText(rootJudge.event), /fixture_state_write_ok/);
    assert.equal(result('full-write-report_issue').event.data.message.isError, false);
    const resolve = result('full-write-resolve_issue').event;
    assert.equal(resolve.data.message.isError, true, 'a worker resolved an issue');
    assert.match(toolText(resolve), /may only state_graph_write report_issue\)/);
    assert.deepEqual(mcpCalls.sort(), ['state_graph_write:record_evidence', 'state_graph_write:report_issue'], 'a refused State write reached the server');

    // 3b. The real idle review (short interval) names the plan with items left. The
    // all-pass plan is named too, until its candidate exists - as ready_to_finish,
    // with nothing left to run, never as items to dispatch again. The fixture root
    // waits for a review before it finishes plan a (the gate in PROVIDER), so at least
    // one payload is emitted while plan a is all-passed and nobody finished it: the
    // loop below runs on real payloads, and a runtime that hides an all-passed plan
    // from the review leaves it empty.
    assert.ok(reviewedB(), 'no idle review named the unfinished plan\n' + JSON.stringify(reviews()));
    const review = reviews().filter(r => r.todo_unfinished?.some(p => p.attempt_id === plans.b.attempt_id)).at(-1);
    assert.deepEqual(review.todo_unfinished.filter(p => p.attempt_id === plans.b.attempt_id), [
      { attempt_id: plans.b.attempt_id, node_key: 'execution.todo-partial', next: 'deliver', remaining: ['deliver'], ready_to_finish: false }]);
    assert.ok(review.todo_unfinished.every(p => p.ready_to_finish === (p.remaining.length === 0)),
      'a plan was marked the wrong way round: ' + JSON.stringify(review.todo_unfinished));
    // Only reviews that can speak to this property are checked: one that arrives
    // before plan a is all-passed may legitimately list it with items left (a slow
    // native child boot puts one in that window - that is not an all-passed plan
    // "handed back as items to run"), and one that already lists it all-passed is
    // evidence whatever the arrival order of two concurrent loopback posts was.
    const allPassedAt = events.findIndex(e => e.event.type === 'tool/result'
      && e.event.data.message.toolCallId === 'full-todo_done-deliver');
    const namedA = r => r.todo_unfinished?.filter(p => p.attempt_id === plans.a.attempt_id) ?? [];
    const allPassed = reviews().filter(r => namedA(r).length
      && (r.index > allPassedAt || namedA(r).every(p => p.remaining.length === 0)));
    assert.ok(allPassed.length, 'no idle review named the all-passed plan before it was finished: ' + JSON.stringify(reviews()));
    for (const payload of allPassed) {
      assert.deepEqual(payload.todo_unfinished.filter(p => p.attempt_id === plans.a.attempt_id), [
        { attempt_id: plans.a.attempt_id, node_key: 'execution.todo-full', next: null, remaining: [], ready_to_finish: true }],
        'the all-passed plan was handed back as items to run: ' + JSON.stringify(payload.todo_unfinished));
    }

    // 4. Compaction: after each successful child compaction the child got one
    // brief line (a host-scoped agent/pre-step reaches native children) and its
    // next todo_status still carries the brief; the root's post-compaction State
    // snapshot lists the plans.
    const compactions = events.filter(e => e.session === full && e.event.type === 'compaction/end' && !e.event.data.error).map(e => e.event.seq);
    assert.ok(compactions.length, 'child never compacted\n' + diagnostics());
    const briefs = events.filter(e => e.session === full && e.event.type === 'user/message' && e.event.data.source?.kind === todoBriefSource);
    compactions.forEach((seq, index) => assert.equal(briefs.filter(b => b.event.seq > seq && b.event.seq < (compactions[index + 1] ?? Infinity)).length, 1,
      'expected exactly one brief after child compaction ' + seq));
    assert.equal(briefs.length, compactions.length);
    for (const brief of briefs) {
      assert.match(brief.event.data.content[0].text, /^todo brief: Write a\.txt and b\.txt for execution\.todo-full \/ Next: test/);
      assert.doesNotMatch(brief.event.data.content[0].text, /\n/);
    }
    assert.ok(requests.some(r => r.session === full && !r.purpose && JSON.stringify(r.messages).includes('todo brief:')), 'brief never reached the child model');
    assert.match(toolJson(result('full-todo_status-after-compact').event).brief, /Next: test/);
    assert.ok(!events.some(e => e.session === partial && e.event.data.source?.kind === todoBriefSource), 'brief without a compaction');
    assert.ok(events.some(e => e.session === 'fixture-root' && e.event.type === 'compaction/end' && !e.event.data.error), 'root never compacted');
    const snapshots = events.filter(e => e.session === 'fixture-root' && e.event.type === 'user/message' && e.event.data.source?.kind === stateInjectionSource);
    const state = s => JSON.parse(s.event.data.content[0].text.split('\n').slice(1).join('\n'));
    assert.equal(state(snapshots[0]).todo, undefined, 'bootstrap snapshot predates any plan');
    const after = snapshots.filter(s => s.event.data.source.compaction_seq > 0).at(-1);
    assert.ok(after, 'no post-compaction snapshot');
    assert.deepEqual(state(after).todo, [
      { attempt_id: plans.b.attempt_id, node_key: 'execution.todo-partial', next: 'deliver', remaining_count: 1,
        ready_to_finish: false, child_alive: false },
    ]);
    assert.match(toolJson(result('root-todo_status-after-compact').event).brief, /Next: deliver/);

    // 5. Finish on the all-pass plan returns the bundle and report paths.
    const finish = toolJson(result('root-todo_finish-a').event);
    assert.equal(finish.refused, undefined, JSON.stringify(finish));
    assert.ok((await stat(finish.git_bundle.path)).size > 0); assert.ok((await stat(finish.report)).size > 0);
    assert.ok(finish.git_bundle.path.startsWith(join(directory, 'todo', 'finish', plans.a.attempt_id)));
    git(plans.a.workspace, 'bundle', 'verify', finish.git_bundle.path);

    // Root-only tools stay off the children's tool lists; worker tools are on both.
    const toolsOf = role => requests.find(r => r.role === role && !r.purpose && r.tools?.length).tools.map(t => t.name);
    for (const role of ['root', 'full', 'partial']) for (const name of ['todo_status', 'todo_begin', 'todo_done', 'todo_report'])
      assert.ok(toolsOf(role).includes(name), role + ' lacks ' + name);
    for (const name of ['todo_templates', 'todo_plan', 'todo_finish']) {
      assert.ok(toolsOf('root').includes(name));
      assert.ok(!toolsOf('full').includes(name) && !toolsOf('partial').includes(name), 'children see ' + name);
    }
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// The escape hatch for a plan whose child died mid-item. One driver boot serves
// all three properties, in the order they need: a plan stuck behind a child that
// DIED (the host restart case), a plan held by a child that is LIVE, and the
// dormancy refusal that only the takeover can clear.
// ---------------------------------------------------------------------------
const REBIND_PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-rebind-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.AITELIER_URL + path, { method: 'POST', body: JSON.stringify(body) });
const plans = JSON.parse(process.env.AITELIER_TEST_PLANS);
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
// The child that died already did the file work; the takeover only has to finish it.
const work = plan => tool('RB_CHILD_WORK', 'bash', { description: 'fixture work', run_in_background: false,
  command: "cd '" + plan.workspace + "' && printf a > a.txt && printf b > b.txt && git add -A && git -c user.name=fixture -c user.email=fixture@example.invalid commit -q -m work" });
const beginChild = (key, item) => tool('RB_CHILD_BEGIN-' + key + '-' + item, 'todo_begin', { attempt_id: plans[key].attempt_id, item_id: item });
const planStep = key => tool('root-todo_plan-' + key, 'todo_plan', plans[key].input);
const spawnStep = (key, marker) => tool('root-spawn-' + key, 'subagent', { description: marker + ' child', prompt: marker + ' attempt_id=' + plans[key].attempt_id });
const doneItem = (key, item) => tool('root-todo_done-' + key + '-' + item, 'todo_done', { attempt_id: plans[key].attempt_id, item_id: item });
const beginItem = (key, item) => tool('root-todo_begin-' + key + '-' + item, 'todo_begin', { attempt_id: plans[key].attempt_id, item_id: item });
const takeover = (key, session) => tool('root-todo_rebind-' + key, 'todo_rebind', { attempt_id: plans[key].attempt_id, item_id: 'implement', session_id: session });
const CHILDREN = {
  stuck: [beginChild('stuck', 'implement'), work(plans.stuck), { type: 'text', text: 'STUCK_CHILD_STOPS' }],
  died: [beginChild('died', 'implement'), work(plans.died), { type: 'text', text: 'DIED_CHILD_STOPS' }],
  // Begins the item, then stays alive until the process is killed: a live child.
  live: [beginChild('live', 'implement'), 'HOLD'],
};
export function apply(ctx) {
  const root = process.env.AITELIER_DSH_SESSION;
  const roles = new Map(), steps = new Map(), done = new Set();
  let liveChildStarted;
  const liveChild = new Promise(resolve => { liveChildStarted = resolve; });
  const roleOf = (session, text) => session === root ? 'root'
    : text.includes('TODO_CHILD_STUCK') ? 'stuck' : text.includes('TODO_CHILD_DIED') ? 'died'
      : text.includes('TODO_CHILD_LIVE') ? 'live' : 'other';
  const sessionOf = role => [...roles].find(([, value]) => value === role)?.[0];
  // Handbacks are read off the session events themselves: the model's message
  // history is JSON-escaped, and the dispatch prompt names the attempt too.
  const handbacks = [];
  const handed = attempt => handbacks.includes(attempt);
  // One step per director turn. A step's wait() holds that turn until the world
  // can answer it (the handback of the child that died, or a live child begun).
  const script = [
    { key: 'plan-stuck', block: () => planStep('stuck') },
    { key: 'spawn-stuck', block: () => spawnStep('stuck', 'TODO_CHILD_STUCK') },
    { key: 'status-stuck', wait: () => handed(plans.stuck.attempt_id),
      block: () => tool('root-todo_status-stuck', 'todo_status', { attempt_id: plans.stuck.attempt_id }) },
    { key: 'idle-blocked', block: () => tool('root-idle-blocked', 'idle_dormant', { reason: 'nothing authorized is available' }) },
    { key: 'rebind-stuck', block: () => takeover('stuck', sessionOf('stuck')) },
    { key: 'done-stuck-implement', block: () => doneItem('stuck', 'implement') },
    { key: 'begin-stuck-test', block: () => beginItem('stuck', 'test') },
    { key: 'done-stuck-test', block: () => doneItem('stuck', 'test') },
    { key: 'begin-stuck-deliver', block: () => beginItem('stuck', 'deliver') },
    { key: 'done-stuck-deliver', block: () => doneItem('stuck', 'deliver') },
    { key: 'finish-stuck', block: () => tool('root-todo_finish-stuck', 'todo_finish', { attempt_id: plans.stuck.attempt_id }) },
    { key: 'idle-after', block: () => tool('root-idle-after', 'idle_dormant', { reason: 'the stuck plan was taken over and finished' }) },
    { key: 'plan-died', block: () => planStep('died') },
    { key: 'spawn-died', block: () => spawnStep('died', 'TODO_CHILD_DIED') },
    { key: 'status-died', wait: () => handed(plans.died.attempt_id),
      block: () => tool('root-todo_status-died', 'todo_status', { attempt_id: plans.died.attempt_id }) },
    { key: 'rebind-died', block: () => takeover('died', sessionOf('died')) },
    { key: 'done-died-implement', block: () => doneItem('died', 'implement') },
    { key: 'begin-died-test', block: () => beginItem('died', 'test') },
    { key: 'done-died-test', block: () => doneItem('died', 'test') },
    { key: 'begin-died-deliver', block: () => beginItem('died', 'deliver') },
    { key: 'done-died-deliver', block: () => doneItem('died', 'deliver') },
    { key: 'finish-died', block: () => tool('root-todo_finish-died', 'todo_finish', { attempt_id: plans.died.attempt_id }) },
    { key: 'plan-live', block: () => planStep('live') },
    { key: 'spawn-live', block: () => spawnStep('live', 'TODO_CHILD_LIVE') },
    { key: 'rebind-live', wait: async () => { await liveChild; return true; },
      block: () => takeover('live', sessionOf('live')) },
    { key: 'done-live', block: () => doneItem('live', 'implement') },
    { key: 'scenario-done', block: () => ({ type: 'text', text: 'REBIND_SCENARIO_DONE' }) },
  ];
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 16000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      if (!roles.has(options.sessionId)) roles.set(options.sessionId, roleOf(options.sessionId, text));
      const role = roles.get(options.sessionId);
      const step = steps.get(options.sessionId) ?? 0;
      steps.set(options.sessionId, step + 1);
      await post('/test/request', { session: options.sessionId, role, purpose: options.purpose ?? null, tools: options.tools });
      let blocks;
      if (options.purpose === 'compaction') blocks = [{ type: 'text', text: 'Checkpoint: continue the plan.' }];
      else if (!options.tools?.length) blocks = [{ type: 'text', text: 'Fixture session' }];
      else if (role === 'root') {
        const next = script.find(candidate => !done.has(candidate.key));
        if (!next) blocks = [{ type: 'text', text: 'ROOT_DONE' }];
        else if (next.wait && !(await next.wait(text))) blocks = [{ type: 'text', text: 'Director idle.' }];
        else { done.add(next.key); blocks = [next.block()]; }
        if (next?.key === 'scenario-done') void post('/test/scenario-done', {});
      } else if (CHILDREN[role]) {
        const entry = CHILDREN[role][step];
        if (entry === 'HOLD') { liveChildStarted(); await post('/test/hold', {}); blocks = [{ type: 'text', text: 'LIVE_CHILD_RELEASED' }]; }
        else blocks = [entry ?? { type: 'text', text: role.toUpperCase() + '_CHILD_IDLE' }];
      } else blocks = [{ type: 'text', text: 'Unexpected session' }];
      for (const [index, block] of blocks.entries()) {
        yield { type: 'block-start', index, blockType: block.type };
        if (block.type === 'text') yield { type: 'text-delta', index, text: block.text };
        else yield { type: 'tool-call-delta', index, id: block.id, name: block.name, argumentsDelta: block.arguments };
        yield { type: 'block-end', index, block };
      }
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } };
      yield { type: 'finish', reason: { kind: blocks.at(-1).type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  ctx.on('session/event', (session, event) => {
    if (['tool/result', 'user/message', 'compaction/end'].includes(event.type)) void post('/test/event', { session: session.id, event });
    const body = event.type === 'user/message' ? (event.data?.content ?? []).map(part => part.text ?? '').join('') : '';
    if (!body.startsWith('AItelier todo handback:')) return;
    try { handbacks.push(JSON.parse(body.slice('AItelier todo handback:'.length).trim()).attempt_id); } catch { /* not a handback */ }
  });
}
`;

let rebindRun;
/** One boot for the three rebind tests below; a failing boot fails all three. */
const rebindScenario = async t => (rebindRun ??= await bootRebindScenario(t));

async function bootRebindScenario(t) {
  const entry = await measuredEntryForTest(t);
  if (!entry) return null;                  // nothing pinned is installed: reported unmeasured
  const directory = await mkdtemp(join(homedir(), 'dsh-todo-rebind-'));
  // Unique per run: idle_dormant's background-process scan matches processes by
  // session, and the other real-DSH test files run their own drivers in parallel.
  const session = 'rebind-root-' + process.pid + '-' + Math.random().toString(36).slice(2, 8);
  const here = dirname(fileURLToPath(import.meta.url));
  const requests = [], events = [], planStates = {}, held = new Set();
  let finished = false, liveChildHeld = false, output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url === '/api/state/query/list_claims') return send({ claims: [] });
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture', event_seq: 10,
      nodes: [{ name: 'n', status: 'PENDING' }], project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'fixture driver skill' });
    if (req.url.endsWith('/activity') || req.url.endsWith('/heartbeat')) return send({ ok: true });
    // The live child's held reply: it began its item and keeps its own turn open,
    // which is the signal the director's takeover step waits for.
    if (req.url === '/test/hold') { liveChildHeld = true; held.add(res); res.on('close', () => held.delete(res)); return; }
    // Parked long-poll: closed with the server.
    if (req.url === '/api/state/query/wait_for_state_change') {
      held.add(res); res.on('close', () => held.delete(res)); return;
    }
    if (req.url === '/test/request') { requests.push(body); return send({ ok: true }); }
    if (req.url === '/test/event') { events.push(body); return send({ ok: true }); }
    if (req.url === '/test/scenario-done') { finished = true; return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const cli = await fakeCli(join(directory, 'cli'));
    const plans = {};
    for (const [key, node] of [['stuck', 'execution.rebind-stuck'], ['died', 'execution.rebind-died'], ['live', 'execution.rebind-live']]) {
      const path = join(directory, 'work', key);
      const input = planInput('attempt-rebind-' + key, node, await workspace(path), path);
      plans[key] = { attempt_id: input.attempt_id, workspace: path, input };
    }
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-rebind-provider.mjs'), REBIND_PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { id: 'tool-subagent', config: { provider: 'spawn', toolName: 'subagent', agentOptions: { provider: 'fixture', model: 'worker' } } },
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-rebind-provider.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutCallerMode(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: session,
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_DSH_EXECUTION: 'host', DSH_PERMISSION_MODE: 'danger-full-access', AITELIER_DSH_IDLE_REVIEW_MS: '60000',
      AITELIER_TODO_PYTHON: cli.python, AITELIER_TODO_CWD: cli.cwd, AITELIER_TEST_PLANS: JSON.stringify(plans) } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);
    const trace = () => JSON.stringify(requests.map(r => [r.role, r.purpose])) + '\n'
      + JSON.stringify(events.map(e => [e.session === session ? 'root' : e.session.slice(0, 8), e.event.type,
        e.event.data?.message?.toolCallId ?? ''])) + '\n' + output.slice(-4000);
    // The scenario is bounded by the progress it OBSERVES, not by the clock (see
    // progress.mjs). The fixture drives one director step per turn and every step
    // is a tool call the runtime must record, so the milestones below are the
    // takeover flow itself: the dead child's item taken over and finished, the
    // live child's plan refused, dormancy refused around the finish. A loaded
    // host could expire the old 60 s budget while the director was still walking
    // that flow and report only "the rebind scenario did not finish". Only
    // forward signals are listed: activity posts, heartbeats, "Director idle."
    // turns and a repeated pending review advance none of them, so a runtime
    // whose watched progress stops still reds - and when a child DIES instead of
    // finishing, the red names the step that never happened.
    const handbackFor = id => events.some(e => {
      const text = e.event.type === 'user/message' ? (e.event.data?.content?.[0]?.text ?? '') : '';
      if (!text.startsWith('AItelier todo handback:')) return false;
      try { return JSON.parse(text.slice('AItelier todo handback:'.length).trim()).attempt_id === id; } catch { return false; }
    });
    const recorded = id => events.some(e => e.event.type === 'tool/result' && e.event.data.message?.toolCallId === id);
    const progress = await watchProgress({
      what: 'the rebind scenario to be driven end to end (a dead child\'s item taken over and finished, a live child\'s plan refused, dormancy refused before the takeover and cleared after it)',
      running: () => proc.exitCode === null,
      milestones: [
        { name: 'the director froze the stuck plan', met: () => recorded('root-todo_plan-stuck') },
        { name: 'the stuck native child began its item', met: () => recorded('RB_CHILD_BEGIN-stuck-implement') },
        { name: 'the stuck child stopped and the runtime handed its plan back', met: () => handbackFor(plans.stuck.attempt_id) },
        { name: 'dormancy refused the unfinished stuck plan', met: () => recorded('root-idle-blocked') },
        { name: 'the takeover of the dead child\'s item succeeded', met: () => recorded('root-todo_rebind-stuck') },
        { name: 'the taken-over plan was finished', met: () => recorded('root-todo_finish-stuck') },
        { name: 'dormancy stopped refusing the finished plan', met: () => recorded('root-idle-after') },
        { name: 'the director froze the died plan', met: () => recorded('root-todo_plan-died') },
        { name: 'the died native child began its item', met: () => recorded('RB_CHILD_BEGIN-died-implement') },
        { name: 'the died child was handed back', met: () => handbackFor(plans.died.attempt_id) },
        { name: 'the died plan was taken over', met: () => recorded('root-todo_rebind-died') },
        { name: 'the died plan was finished', met: () => recorded('root-todo_finish-died') },
        { name: 'the director froze the live plan', met: () => recorded('root-todo_plan-live') },
        { name: 'the live native child began its item and held its turn open', met: () => recorded('RB_CHILD_BEGIN-live-implement') && liveChildHeld },
        { name: 'the takeover of the live child\'s plan was refused', met: () => recorded('root-todo_rebind-live') },
        { name: 'the director\'s edit of the live plan was refused', met: () => recorded('root-todo_done-live-implement') },
        { name: 'the scenario reported itself complete', met: () => finished },
      ],
    });
    // A red names the milestone that never arrived and the last progress it saw,
    // then the request/event trace - the old "scenario did not finish" named
    // neither, and said nothing at all about a child that died rather than stalled.
    assert.ok(progress.done, `${progress.message}\n` + trace());
    assert.equal(proc.exitCode, null, output);
    for (const key of Object.keys(plans)) {
      planStates[key] = JSON.parse(await readFile(join(directory, 'todo', plans[key].attempt_id + '.json'), 'utf8'));
    }
    const roles = new Map(requests.map(r => [r.session, r.role]));
    const sessionOf = role => [...roles].find(([, value]) => value === role)?.[0];
    const sessions = { stuck: sessionOf('stuck'), died: sessionOf('died'), live: sessionOf('live') };
    assert.ok(sessions.stuck && sessions.died && sessions.live && !Object.values(sessions).includes(session),
      'the fixture children did not run in their own sessions: ' + JSON.stringify(sessions));
    const result = id => events.find(e => e.event.type === 'tool/result' && e.event.data.message.toolCallId === id);
    const json = id => {
      const found = result(id);
      assert.ok(found, 'no tool result was recorded for ' + id + '\n' + trace());
      return JSON.parse(toolText(found.event));
    };
    const toolsOf = role => requests.find(r => r.role === role && !r.purpose && r.tools?.length)?.tools.map(t => t.name) ?? [];
    return { session, sessions, plans, planStates, requests, events, result, json, toolsOf };
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of held) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
}

// These three read one shared boot, and that boot is bounded by the progress it
// observes (see bootRebindScenario), never by a harness timeout: their own bodies
// only assert what the boot already watched arrive, so a wall budget here would
// bound nothing but the host's load.
test('todo_rebind: a director takes over an item begun by a session that died, then done and finish succeed', async (t) => {
  const run = await rebindScenario(t);
  if (!run) return;
  const died = run.sessions.died;
  // The plan really was stuck behind the dead child: bound to it, item already begun.
  const before = run.json('root-todo_status-died');
  assert.equal(before.child_session, died, 'the fixture plan was not bound to the dead child: ' + JSON.stringify(before));
  assert.ok(before.remaining.includes('implement'), JSON.stringify(before));
  const rebind = run.json('root-todo_rebind-died');
  assert.equal(rebind.refused, undefined, 'the director could not take the plan over: ' + JSON.stringify(rebind));
  assert.equal(rebind.child_session, run.session);
  assert.equal(rebind.rebound_from, died);
  assert.equal(rebind.taken_over_by, run.session);
  for (const item of ['implement', 'test', 'deliver']) {
    const done = run.json('root-todo_done-died-' + item);
    assert.equal(done.refused, undefined, item + ': ' + JSON.stringify(done));
    assert.equal(done.outcome, 'pass', item);
  }
  const finish = run.json('root-todo_finish-died');
  assert.equal(finish.refused, undefined, 'the finished plan was refused: ' + JSON.stringify(finish));
  assert.ok(finish.git_bundle.path && finish.report, JSON.stringify(finish));
  // The plan file records the handover, so a later reader sees it changed hands.
  assert.equal(run.planStates.died.rebound_from, died);
  assert.equal(run.planStates.died.child_session, run.session);
  assert.deepEqual([...run.planStates.died.done].sort(), ['deliver', 'implement', 'test']);
});

test('todo_rebind: a live child keeps its plan - the takeover refuses and done still says bound_to_other_session', async (t) => {
  const run = await rebindScenario(t);
  if (!run) return;
  const live = run.sessions.live;
  const refused = run.json('root-todo_rebind-live');
  assert.equal(refused.refused, 'live_session', 'a live child was taken over: ' + JSON.stringify(refused));
  assert.equal(run.planStates.live.child_session, live, 'a live child lost its plan');
  assert.equal(run.planStates.live.rebound_from, undefined);
  assert.equal(run.result('root-todo_done-live-implement').session, run.session);
  const done = run.json('root-todo_done-live-implement');
  assert.equal(done.refused, 'bound_to_other_session', 'the director edited a live child\'s plan: ' + JSON.stringify(done));
  // Root-only, like todo_finish: children never see the takeover.
  assert.ok(run.toolsOf('root').includes('todo_rebind'), 'the director was not offered todo_rebind');
  for (const role of ['stuck', 'died', 'live']) assert.ok(!run.toolsOf(role).includes('todo_rebind'), role + ' child sees todo_rebind');
});

test('todo_rebind: dormancy refuses unfinished_plans before the takeover and stops refusing it after', async (t) => {
  const run = await rebindScenario(t);
  if (!run) return;
  const id = run.plans.stuck.attempt_id;
  const blocked = run.json('root-idle-blocked');
  assert.equal(blocked.refused, 'unfinished_plans', 'dormancy was not refused by the stuck plan: ' + JSON.stringify(blocked));
  assert.ok((blocked.attempts ?? []).includes(id), JSON.stringify(blocked));
  // The takeover sits between the two calls and is what clears the refusal.
  assert.equal(run.json('root-todo_rebind-stuck').refused, undefined);
  assert.equal(run.json('root-todo_finish-stuck').refused, undefined);
  const after = run.json('root-idle-after');
  assert.notEqual(after.refused, 'unfinished_plans', 'dormancy still refuses the finished plan: ' + JSON.stringify(after));
  assert.ok(!(after.attempts ?? []).includes(id), JSON.stringify(after));
});

test('CLI results: a refusal is returned with the brief, a missing brief is filled from status, a crash throws', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-todo-cli-'));
  try {
    const cli = await fakeCli(join(directory, 'cli'));
    const path = join(directory, 'work');
    const plan = planInput('attempt-unit', 'execution.unit', await workspace(path), path);
    const todo = new TodoCli({ python: cli.python, cwd: cli.cwd, stateDir: directory });
    const typo = await todo.call(plan.attempt_id, createArgs({ ...plan, params: { ...plan.params, test_timeout: 'soon' } }));
    assert.ok(typo.refused); assert.match(JSON.stringify(typo), /timeout/); assert.equal(typo.brief, null);
    const created = await todo.call(plan.attempt_id, createArgs(plan));
    assert.match(created.plan_hash, /^[0-9a-f]{64}$/); assert.match(created.brief, /Next: implement/);
    const refused = await todo.call(plan.attempt_id, ['begin', '--attempt', plan.attempt_id, '--item', 'test', '--session', 's1']);
    assert.ok(refused.refused); assert.match(refused.brief, /Next: implement/);
    const absent = await todo.call('attempt-missing', ['status', '--attempt', 'attempt-missing']);
    assert.ok(absent.refused); assert.equal(absent.brief, null);
    const crash = await crashCli(join(directory, 'crash'));
    await assert.rejects(new TodoCli({ python: crash.python, cwd: crash.cwd, stateDir: directory }).run(['status', '--attempt', 'x']), /todo_cli status/);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('CLI bounds: done/finish get the plan budget plus a minute; a timeout or abort stops the process group (SIGTERM, then SIGKILL)', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-todo-bound-'));
  const alive = pid => { try { process.kill(pid, 0); return true; } catch { return false; } };
  let sleeper;
  try {
    const cli = await fakeCli(join(directory, 'cli'));
    const path = join(directory, 'work');
    const plan = planInput('attempt-bound', 'execution.bound', await workspace(path), path);
    const todo = new TodoCli({ python: cli.python, cwd: cli.cwd, stateDir: directory });
    await todo.call(plan.attempt_id, createArgs(plan));
    // Bounds follow whatever budget the configured CLI (fake or real) reports.
    const { result: status } = await todo.run(['status', '--attempt=' + plan.attempt_id]);
    assert.ok(status.timeout_budget_seconds.test > 0 && status.finish_budget_seconds > 0, JSON.stringify(status));
    assert.equal(await todo.bound(plan.attempt_id, s => s.timeout_budget_seconds?.test), (status.timeout_budget_seconds.test + 60) * 1000);
    assert.equal(await todo.bound(plan.attempt_id, s => s.finish_budget_seconds), (status.finish_budget_seconds + 60) * 1000);
    assert.equal(await todo.bound(plan.attempt_id, s => s.no_such_budget), 60000);
    // Process handling is the runtime's, so it always runs against the fake's hanging items.
    const fake = new TodoCli({ python: 'python3', cwd: join(directory, 'cli'), stateDir: join(directory, 'fake') });
    await fake.call(plan.attempt_id, createArgs(plan));
    for (const stop of ['timeout', 'abort', 'stubborn']) {
      await rm(join(directory, 'fake', 'todo', 'hang.pid'), { force: true });
      const controller = new AbortController();
      const pending = fake.run(['done', '--attempt=' + plan.attempt_id, '--item=' + (stop === 'stubborn' ? 'stubborn' : 'hang')],
        controller.signal, stop === 'abort' ? 60000 : 1500);
      while (!(sleeper = Number(await readFile(join(directory, 'fake', 'todo', 'hang.pid'), 'utf8').catch(() => 0)))) await new Promise(r => setTimeout(r, 50));
      assert.ok(alive(sleeper));
      if (stop === 'abort') controller.abort();
      const stopped = Date.now();
      await assert.rejects(pending, stop === 'abort' ? /aborted; its process group was stopped \(SIGTERM\)/
        : stop === 'timeout' ? /timed out after 1500 ms; its process group was stopped \(SIGTERM\)/
        : /timed out after 1500 ms; its process group was stopped \(SIGKILL\)/);
      // A surviving grandchild would hold the CLI's stdout open until its 30 s sleep ends.
      assert.ok(Date.now() - stopped < 10000, `the call only settled after the grandchild exited on its own (${stop})`);
      for (let i = 0; i < 40 && alive(sleeper); i++) await new Promise(r => setTimeout(r, 50));
      assert.equal(alive(sleeper), false, `the predicate's grandchild survived the ${stop}`);
    }
  } finally {
    if (sleeper) try { process.kill(sleeper, 'SIGKILL'); } catch {}
    await rm(directory, { recursive: true, force: true });
  }
});

test('idle review and snapshot views: unfinished plans without a live child, and render() only lists plans when there are any', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-todo-views-'));
  try {
    const cli = await fakeCli(join(directory, 'cli'));
    const todo = new TodoCli({ python: cli.python, cwd: cli.cwd, stateDir: directory });
    assert.deepEqual(await todo.plans(), [], 'no store yet is no plans, not an error');
    for (const key of ['open', 'done']) {
      const path = join(directory, 'work-' + key);
      const plan = planInput('attempt-' + key, 'execution.' + key, await workspace(path), path);
      await todo.call(plan.attempt_id, createArgs(plan));
      await todo.call(plan.attempt_id, ['begin', '--attempt', plan.attempt_id, '--item', 'implement', '--session', 'child-' + key]);
      if (key === 'done') {
        await writeFile(join(path, 'a.txt'), 'a'); await writeFile(join(path, 'b.txt'), 'b');
        git(path, 'add', 'a.txt', 'b.txt'); git(path, '-c', 'user.name=f', '-c', 'user.email=f@example.invalid', 'commit', '-q', '-m', 'ab');
        for (const item of ['implement', 'test', 'deliver']) {
          await todo.call(plan.attempt_id, ['begin', '--attempt', plan.attempt_id, '--item', item, '--session', 'child-done']);
          assert.equal((await todo.call(plan.attempt_id, ['done', '--attempt', plan.attempt_id, '--item', item])).outcome, 'pass');
        }
      }
    }
    const plans = await todo.plans();
    assert.deepEqual(plans.map(p => p.attempt_id), ['attempt-done', 'attempt-open']);
    // attempt-done walked every item but nobody ran todo_finish, so it is work
    // waiting on the director - carried with the explicit marker, and with no item
    // offered as remaining.
    assert.deepEqual(plans.map(p => p.finish_reported), [false, false]);
    assert.deepEqual(unfinishedWithoutChild(plans, new Set()), [
      { attempt_id: 'attempt-done', node_key: 'execution.done', next: null, remaining: [], ready_to_finish: true },
      { attempt_id: 'attempt-open', node_key: 'execution.open', next: 'implement', remaining: ['implement', 'test', 'deliver'], ready_to_finish: false }]);
    // A live child still keeps its own plan out of the handback - including the one
    // whose items all passed: nothing is handed back for a living session.
    assert.deepEqual(unfinishedWithoutChild(plans, new Set(['child-open'])), [
      { attempt_id: 'attempt-done', node_key: 'execution.done', next: null, remaining: [], ready_to_finish: true }]);
    assert.deepEqual(todoSummary(plans, new Set(['child-open'])), [
      { attempt_id: 'attempt-done', node_key: 'execution.done', next: null, remaining_count: 0, ready_to_finish: true, child_alive: false },
      { attempt_id: 'attempt-open', node_key: 'execution.open', next: 'implement', remaining_count: 3, ready_to_finish: false, child_alive: true }]);
    let listed = [];
    const runtime = new DriverRuntime({ project: 'alpha', driver: 'driver-a', directory,
      request: async () => ({ nodes: [], project_note: {}, private_note: {} }), todoPlans: async () => listed });
    await runtime.refresh();
    assert.equal(JSON.parse(runtime.render()).todo, undefined);
    listed = todoSummary(plans, new Set());
    await runtime.refresh();
    assert.deepEqual(JSON.parse(runtime.render()).todo, listed);
    assert.equal(JSON.parse(runtime.render()).todo_error, undefined);
    // A broken todo CLI degrades the snapshot; it never fails a State refresh.
    const brokenCli = await crashCli(join(directory, 'broken'));
    const broken = new DriverRuntime({ project: 'alpha', driver: 'driver-a', directory,
      request: async path => path.endsWith('/me') ? { driver_id: 'driver-a' } : { event_seq: 1, nodes: [], project_note: {}, private_note: {} },
      todoPlans: () => new TodoCli({ python: brokenCli.python, cwd: brokenCli.cwd, stateDir: directory }).plans() });
    await broken.init();
    const degraded = JSON.parse(broken.render());
    assert.equal(degraded.todo, undefined); assert.match(degraded.todo_error, /todo_cli list/);
    await assert.rejects(todo.run(['list'], undefined, 1), /todo_cli list/, 'a CLI call past its timeout is an error, not a hang');
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('prompts: dispatch picks a template and binds params, adds templates by reviewed change, and no longer prefers pipelines', async () => {
  const goal = driverGoal('alpha'), driver = await readFile(new URL('./driver.md', import.meta.url), 'utf8');
  for (const text of [goal, driver]) {
    assert.match(text, /todo_templates/); assert.match(text, /todo_plan/); assert.match(text, /params/);
    assert.match(text, /todo_templates\//, 'adding a template under todo_templates/ is not named');
    assert.match(text, /todo_status/); assert.match(text, /todo_finish/);
  }
  assert.match(goal, /经过评审的代码改动/); assert.match(driver, /reviewed code\s+change/);
  assert.doesNotMatch(goal, /已有适用的 pipeline|没有适用 pipeline/);
  assert.doesNotMatch(driver, /Delegate bounded work to DSH subagent or an AItelier pipeline/);
});

// iss-92653d46882441de: the tool shell drops any environment name containing KEY, so git's indexed
// config arrives as COUNT=1 with no KEY_0 and every bare git command dies before it runs.
test('the indexed git config environment is removed before anything can inherit it', async () => {
  const { dropIndexedGitConfig } = await import('./runtime.mjs');
  const env = {
    PATH: '/usr/bin',
    GIT_CONFIG_COUNT: '1',
    GIT_CONFIG_KEY_0: 'credential.helper',
    GIT_CONFIG_VALUE_0: '/some/helper.sh',
    GIT_CONFIG_KEY_1: 'user.name',
    GIT_CONFIG_VALUE_1: 'someone',
    GIT_CONFIG_GLOBAL: '/tmp/keep-me',
    GIT_TOKENLIKE_UNRELATED: 'keep',
  };
  dropIndexedGitConfig(env);
  assert.deepEqual(Object.keys(env).sort(), ['GIT_CONFIG_GLOBAL', 'GIT_TOKENLIKE_UNRELATED', 'PATH']);
  // A clean environment is the whole point: with COUNT gone git reads ~/.gitconfig and parses.
  assert.equal(env.GIT_CONFIG_COUNT, undefined);
});
