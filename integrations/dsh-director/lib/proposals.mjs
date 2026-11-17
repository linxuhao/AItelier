/** The steward's proposals (plugin step 13, design-steward 5): it proposes, its owner
 *  decides, the host executes what the owner approved. The steward holds no execution
 *  right: propose only records an open proposal. Registered only when the host provides
 *  `proposals` (host.d.ts v1.2). */

export const PROPOSAL_KINDS = ['create_project', 'start_driver', 'stop_driver'];

/** @param proposals host.proposals: { propose({kind, payload, reason}, signal), withdraw({id, reason}, signal) } */
export function registerProposalTools(scope, { defineTool, jsonOutput, proposals }) {
  scope.tools.register(defineTool({ name: 'propose',
    description: `Propose an action only your owner may approve; you never run it yourself. kind is one of ${PROPOSAL_KINDS.join(', ')}. payload: create_project {project_id, title, goal, seed_nodes: [{node_key, title, ...}]}; start_driver {project_id}; stop_driver {project_id}. reason says why, in your owner's terms. Your owner sees a card and approves, declines or asks you to change it; the result comes back to you as an event. To change an open proposal, propose again with supersedes = its id.`,
    parameters: { kind: { type: 'string', required: true, enum: PROPOSAL_KINDS }, payload: { type: 'object', additionalProperties: true, required: true },
      reason: { type: 'string', required: true }, supersedes: { type: 'string' } },
    output: jsonOutput,
    execute: (args, exec) => PROPOSAL_KINDS.includes(args.kind)
      ? proposals.propose({ kind: args.kind, payload: args.payload, reason: args.reason, ...(args.supersedes ? { supersedes: args.supersedes } : {}) }, exec.signal)
      : { refused: 'unknown_kind', kinds: PROPOSAL_KINDS },
  }));
  scope.tools.register(defineTool({ name: 'withdraw_proposal',
    description: 'Take back one of your open proposals (it stays visible as withdrawn), with the reason.',
    parameters: { id: { type: 'string', required: true }, reason: { type: 'string', required: true } }, output: jsonOutput,
    execute: (args, exec) => proposals.withdraw({ id: args.id, reason: args.reason }, exec.signal),
  }));
}
