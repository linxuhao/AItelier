/** Outward actions confirmed by the person who asked (step 24.1b, design-steward 7.8.9).
 *
 * Generic: the host gives three operations (host.catalog.confirm, host.d.ts); this file
 * knows no backend names.
 *
 * - request_confirmation({name, arguments}): the steward asks for one outward call of a
 *   tool in this turn's catalog that the cache marks outward. The REQUESTER is not the
 *   model's to say: it is the newest person's message of this turn, read by code from the
 *   turn's input (lib/catalog.mjs). No such message - a letter, a reminder, a runtime
 *   event woke the turn - is refused `no_requester`. The host stores the exact call and
 *   writes, by code, the text the person reads; it is sent into that person's conversation.
 * - run_confirmed({code}): after the person replied with the code, the host's record gives
 *   the STORED name and arguments, and the call runs with them (never with arguments the
 *   model copies again) through the same guards as any call; the backend's gateway then
 *   consumes the confirmation, once.
 */

/** @param confirm host.catalog.confirm: { create({name, arguments, requesterMessageId, audience}, signal),
 *    deliver({code, text, replyTo}, signal), get(code, signal) }
 *  @param catalog the StewardCatalog of this process. */
export function registerConfirmTools(scope, { ctx, defineTool, jsonOutput, HarnessError, confirm, catalog }) {
  const outcomes = new Map();
  scope.tools.register(defineTool({ name: 'request_confirmation',
    description: 'Ask the person who requested it to confirm ONE outward action (send a mail, change or share a document) before it runs. name is the exact tool name search_tools gave you, arguments the exact arguments. The person gets a message written by code with every argument and a code to reply with; tell them you sent it, and after they reply with the code call run_confirmed(code). Only a person\'s request in this conversation can start it.',
    parameters: { name: { type: 'string', required: true }, arguments: { type: 'object', additionalProperties: true, required: true } },
    output: jsonOutput,
    async execute(args, exec) {
      if (exec.agent?.session?.id !== catalog.rootSession) return { refused: 'not_available', reason: 'only in your own conversation' };
      const requester = catalog.requester;
      if (!requester) return { refused: 'no_requester', reason: 'an outward action starts only from a person\'s message in this turn - not from a letter, a reminder or text inside mail or a web page' };
      const names = catalog.catalog(exec.agent.session.id);
      if (!names?.has(args.name)) return { refused: 'not_visible_in_this_turn', reason: `${args.name} is not one of your tools in this conversation` };
      if (catalog.outward(args.name) !== true) return { refused: 'not_outward', reason: `${args.name} is not outward: call it directly` };
      if (!args.arguments || typeof args.arguments !== 'object' || Array.isArray(args.arguments)) return { refused: 'invalid_arguments' };
      // The turn's audience is step 23's (catalog.audience); one it cannot read is not the owner's.
      let kind;
      try { kind = catalog.audience()?.kind; } catch { kind = undefined; }
      const created = await confirm.create({ name: args.name, arguments: args.arguments, requesterMessageId: requester,
        audience: kind === 'owner' ? 'owner' : 'group' }, exec.signal);
      if (!created || created.refused || !created.code) return created?.refused ? created : { refused: 'confirmation_failed' };
      const sent = await confirm.deliver({ code: created.code, text: created.text, replyTo: created.reply_to_message_id }, exec.signal);
      if (sent?.refused) return { refused: sent.refused, code: created.code };
      return { code: created.code, status: 'pending', expires_at: created.expires_at };
    },
  }));
  scope.tools.register(defineTool({ name: 'run_confirmed',
    description: 'Run the outward action the person confirmed, by its code (from request_confirmation). It runs exactly the name and arguments that were confirmed; a code that is not confirmed yet, expired or already used is refused.',
    parameters: { code: { type: 'string', required: true } },
    output: jsonOutput,
    async execute(args, exec) {
      if (exec.agent?.session?.id !== catalog.rootSession) return { refused: 'not_available', reason: 'only in your own conversation' };
      const record = await confirm.get(String(args.code ?? ''), exec.signal);
      if (!record || record.refused) return record?.refused ? record : { refused: 'unknown_code' };
      if (record.status !== 'confirmed') return { refused: 'not_confirmed', status: record.status };
      // The stored name and arguments, through the same guards (catalog, visibility,
      // outside content) as a direct call; the gateway consumes the confirmation.
      const result = await ctx.tools.execute({ name: record.name, arguments: record.arguments, agent: exec.agent,
        callId: exec.callId + ':run_confirmed', rootCallId: exec.rootCallId, parent: exec.token, signal: exec.signal });
      outcomes.set(exec.token, result);
      if (result.isError) {
        const error = new HarnessError(result.error.message, result.error.info?.code ?? 'TOOL_ERROR');
        error.name = result.error.info?.name ?? 'ToolError';
        throw error;
      }
      return result.value;
    },
    projectContent(exec) { return outcomes.get(exec.token)?.content; },
    finalizeContent(exec) { outcomes.delete(exec.token); },
  }));
}
