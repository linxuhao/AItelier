/** AItelier's wording for dsh-director: the labels, texts and tool descriptions that
 *  name State, its tools and its MCP servers. Moved verbatim from runtime.mjs and
 *  tool-discovery.mjs (plugin design step 2), so this deployment's requests and its
 *  persisted message kinds are byte-for-byte what they were before the split. The
 *  generic plugin has its own neutral defaults (../dsh-director/lib/core.mjs). */
import { DORMANT_MIN_MINUTES, DORMANT_MAX_MINUTES } from '../dsh-director/lib/core.mjs';

export const stateInjectionSource = 'aitelier-driver-state';
/** Source kind for a State runtime event. Deliberately NOT `user`: a routine
 *  tick is not the owner speaking, and labelling it `user` made a driver hunt for
 *  a message that was never sent. */
export const runtimeEventSource = 'aitelier-runtime-event';
/** Source kind for the one-line plan brief a bound child gets after compaction. */
export const todoBriefSource = 'aitelier-todo-brief';
/** Source of the runtime's continuation question: not input, not a goal round, not a wake source. */
export const continueSource = 'aitelier-continue';

export const labels = Object.freeze({
  runtimeEvent: runtimeEventSource,
  continue: continueSource,
  todoBrief: todoBriefSource,
  context: stateInjectionSource,
  // The system clock line (message-time design 3.2): not input, never a wake source.
  clock: 'aitelier-clock',
  contextSection: 'aitelier:state',
  sectionPrefix: 'aitelier',
});

/** What a blocker that waits on the user must be: a question they can see. */
export const askTheUser = 'Anything that waits on your user (a ruling, an authorization, a hold only they can lift) must be an open question block they can see: check list_user_questions and ask, with send_user_message segments, each such decision that is not already open, before you answer NOTHING_LEFT or go dormant.';
export const continuationQuestion = 'AItelier continuation check: you ended a turn. Before parking, look for more work you can do now, in parallel with anything running: ready frontier nodes, open issues you can close or advance, next steps you promised yourself, results you can collect. If there is one, do it now. If not, answer with one line that starts with NOTHING_LEFT: naming why each candidate is blocked. For a long background job you started, call watch_process with its pid instead of polling it. ' + askTheUser;
export const reviewInstruction = 'Recheck live State frontier (ready nodes), open issues, next steps you promised yourself, results you can collect, worker capacity and prior blockers. Continue independent authorized work; a blocked task does not block the whole project. ' + askTheUser + ' If nothing is actionable, reply with one line that starts with NOTHING_LEFT: naming why each candidate is blocked; if nothing is in flight either, call idle_dormant with that reason and how long to sleep instead.';

export const discoveryTexts = Object.freeze({
  sectionName: 'aitelier:tool-discovery',
  section: 'MCP capabilities are available through search_tools(query) and call_tool(name, arguments). Search names/descriptions to retrieve only the relevant full parameter schemas; then use call_tool with the exact name and arguments. For AgentMCP image, OCR, PDF, audio or video work, for example search_tools({query:"agentmcp image",limit:3}), then call_tool({name:"<exact returned tool name>",arguments:{/* fields from its returned schema */}}). Search again after compaction if you need an earlier schema. Known names may be called without searching first. The everyday coding and coordination tools are listed directly; less-used ones (glob, web_fetch, web_search, interrupt_agent, goals, MCP resources and the rest) are not listed but are found with search_tools and called the same way. Discovery does not change your existing permissions.',
  searchDescription: 'Find available tools by name or description and return their parameter schemas (default 5, maximum 10). Includes the less-used native tools that are not listed (glob, web_fetch, web_search, interrupt_agent, goals, MCP resources) and the deferred MCP capabilities: State writes, pipelines, provider management, AgentMCP images/OCR/PDF/audio/video, devmcp and zvec. Use call_tool to invoke a result. The catalog follows live MCP updates and your own agent scope.',
});

export const texts = Object.freeze({
  askTheUser, continuationQuestion, reviewInstruction,
  reviewDue: 'AItelier director review due:\n',
  jobFinished: 'AItelier background job finished:\n',
  todoHandback: 'AItelier todo handback:\n',
  todoBatch: 'AItelier todo batch:\n',
  modelCompletion: 'AItelier model completion',
  contextHeader: 'AItelier driver state — startup / post-compaction snapshot:\n',
  toolDiscoverySection: discoveryTexts.section,
  heldWork: 'you hold live State claims',
  slotFree: 'AItelier worker slot free:\n',
  workersResumed: 'AItelier workers resumed after a restart:\n',
  wakesOn: ['State event', 'inbox message', 'owner message', 'child message', 'wake_after_minutes running out'],
  tools: {
    search_tools: discoveryTexts.searchDescription,
    todo_status: 'Read the frozen todo plan of a State attempt: brief, goal, next item, done and remaining items. Call it first, and again whenever you lost track (for example after compaction).',
    todo_plan: 'Director only. Freeze the todo plan of a State attempt before dispatching its native child, by expanding a repository template (see todo_templates) with params bound to that template\'s declared params. context_hash is the one start_external_attempt returned. A wrongly typed, missing or unknown param refuses the plan. Items cannot be supplied: when no template fits, add one under todo_templates/ through a reviewed code change.',
    todo_batch_plan: 'Director only. Freeze a todo BATCH for a State attempt from a kind: batch template (todo_templates lists find_verify): one find sub-plan per dimension now, one verify sub-plan per finding once every finder passed. Same arguments as todo_plan. dispatch holds the finder prompts, cut to the batch\'s max_parallel (concurrent finders; the rest are pending), each with its prompt_sha256: give each one verbatim to its own new native subagent, followed by a last line "prompt_sha256: <that sha>". A batch template refuses todo_plan, an ordinary template refuses this tool.',
    todo_finish: 'Director only. Re-evaluate every item of an attempt\'s plan; only when all pass, write the git bundle, its sha256 and the candidate report and return their paths for report_external_attempt. Otherwise it is refused with the failing items. For a batch attempt it re-runs nothing: every sub-plan must already have passed, and the candidate also names the fan-in summary (evidence: false - record evidence from each verify report).',
    idle_dormant: `Director only. Call when no authorized work is available and nothing is in flight: stops the 15-minute idle review (no model turns while nothing happens) and compacts a large context once. wake_after_minutes (${DORMANT_MIN_MINUTES}-${DORMANT_MAX_MINUTES}) is how long you choose to sleep: when it runs out with no input you get one review, so pick it from when something you wait on could have changed. You stay subscribed: any State event, inbox message, owner message or child message wakes you earlier and the review resumes. Before calling it, every decision you wait on from your user must already be an open question block (list_user_questions). Refused while you have a live child, a live State claim or an unfinished todo plan whose State attempt has not settled.`,
  },
});
