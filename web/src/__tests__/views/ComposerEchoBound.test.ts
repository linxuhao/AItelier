import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';
const api = vi.hoisted(() => ({ dshDriverConversation: vi.fn(), sendDriverConversationMessage: vi.fn(), markDriverConversationRead: vi.fn() }));
vi.mock('../../lib/api', () => api);
import DriverConversation from '../../views/DriverConversation.svelte';

// The composer's reply echo is BOUNDED.
//
// `.composer` is `flex:0 0 auto` inside `.conversation`, a fixed-height flex
// column with `overflow:hidden`, so the composer is the last band of a clipped
// pane and never shrinks. The echo above the input used to render the whole
// question, so replying to a long one grew the band past the pane's fixed height
// and pushed the textarea and Send below the column's clipped bottom edge, with
// no scroll path back to them: measured in Chromium on 8b971d183c at the
// dashboard's 508px pane height, a 4050-character echo made the reply input
// disappear.
//
// These tests mount a 4000-character question, take the rail's reply control,
// and hold the properties that keep the input reachable: the echo renders a
// bounded excerpt (the untruncated question stays in the band's `title`), the
// band has its own height bound so it scrolls instead of growing, and the rail
// row - where the question is actually read and answered - still carries all
// 4000 characters.

const driverMsg = (id: string, subject: string, created: string, extra: any = {}) =>
  ({message_id: id, thread_id: id, project_id: 'alpha', sender_kind: 'driver',
    sender_driver_id: 'dsh-alpha', subject, body: '', refs: {}, reply_to_message_id: null,
    created_at: created, ...extra});
const page = (messages: any[]) =>
  ({project_id: 'alpha', messages, has_more: false, next_cursor: null, end_of_history: true,
    matched_total: messages.length, unread_count: 0, read_upto_message_id: null,
    read_upto_created_at: null});

/** A question of exactly 4000 characters with no whitespace runs in it, so the
 *  bound is measured and not the whitespace rule. Readable sentences rather than
 *  one repeated letter, so a truncated excerpt is recognisable. */
const QUESTION_CHARS = 4000;
const longQuestion = 'may I restart the host, then re-run the whole sweep and tell me what moved? '
  .repeat(80).slice(0, QUESTION_CHARS);
/** What the echo may render: at most 168 characters, then one ellipsis. */
const ECHO_CHARS = 168;
const ELLIPSIS = '\u2026';
const ANSWERING_LABEL = 'Answering a question \u00b7 ';
const CANCEL_LABEL = 'Cancel reply';

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
});
afterEach(() => cleanup());

/** The composer's echo band, or null when the composer is plain.
 *
 *  The band IS the echo element: it is the `<p>` that renders the excerpt and
 *  carries `composer-echo` (the CSS bound) and the `title` (the whole question),
 *  so the excerpt is read out of its text by taking off the label in front and
 *  the cancel control behind. */
function echoBand(container: HTMLElement): HTMLElement | null {
  return container.querySelector('[data-conversation-composer] .composer-echo') as HTMLElement | null;
}
function excerptOf(band: HTMLElement, label: string): string {
  // `trimStart` because the band's markup indents the label onto its own line.
  const text = (band.textContent ?? '').trimStart();
  expect(text.startsWith(label)).toBe(true);
  const cancel = band.querySelector('button')?.textContent ?? CANCEL_LABEL;
  const rest = text.slice(label.length);
  expect(rest.endsWith(cancel)).toBe(true);
  return rest.slice(0, rest.length - cancel.length).trim();
}

