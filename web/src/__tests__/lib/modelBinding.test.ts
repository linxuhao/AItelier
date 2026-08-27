import { describe, it, expect } from 'vitest';
import { modelBinding } from '../../lib/traceFormat';

describe('modelBinding', () => {
  it('renders route → served_by for a usage payload', () => {
    expect(modelBinding({ model_route: 'flash', served_by: 'qwen/qwen3.8-flash' }))
      .toBe('flash → qwen/qwen3.8-flash');
  });
  it('omits the arrow when the route IS the concrete endpoint', () => {
    expect(modelBinding({ model_route: 'ark/glm-5.3', served_by: 'ark/glm-5.3' }))
      .toBe('ark/glm-5.3');
  });
  it('returns null for non-usage payloads (claim rows must never show a guess)', () => {
    expect(modelBinding({ step_id: 't_impl', event: 'claimed' })).toBeNull();
    expect(modelBinding('text')).toBeNull();
    expect(modelBinding(null)).toBeNull();
  });
});
