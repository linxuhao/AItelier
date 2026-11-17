/** The steward's view across projects, its own inbox page and its handoff (plugin step 12,
 *  design-steward 4). Registered only for what the host provides (host.d.ts v1.2 `portfolio`,
 *  `handoff`), and the inbox reader only when the host's portfolio offers it; which role is
 *  shown them is roles.json's business. Every read is the host's: this file adds no reading of
 *  its own, so the host's read rules are the only ones.
 *
 *  The inbox reader exists because a wake carries only the first 240 characters of every
 *  message body: anything longer than that is unread until it is fetched whole. Its bounds are
 *  in its description, and the host states the same numbers back in every answer (`limits`), so
 *  a steward can tell what it was and was not shown. */

export const PORTFOLIO_TOOLS = ['portfolio_projects', 'portfolio_project', 'portfolio_conversation', 'portfolio_inbox'];

/** @param portfolio host.portfolio: { projects(signal), project(id, signal), conversation(id, cursor, signal),
 *    inbox(page, signal) } - `inbox` is optional; without it the reader is not registered.
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
    if (portfolio.inbox) scope.tools.register(defineTool({ name: 'portfolio_inbox',
      description: 'One page of the messages delivered to your own inbox, oldest delivery first, whole: each delivery\'s id, seq, message id, subject, sender, time, status and version, and its body. One call holds at most 5 deliveries, one body at most 8000 characters, and the whole answer is capped at 24000 characters: a longer body comes back with its true body_chars and body_truncated, and a longer page answers with matched_total, has_more and next_after, which you pass back as after for the next page. Read-only: nothing is acknowledged or changed.',
      parameters: { after: { type: 'integer', description: 'the last delivery_seq you have read; omit to start at the first delivery' },
        limit: { type: 'integer', description: 'deliveries to return; at most 5, and fewer when an answer would pass its cap' } },
      output: jsonOutput,
      execute: (args, exec) => portfolio.inbox({ after: args.after, limit: args.limit }, exec.signal),
    }));
  }
  if (handoff) scope.tools.register(defineTool({ name: 'handoff',
    description: 'Pass what your owner said to a project\'s director, only because your owner asked for it: cites_user_message_id is the id of the owner\'s own message, which is quoted to the director with your text. You cannot instruct a director on your own; tell your owner what you passed on.',
    parameters: { project_id: projectId, text: { type: 'string', required: true }, cites_user_message_id: { type: 'string', required: true } },
    output: jsonOutput,
    execute: (args, exec) => handoff({ projectId: args.project_id, text: args.text, citesUserMessageId: args.cites_user_message_id }, exec.signal),
  }));
}