describe('the composer echo is bounded', () => {
  it('a 4000-character question becomes a bounded excerpt, with the whole question in its title', async () => {
    // The fixture's own premise: the question really is 4000 characters.
    expect(longQuestion).toHaveLength(QUESTION_CHARS);
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: longQuestion}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await fireEvent.click(await view.findByLabelText('Answer this question: ' + longQuestion));

    // The composer is bound to that question: the same three attributes, and the
    // same cancel control, as before the change.
    const bound = view.container.querySelector('[data-answering]') as HTMLElement;
    expect(bound).not.toBeNull();
    expect(bound.getAttribute('data-answer-message-id')).toBe('m1');
    expect(bound.getAttribute('data-answer-segment-id')).toBe('q1');
    expect(view.getByRole('button', {name: CANCEL_LABEL})).toBeTruthy();

    // The echo is an excerpt: at most 168 characters plus one ellipsis, the
    // opening of the question, and nowhere near the whole 4000.
    const band = echoBand(view.container) as HTMLElement;
    expect(band).not.toBeNull();
    expect(band).toBe(bound);
    const shown = excerptOf(band, ANSWERING_LABEL);
    expect(shown.length).toBeLessThanOrEqual(ECHO_CHARS + 1);
    expect(shown.endsWith(ELLIPSIS)).toBe(true);
    expect(shown).toBe(longQuestion.slice(0, ECHO_CHARS) + ELLIPSIS);
    expect(longQuestion.startsWith(shown.slice(0, -1))).toBe(true);

    // The untruncated text is still reachable: the whole question is the title.
    expect(band.getAttribute('title')).toHaveLength(QUESTION_CHARS);
    expect(band.getAttribute('title')).toBe(longQuestion);

    // Nothing in the composer echoes the whole question, so what sits in the
    // clipped pane stays bounded whatever the question's length.
    expect(view.container.querySelector('[data-conversation-composer]')?.textContent)
      .not.toContain(longQuestion);
  });

  it('keeps the whole 4000-character question on the rail row, where it is read and answered', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: longQuestion}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    const row = await waitFor(() => {
      const found = view.container.querySelector('[data-question-segment-id="q1"]') as HTMLElement | null;
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    // Bounding the composer's echo is not bounding the rail: the question the
    // user is answering is still there in full, where it is listed.
    const text = row.querySelector('.question-text') as HTMLElement;
    expect(text).not.toBeNull();
    expect(text.textContent).toBe(longQuestion);
    expect(text.textContent).toHaveLength(QUESTION_CHARS);
    expect(view.container.querySelector('[data-questions-box]')?.textContent).toContain(longQuestion);
  });

  it('gives the echo band its own height bound, so it scrolls instead of growing the band', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: longQuestion}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await fireEvent.click(await view.findByLabelText('Answer this question: ' + longQuestion));

    // The rule, by value: `.composer-echo{max-height:60px;overflow-y:auto;…}`.
    // Asserted on the element that actually holds the text, because `max-height`
    // on an inline box does nothing - which is how a "bounded" echo could still
    // have grown the band.
    const band = echoBand(view.container) as HTMLElement;
    const style = getComputedStyle(band);
    expect(style.maxHeight).toBe('60px');
    expect(style.overflowY).toBe('auto');
    expect(style.display).not.toBe('inline');

    // A user-dragged textarea scrolls its own text past 6rem instead of growing
    // the composer past the clipped pane.
    const textarea = view.getByLabelText('Write a message') as HTMLTextAreaElement;
    const textareaStyle = getComputedStyle(textarea);
    expect(textareaStyle.maxHeight).toBe('6rem');
    expect(textareaStyle.overflowY).toBe('auto');
  });

  it('cancelling returns the plain composer, with no echo left', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: longQuestion}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await fireEvent.click(await view.findByLabelText('Answer this question: ' + longQuestion));
    expect(echoBand(view.container)).not.toBeNull();

    await fireEvent.click(view.getByRole('button', {name: CANCEL_LABEL}));

    // Nothing of the echo survives: no band, no binding, no excerpt, and the
    // input is still there to type in.
    expect(echoBand(view.container)).toBeNull();
    expect(view.container.querySelector('.composer-echo')).toBeNull();
    expect(view.container.querySelector('[data-answering]')).toBeNull();
    expect(view.container.querySelector('[data-conversation-composer]')?.textContent)
      .not.toContain(longQuestion);
    expect(view.getByLabelText('Write a message')).not.toBeNull();
  });

  it('collapses whitespace and trims before it cuts, and the title keeps the text as it was written', async () => {
    // A question written with newlines and indentation: the excerpt is one
    // collapsed line, so the band cannot be grown by the SHAPE of the text
    // either (120 blank lines would otherwise be 120 lines of band). The title
    // is the untruncated text as stored, so the layout of the question is not
    // lost - it is only kept out of the band.
    const raw = '\n\n   first line\n\n\n      second line is indented\n   '
      + 'x'.repeat(QUESTION_CHARS) + '\n\n';
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: raw}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    const row = await waitFor(() => {
      const found = view.container.querySelector('[data-answer-message-id="m1"]') as HTMLElement | null;
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    await fireEvent.click(row);
    const band = echoBand(view.container) as HTMLElement;
    const shown = excerptOf(band, ANSWERING_LABEL);
    const collapsed = ('first line second line is indented ' + 'x'.repeat(QUESTION_CHARS)).trim();
    expect(shown).toBe(collapsed.slice(0, ECHO_CHARS) + ELLIPSIS);
    expect(shown).not.toContain('\n');
    expect(shown).not.toMatch(/\s\s/);
    // 168 characters of the collapsed text, then one ellipsis.
    expect(shown).toHaveLength(ECHO_CHARS + 1);
    expect(shown.endsWith(ELLIPSIS)).toBe(true);
    expect(shown.startsWith('first line second line is indented xxx')).toBe(true);
    // The title is the text as stored: whitespace and all.
    expect(band.getAttribute('title')).toBe(raw);
  });

  it('bounds the thread-reply echo too, on the same rule', async () => {
    // The other branch renders the driver message's SUBJECT, which a driver can
    // also write long: same excerpt rule, same title, same class.
    const longSubject = ('Sweep report for the host restart and the node checks that followed. ')
      .repeat(70).slice(0, QUESTION_CHARS);
    expect(longSubject).toHaveLength(QUESTION_CHARS);
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', longSubject, '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'i1', kind: 'info', text: 'context'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    const replyControl = await waitFor(() => {
      const found = view.container.querySelector('[data-conversation-thread] article.message header button') as HTMLElement | null;
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    await fireEvent.click(replyControl);

    const band = echoBand(view.container) as HTMLElement;
    expect(band).not.toBeNull();
    const shown = excerptOf(band, 'Replying to \u00b7 ');
    expect(shown.length).toBeLessThanOrEqual(ECHO_CHARS + 1);
    expect(shown).toBe(longSubject.slice(0, ECHO_CHARS) + ELLIPSIS);
    expect(band.getAttribute('title')).toBe(longSubject);
    expect(getComputedStyle(band).maxHeight).toBe('60px');
    // The reply target itself is untouched: only the echo is bounded, and the
    // thread still shows the message in full.
    expect(view.container.querySelector('[data-conversation-thread]')?.textContent).toContain(longSubject);
  });
});
