/** Model-free provider used only by runtime.roles.test.mjs.
 *
 * Per boot (AITELIER_TEST_BOOT): the director's first tool-bearing request spawns one
 * native child; the child tries `subagent` through call_tool, then by its direct
 * name, then `job_kill` through call_tool, and finishes with a boot-stamped line;
 * every other director request answers with text. Every request is posted to the
 * test server, so the test reads the real tool lists and system prompts. */
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-roles-provider';
export const inject = ['llm'];

const post = (path, body) => fetch(process.env.AITELIER_URL + path, { method: 'POST', body: JSON.stringify(body) });

export function apply(ctx) {
  const boot = process.env.AITELIER_TEST_BOOT;
  let spawned = false;
  const childCalls = [
    ['call_tool', { name: 'subagent', arguments: { description: 'nested', prompt: 'nested' } }],
    ['subagent', { description: 'nested', prompt: 'nested' }],
    ['call_tool', { name: 'job_kill', arguments: { job_id: 'fixture-none' } }],
  ];
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 400000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const child = options.sessionId !== process.env.AITELIER_DSH_SESSION;
      await post('/test/request', { options, child, boot, purpose: options.purpose ?? null });
      let block;
      if (options.purpose || !options.tools?.length) block = { type: 'text', text: 'Fixture text' };
      else if (child) {
        const step = options.messages.filter(m => m.role === 'assistant').length;
        block = step < childCalls.length
          ? { type: 'tool-call', id: `roles-child-${boot}-${step}`, name: childCalls[step][0], arguments: JSON.stringify(childCalls[step][1]) }
          : { type: 'text', text: 'ROLES_CHILD_DONE boot ' + boot };
      } else if (!spawned) {
        spawned = true;
        block = { type: 'tool-call', id: 'roles-spawn-' + boot, name: 'subagent',
          arguments: JSON.stringify({ description: 'roles child', prompt: 'ROLES_CHILD_TASK boot ' + boot }) };
      } else block = { type: 'text', text: 'ROLES_ROOT_IDLE boot ' + boot };
      yield { type: 'block-start', index: 0, blockType: block.type };
      if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text };
      else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments };
      yield { type: 'block-end', index: 0, block };
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } };
      yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
}
