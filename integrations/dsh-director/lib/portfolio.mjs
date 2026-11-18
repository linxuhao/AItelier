/** The steward's view across projects, its own inbox pages and its handoff (plugin step 12,
 *  design-steward 4). Registered only for what the host provides (host.d.ts v1.2 `portfolio`,
 *  `handoff`), and each inbox reader only when the host's portfolio offers it; which role is
 *  shown them is roles.json's business. Every read is the host's: this file adds no reading of
 *  its own, so the host's read rules are the only ones.
 *
 *  The inbox readers exist because a wake carries only the first 240 characters of every
 *  message body: anything longer than that is unread until it is fetched whole - in the
 *  steward's project inbox (`portfolio_inbox`) and in its private driver inbox
 *  (`private_inbox`). Both have the same bounds, stated in their descriptions, and the host
 *  states the same numbers back in every answer (`limits`), so a steward can tell what it was
 *  and was not shown.
 *
 *  The steward's inbox writes are registered the same way, only when the host's portfolio
 *  acknowledges: a wake re-injects every unread delivery, so acknowledging one is the only
 *  thing that stops the same letters coming back - in the steward's project inbox
 *  (`portfolio_inbox_acknowledge`) and in its private driver inbox
 *  (`private_inbox_acknowledge`). They are writes, not reads, so they are not part of
 *  PORTFOLIO_TOOLS; a host without one registers no such tool. */

export const PORTFOLIO_TOOLS = ['portfolio_projects', 'portfolio_project', 'portfolio_conversation', 'portfolio_inbox'];

/** @param portfolio host.portfolio: { projects(signal), project(id, signal, page), conversation(id, cursor, signal),
 *    inbox(page, signal), privateInbox(page, signal), acknowledge({ deliveryId, version }, signal),
 *    acknowledgePrivate({ deliveryId, version }, signal) } - all optional; without `inbox`
 *    the project reader is not registered, without `privateInbox` neither is the private one,
 *    without `acknowledge` neither is the project-inbox write, without `acknowledgePrivate`
 *    neither is the private-inbox one.
 *  @param handoff host.handoff: ({ projectId, text, citesUserMessageId }, signal) => result */
