/** The steward's family-group tools (step 23, 25 C4, 23.1): the family memory, the family lists
 *  and a group member's one-time reminder - each only when the host's `groups` provides it.
 *  Moved out of index.mjs's setup unchanged so a test can run them through the pinned DSH
 *  defineTool without booting (2026-11-22: a schema the pinned defineTool refuses stops the
 *  steward at boot, and the other tests pass definitions through `t => t`).
 *
 *  @param groups host.groups (familyNote/familyShow, listShow/listAdd/listCheck/listRemove, reminderSet).
 *  @param turns the GroupTurns (open() and audience()). */
export function registerGroupTools(scope, { defineTool, jsonOutput, groups, turns }) {
  // Step 23: the family memory, a store of its own on the host, for group turns only (the
  // guard refuses both tools in an owner turn; the host accepts them only from a group turn).
  if (groups?.familyNote) {
    scope.tools.register(defineTool({ name: 'family_memory_note',
      description: 'Group turns only. Keep one short thing someone in this family group asked you to remember for the family (at most 200 characters). Never your owner\'s memory; shopping and to-do items go to the family lists.',
      parameters: { text: { type: 'string', required: true } }, output: jsonOutput,
      execute: (args, exec) => groups.familyNote(args.text, exec.signal, { turn: turns.open()?.turn }) }));
    scope.tools.register(defineTool({ name: 'family_memory_show',
      description: 'Group turns only. This group\'s family memory (newest first) and your owner\'s note to the family.',
      parameters: {}, output: jsonOutput, execute: (_args, exec) => groups.familyShow(exec.signal, { turn: turns.open()?.turn }) }));
  }
  // Step 25 C4 (design-steward 7.9.5): the family lists, shopping and to-do. Show, add and check
  // in a group turn too (the guard's whitelist; the host's request then carries the public-read
  // header, and this code - never the model - names the open turn the backend checks against
  // the recorded audience); remove in an owner turn only.
  if (groups?.listShow) {
    const turn = () => ({ turn: turns.open()?.turn });
    // Step 23.1 (7.9.5): each bound group has its own lists. In an owner turn `group` names whose
    // (required when several groups are bound); in a group turn the lists are that group's and
    // a group the model names is never sent (the backend takes the turn's group from its record).
    const owned = args => (turns.audience().kind === 'owner' && typeof args.group === 'string' && args.group ? { group: args.group } : {});
    const group = { type: 'string', description: 'Your owner\'s turns: the bound group whose lists (its name), required when more than one group is bound. Ignored in a group turn.' };
    const list = { type: 'string', enum: ['shopping', 'todo'] };
    scope.tools.register(defineTool({ name: 'list_show',
      description: 'A family group\'s lists (shopping and to-do; or one, with list): each item\'s item_id, text, who added it and when, and whether it is checked. Every open item, then at most 100 checked items per list, newest first (a checked item is kept 30 days): done_total counts each list\'s checked items, done_more how many are not shown, and done_not_shown says it in words (另有 N 条已勾掉的未显示) - say so rather than call the list complete. Everyone in that group can see everything on them, whoever added it; each bound group has its own lists.',
      parameters: { list, group }, output: jsonOutput, execute: (args, exec) => groups.listShow({ list: args.list, ...owned(args) }, exec.signal, turn()) }));
    scope.tools.register(defineTool({ name: 'list_add',
      description: 'Add up to 10 items (each at most 100 characters) to a family group\'s shopping or to-do list. An item already open on that list is not added twice. Refused withheld when an item names something the group may not see; list_full at 100 open items.',
      parameters: { list: { ...list, required: true }, items: { type: 'array', items: { type: 'string' }, required: true }, group }, output: jsonOutput,
      execute: (args, exec) => groups.listAdd({ list: args.list, items: args.items, ...owned(args) }, exec.signal, turn()) }));
    scope.tools.register(defineTool({ name: 'list_check',
      description: 'Check off (done: true, the default) or uncheck (done: false) family list items by the item_id list_show gave.',
      parameters: { item_ids: { type: 'array', items: { type: 'string' }, required: true }, done: { type: 'boolean' }, group }, output: jsonOutput,
      execute: (args, exec) => groups.listCheck({ itemIds: args.item_ids, done: args.done ?? true, ...owned(args) }, exec.signal, turn()) }));
    scope.tools.register(defineTool({ name: 'list_remove',
      description: 'Your owner\'s turns only: remove family list items by item_id (in the family group items are only checked off; only your owner removes).',
      parameters: { item_ids: { type: 'array', items: { type: 'string' }, required: true }, group }, output: jsonOutput,
      execute: (args, exec) => groups.listRemove({ itemIds: args.item_ids, ...owned(args) }, exec.signal, turn()) }));
  }
  // Step 23.1 (design-steward 7.9.4, owner 2026-11-22 「那个群问的哪个群提醒」): in a group turn
  // someone may ask for a one-time reminder to that same group. Group turns only (the guard
  // refuses it in an owner turn; the host accepts it only from a group turn): the group and who
  // asked come from the turn's record, never from the model; the words are posted as written.
  if (groups?.reminderSet) scope.tools.register(defineTool({ name: 'group_reminder_set',
    description: 'Group turns only. A one-time reminder to THIS group that someone here asked for: at is the time (ISO 8601; a time without offset is your owner\'s local time), within 7 days; text is their exact words, at most 500 characters, posted into this group as written when due (you are not woken). At most 3 waiting per person and 6 per group (member_reminders_full, group_member_reminders_full); refused withheld when the words name something the group may not see; ambiguous_requester when this turn holds questions of several people. Repeating reminders, changes and stopping one are your owner\'s.',
    parameters: { at: { type: 'string', required: true }, text: { type: 'string', required: true } }, output: jsonOutput,
    execute: (args, exec) => groups.reminderSet({ at: args.at, text: args.text }, exec.signal, { turn: turns.open()?.turn }) }));
}
