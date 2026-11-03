/** Model-free provider used only by runtime.smoke.test.mjs. */
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-provider';
export const inject = ['llm'];
export function apply(ctx) {
  let rootCalls = 0, pressure = false, triggered = false, childMessaged = false, childShell = false, childWork = 0;
  const host = process.env.AITELIER_DSH_EXECUTION === 'host';
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 16000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      const last = JSON.stringify(options.messages.at(-1));
      const summary = /compaction engine|structured checkpoint|Condense the conversation/i.test(last);
      const child = options.messages.some(m => m.role === 'user' && JSON.stringify(m.content).includes('CHILD_TASK_MARKER'));
      await fetch(process.env.AITELIER_URL + '/test/request', { method: 'POST', body: JSON.stringify({ options, summary, child }) });
      let block;
      if (summary) block = { type: 'text', text: 'Checkpoint: continue the authorized task; earlier details can be searched.' };
      else if (child && !childMessaged) {
        childMessaged = true;
        block = { type: 'tool-call', id: 'fixture-child-message', name: 'send_message', arguments: JSON.stringify({ agent_id: process.env.AITELIER_DSH_SESSION, message: 'CHILD_MESSAGE_MARKER' }) };
      }
      else if (child && !childShell) {
        childShell = true;
        const outside = process.env.AITELIER_TEST_OUTSIDE_CWD;
        const git = host ? `git init -q --bare '${outside}/origin.git' && git init -q '${outside}/worker' && cd '${outside}/worker' && git config user.name fixture && git config user.email fixture@example.invalid && printf host > child.txt && git add child.txt && git commit -q -m child && git remote add origin '${outside}/origin.git' && git push -q origin HEAD:refs/heads/main && git fetch -q origin main && git merge --ff-only FETCH_HEAD && git rev-parse HEAD > '${outside}/host-git-proof.txt'` : `git init -q . && git add worker-proof.txt && git -c user.name=fixture -c user.email=fixture@example.invalid commit -q -m 'native workspace fixture' && git rev-parse HEAD > commit-proof.txt`;
        block = { type: 'tool-call', id: 'fixture-child-shell', name: 'bash', arguments: JSON.stringify({ description: 'Verify inherited workspace and owned checkout writes', command: `if printf allowed > '${outside}/outside-probe.txt'; then printf OUTSIDE_WRITE_ALLOWED; else printf OUTSIDE_WRITE_DENIED; fi; printf 'WORKER_SHELL_OK' > worker-proof.txt; cat worker-proof.txt; ${git}`, run_in_background: false }) };
      }
      else if (child && !host && childWork < 4) {
        const steps = [['open', {}], ['write', {path:'child.txt',content:'native child',create:true}],
          ['bash', {command:'python3 -m pytest -q test_child.py'}], ['deliver', {commit_message:'native child change'}]];
        const [action, args] = steps[childWork++];
        block = { type:'tool-call', id:'fixture-child-work-' + action, name:'project_work',
          arguments:JSON.stringify({attempt_id:'attempt-fixture',fence:1,action,arguments:args}) };
      }
      else if (child) {
        await fetch(process.env.AITELIER_URL + '/test/parent-waiting');
        block = { type: 'text', text: 'Native child completed.' };
      }
      else if (!options.tools?.length) block = { type: 'text', text: 'Fixture session' };
      else {
        const rawCall = rootCalls++;
        const call = host ? rawCall - 1 : rawCall;
        if (host && rawCall === 0) block = {type:'tool-call',id:'fixture-root-host-shell',name:'bash',arguments:JSON.stringify({description:'Verify migrated root host access',command:`printf ROOT_HOST_OK > '${process.env.AITELIER_TEST_OUTSIDE_CWD}/root-host-proof.txt'` + (process.env.AITELIER_TEST_DOCKER ? ` && docker version --format '{{.Server.Version}}' > '${process.env.AITELIER_TEST_OUTSIDE_CWD}/docker-proof.txt'` : ''),run_in_background:false})};
        else if (call === 0) block = { type: 'tool-call', id: 'fixture-pipelines', name: 'mcp__aitelier__list_pipelines', arguments: '{}' };
        else if (call === 1) block = { type: 'tool-call', id: 'fixture-spawn', name: 'subagent', arguments: JSON.stringify({ description: 'smoke child', prompt: 'CHILD_TASK_MARKER', cwd: process.env.AITELIER_TEST_CHILD_CWD }) };
        else if (call === 2) { pressure = true; block = { type: 'text', text: 'HISTORY_BEFORE_COMPACT_ANCHOR ' + 'earlier context '.repeat(14000) }; }
        else if (call === 3) block = { type: 'tool-call', id: 'fixture-grep', name: 'grep_chat_history', arguments: JSON.stringify({ pattern: 'Resume the assigned State DAG' }) };
        else block = { type: 'tool-call', id: 'fixture-wait-' + call, name: 'wait', arguments: '{}' };
      }
      yield { type: 'block-start', index: 0, blockType: block.type };
      if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text };
      else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments };
      yield { type: 'block-end', index: 0, block };
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: block.type === 'text' && block.text.length > 10000 ? 30000 : 20 } };
      yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  ctx.on('agent/status', ({ agent, status }) => {
    if (agent.session.id === process.env.AITELIER_DSH_SESSION && status === 'idle' && pressure && !triggered) {
      triggered = true;
      void fetch(process.env.AITELIER_URL + '/test/change', { method: 'POST' });
    }
  });
  ctx.on('session/event', (session, event) => {
    if ((session.id === process.env.AITELIER_DSH_SESSION || event.data?.message?.toolCallId === 'fixture-child-shell') && ['compaction/end', 'surface/replace', 'tool/result'].includes(event.type)) {
      void fetch(process.env.AITELIER_URL + '/test/event', { method: 'POST', body: JSON.stringify(event) });
    }
  });
}