export function registerPortfolioTools(scope, { defineTool, jsonOutput, portfolio, handoff }) {
  const projectId = { type: 'string', required: true };
  if (portfolio) {
    scope.tools.register(defineTool({ name: 'portfolio_projects',
      description: 'Every project your owner has: its id and title, whether its director is running, and how many of its questions to your owner are open. Read-only.',
      parameters: {}, output: jsonOutput,
      execute: (_args, exec) => portfolio.projects(exec.signal),
    }));
    // The page arguments are passed through as given; what a page holds and how it says what
    // it did not show is the host's.
    scope.tools.register(defineTool({ name: 'portfolio_project',
      description: 'One project in summary: how many work items it has in each state, its most recently closed work items, its open work (in key order, one page at a time; each line says when it last changed), its open issues (oldest first, one page at a time) and its open questions to your owner. Nothing is cut silently: nodes_shown says how many of how many you were shown; pass open_next_after back as open_after, or issues_next_after as issues_after, to read the next page. Read-only.',
      parameters: { project_id: projectId,
        open_after: { type: 'string', description: 'the open_next_after a previous answer gave; omit for the first page' },
        issues_after: { type: 'integer', description: 'the issues_next_after a previous answer gave; omit for the first page' } },
      output: jsonOutput,
      execute: (args, exec) => portfolio.project(args.project_id, exec.signal, { openAfter: args.open_after, issuesAfter: args.issues_after }),
    }));
    scope.tools.register(defineTool({ name: 'portfolio_conversation',
      description: 'One page of a project\'s conversation between your owner and its director, newest first; pass next_cursor to read older messages. Read-only.',
      parameters: { project_id: projectId, cursor: { type: 'string' } }, output: jsonOutput,
      execute: (args, exec) => portfolio.conversation(args.project_id, args.cursor, exec.signal),
    }));
    if (portfolio.inbox) scope.tools.register(defineTool({ name: 'portfolio_inbox',
      description: 'One page of your PROJECT inbox - the letters sent to your project (in a wake they are under project_inbox; a letter sent to you yourself, under driver_inbox, is in private_inbox instead), oldest delivery first, whole: each delivery\'s id, seq, message id, subject, sender, time, status and version, and its body. One call holds at most 5 deliveries, one body at most 8000 characters, and the whole answer is capped at 24000 characters: a longer body comes back with its true body_chars and body_truncated, and a longer page answers with matched_total, has_more and next_after, which you pass back as after for the next page. Read-only: nothing is acknowledged or changed.',
      parameters: { after: { type: 'integer', description: 'the last delivery_seq you have read; omit to start at the first delivery' },
        limit: { type: 'integer', description: 'deliveries to return; at most 5, and fewer when an answer would pass its cap' } },
      output: jsonOutput,
      execute: (args, exec) => portfolio.inbox({ after: args.after, limit: args.limit }, exec.signal),
    }));
    // The same reader, one inbox over: a letter delivered to the steward's PRIVATE driver
    // inbox. A wake shows only the first 240 characters of a private letter too, so this is
    // the only way to read one whole before answering it. Same bounds, same self-describing
    // answer, same single read; nothing is acknowledged or changed.
    if (portfolio.privateInbox) scope.tools.register(defineTool({ name: 'private_inbox',
      description: 'One page of your PRIVATE inbox - the letters sent to you yourself, by your driver id (in a wake they are under driver_inbox; a letter sent to your project, under project_inbox, is in portfolio_inbox instead), oldest delivery first, whole: every delivery\'s id and seq (delivery_id, delivery_seq), message id, subject, sender (sender_driver_id, the driver that sent it), time, status, version and its body. A wake shows you only the first 240 characters of a letter, so read it whole here before you answer it. One call holds at most 5 deliveries, one body at most 8000 characters, and the whole answer is capped at 24000 characters: a longer body comes back with its true body_chars and body_truncated, and a longer page answers with matched_total, has_more and next_after, which you pass back as after for the next page. Read-only: nothing is acknowledged or changed, and only deliveries addressed to you are in it.',
      parameters: { after: { type: 'integer', description: 'the last delivery seq you have read; omit to start at the first delivery' },
        limit: { type: 'integer', description: 'deliveries to return; at most 5, and fewer when an answer would pass its cap' } },
      output: jsonOutput,
      execute: (args, exec) => portfolio.privateInbox({ after: args.after, limit: args.limit }, exec.signal),
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
    // The same write, one inbox over: a letter delivered to the steward's PRIVATE inbox.
    // Acknowledgement only - no resolve, no other driver, no break-glass (the host never
    // sends one, and the server refuses it for the steward role).
    if (portfolio.acknowledgePrivate) scope.tools.register(defineTool({ name: 'private_inbox_acknowledge',
      description: 'Acknowledge ONE delivery addressed to your own private inbox, so it stops being re-injected: a wake re-injects every unread private delivery, and this is what stops the same letter coming back. Pass the delivery_id your wake (or your inbox page) showed you, and the version it showed if you have it. It is acknowledgement only: no resolve, no other driver, no break-glass. The same delivery and version is the same request, so a retry is not a second acknowledgement.',
      parameters: { delivery_id: { type: 'string', required: true },
        version: { type: 'integer', description: 'the delivery version you were shown; omit to settle the version the inbox holds' } },
      output: jsonOutput,
      execute: (args, exec) => portfolio.acknowledgePrivate({ deliveryId: args.delivery_id, version: args.version }, exec.signal),
    }));
  }
  if (handoff) scope.tools.register(defineTool({ name: 'handoff',
    description: 'Pass what your owner said to a project\'s director, only because your owner asked for it: cites_user_message_id is the id of the owner\'s own message, which is quoted to the director with your text. You cannot instruct a director on your own; tell your owner what you passed on.',
    parameters: { project_id: projectId, text: { type: 'string', required: true }, cites_user_message_id: { type: 'string', required: true } },
    output: jsonOutput,
    execute: (args, exec) => handoff({ projectId: args.project_id, text: args.text, citesUserMessageId: args.cites_user_message_id }, exec.signal),
  }));
}
