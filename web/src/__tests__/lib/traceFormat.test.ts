import { describe, it, expect } from 'vitest';
import { extractPayloadText, shortTime, traceSummary } from '../../lib/traceFormat';

/**
 * A trace payload has no fixed shape — it is whatever the step wrote. These
 * cover the shapes the live pipeline actually produces (checked against a real
 * dpe_game run's trace), because that is what the reader sees.
 */
describe('extractPayloadText', () => {
  it('renders tool calls as name(args) rather than raw JSON', () => {
    const out = extractPayloadText({
      text: 'looking',
      tool_calls: [{ name: 'search', arguments: '{"path":"scenes"}' }],
    });
    expect(out).toContain('looking');
    expect(out).toContain('→ search({"path":"scenes"})');
  });

  it('pairs a string tool_call with its positional tool_args', () => {
    const out = extractPayloadText({ tool_calls: ['write'], tool_args: ['a.txt'] });
    expect(out).toBe('→ write(a.txt)');
  });

  it('labels reasoning so it is not mistaken for the answer', () => {
    expect(extractPayloadText({ reasoning_content: 'hmm' })).toContain('[reasoning]');
  });

  it('falls back to pretty JSON instead of [object Object]', () => {
    expect(extractPayloadText({ verdict: 'passed' })).toBe('{\n  "verdict": "passed"\n}');
  });

  it('expands a prompt record into its system and user halves', () => {
    const out = extractPayloadText({ attempt: 1, mode: 'native',
      system: 'you are a PM', user: 'break this down' });
    expect(out).toContain('[system]\nyou are a PM');
    expect(out).toContain('[user]\nbreak this down');
    // ...and NOT as an escaped JSON blob, which is what it used to be.
    expect(out.startsWith('{')).toBe(false);
  });

  it('passes strings through and survives null', () => {
    expect(extractPayloadText('plain')).toBe('plain');
    expect(extractPayloadText(null)).toBe('');
  });
});

describe('traceSummary', () => {
  it('summarises a tool_call by its arguments', () => {
    expect(traceSummary({ event: 'search', payload: { params: { path: 'scenes' } } }))
      .toBe('{"path":"scenes"}');
  });

  it('uses a tool_result preview instead of the whole result', () => {
    expect(traceSummary({ event: 'search', payload: { preview: '3 matches' } }))
      .toBe('3 matches');
  });

  it('peels a single-key envelope off a preview, but only one layer', () => {
    // `{"output": "{\"files\": []}"}` — the useful part double-encoded inside
    // a wrapper that says nothing.
    const enveloped = JSON.stringify({ output: '{"files": []}' });
    expect(traceSummary({ payload: { preview: enveloped } })).toBe('{"files": []}');
    // Two keys is real structure, not an envelope: leave it alone.
    expect(traceSummary({ payload: { preview: '{"a": "1", "b": "2"}' } }))
      .toBe('{"a": "1", "b": "2"}');
  });

  it('reduces a token-usage record to in/out counts', () => {
    expect(traceSummary({ payload: { prompt_tokens: 100, completion_tokens: 7 } }))
      .toBe('in 100 · out 7');
  });

  it('names the tools a text-less response turn reached for', () => {
    // The commonest response record of all: the model said nothing and just
    // called a tool. `text` is ''.
    expect(traceSummary({ payload: {
      attempt: 1, turn: 2, text: '', reasoning_content: 'because',
      tool_calls: [{ name: 'finish_step', arguments: '{}' }],
    } })).toBe('→ finish_step');
  });

  it('shows the prompt itself, on one line', () => {
    expect(traceSummary({ payload: {
      attempt: 1, mode: 'native', system: 'sys', user: 'do\n  the thing' } }))
      .toBe('do the thing');
  });

  it('reads an engine record as where the run went', () => {
    expect(traceSummary({ payload: { flags: {}, next_node: '3' } })).toBe('→ 3');
    expect(traceSummary({ payload: { step_id: '3', label: 'Review Task Breakdown',
      next_node: '3_budget' } })).toBe('Review Task Breakdown');
    expect(traceSummary({ payload: { status: 'completed', detail: '1 file(s)' } }))
      .toBe('completed · 1 file(s)');
  });

  it('never leads with a bare brace', () => {
    // The defect this replaces: two visible lines spent on "{" and whichever
    // field happened to be first.
    const line = traceSummary({ payload: { attempt_feedback: true, validation_error: null } });
    expect(line).toBe('attempt_feedback=true');
    expect(line.startsWith('{')).toBe(false);
    // A record whose every field is empty says nothing; the head row names it.
    expect(traceSummary({ payload: { attempt_feedback: false, validation_error: null } }))
      .toBe('');
  });
});

describe('shortTime', () => {
  it('extracts the clock out of a trace timestamp', () => {
    expect(shortTime('2026-08-26T15:06:48Z')).toBe('15:06:48');
  });
  it('returns the input when there is no clock in it, and "" for nothing', () => {
    expect(shortTime('yesterday')).toBe('yesterday');
    expect(shortTime(null)).toBe('');
  });
});
