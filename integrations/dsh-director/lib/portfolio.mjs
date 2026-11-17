/** The steward's view across projects and its handoff (plugin step 12, design-steward 4).
 *  Registered only for what the host provides (host.d.ts v1.2 `portfolio`, `handoff`);
 *  which role is shown them is roles.json's business. Every read is the host's: this
 *  file adds no reading of its own, so the host's read rules are the only ones.
 *
 *  The steward's one inbox write is registered the same way, only when the host's
 *  portfolio acknowledges: a wake re-injects every unread delivery, so acknowledging
 *  one is the only thing that stops the same letters coming back. It is a write, not
 *  a read, so it is not part of PORTFOLIO_TOOLS. */

export const PORTFOLIO_TOOLS = ['portfolio_projects', 'portfolio_project', 'portfolio_conversation'];

/** @param portfolio host.portfolio: { projects(signal), project(id, signal), conversation(id, cursor, signal),
 *    acknowledge({ deliveryId, version }, signal) } - `acknowledge` is optional; without it the
 *    inbox acknowledgement is not registered.
 *  @param handoff host.handoff: ({ projectId, text, citesUserMessageId }, signal) => result */
export function registerPortfolioTools(scope, { defineTool, jsonOutput, portfolio, handoff }) {
  const projectId = { type: 'string', required: true };
  if (portfolio) {
    scope.tools.register(defineTool({ name: 'portfolio_projects',
      description: 'Every project your owner has: its id and title, whether its director is running, and how many of its questions to your owner are open. Read-only.',
      parameters: {}, output: jsonOutput,
      execute: (_args, exec) => portfolio.projects(exec.signal),
    }));
    scope.tools.register(defineTool({ name: 'portfolio_project',
      description: 'One project in detail: its overview (work items and their states), its open issues and its open questions to your owner. Read-only.',
      parameters: { project_id: projectId }, output: jsonOutput,
      execute: (args, exec) => portfolio.project(args.project_id, exec.signal),
    }));
    scope.tools.register(defineTool({ name: 'portfolio_conversation',
      description: 'One page of a project\'s conversation between your owner and its director, newest first; pass next_cursor to read older messages. Read-only.',
      parameters: { project_id: projectId, cursor: { type: 'string' } }, output: jsonOutput,
      execute: (args, exec) => portfolio.conversation(args.project_id, args.cursor, exec.signal),
    }));
    // One inbox write, and only for a delivery in the steward's own inbox: the wake
    // carries each delivery's id and version, and an acknowledged delivery's body is
    // withheld from later wakes. No resolve: standing guidance is the project's call.
    if (portfolio.acknowledge) scope.tools.register(defineTool({ name: 'portfolio_inbox_acknowledge',
      description: 'Acknowledge ONE delivery in your own inbox, so it stops being re-injected: pass the delivery_id and version the wake (or the inbox page) showed you. It is recorded under your own driver id; an acknowledged delivery\'s body is withheld from later wakes. It writes nothing else: no resolve, no other project, no other driver. The same delivery and version is the same request, so a retry is not a second acknowledgement.',
      parameters: { delivery_id: { type: 'string', required: true },
        version: { type: 'integer', required: true, description: 'the delivery version the wake showed you' } },
      output: jsonOutput,
      execute: (args, exec) => portfolio.acknowledge({ deliveryId: args.delivery_id, version: args.version }, exec.signal),
    }));
  }
  if (handoff) scope.tools.register(defineTool({ name: 'handoff',
    description: 'Pass what your owner said to a project\'s director, only because your owner asked for it: cites_user_message_id is the id of the owner\'s own message, which is quoted to the director with your text. You cannot instruct a director on your own; tell your owner what you passed on.',
    parameters: { project_id: projectId, text: { type: 'string', required: true }, cites_user_message_id: { type: 'string', required: true } },
    output: jsonOutput,
    execute: (args, exec) => handoff({ projectId: args.project_id, text: args.text, citesUserMessageId: args.cites_user_message_id }, exec.signal),
  }));
}
