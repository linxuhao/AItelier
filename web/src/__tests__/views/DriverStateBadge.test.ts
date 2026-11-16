import { afterEach, describe, it, expect } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';
import DriverStateBadge from '../../views/DriverStateBadge.svelte';
import SOURCE from '../../views/DriverStateBadge.svelte?raw';

const STATES = ['running', 'idle', 'dormant', 'starting', 'stopping', 'failed', 'stopped'] as const;

describe('DriverStateBadge', () => {
  afterEach(() => { cleanup(); langStore.set('en'); });

  it('maps the runtime phase to a state, a hidden mark and the label', () => {
    langStore.set('en');
    const cases: [string | undefined, string, string][] = [
      ['running', 'running', 'Busy'], ['waiting', 'idle', 'Ready'], ['dormant', 'dormant', 'Dormant'],
      ['starting', 'starting', 'Starting'], ['stopping', 'stopping', 'Stopping'],
      ['failed', 'failed', 'Failed'], [undefined, 'stopped', 'Stopped'],
    ];
    for (const [phase, state, label] of cases) {
      const view = render(DriverStateBadge, { phase });
      const badge = view.container.querySelector('.driver-state')!;
      expect(badge.getAttribute('data-phase')).toBe(state);
      expect(badge.textContent).toBe(label);
      expect(badge.getAttribute('title')).toBe(label);
      expect(badge.querySelector('.driver-state-mark')?.getAttribute('aria-hidden')).toBe('true');
      cleanup();
    }
  });

  it('labels busy and ready in each language, and keeps an explicit title', () => {
    langStore.set('zh');
    let view = render(DriverStateBadge, { phase: 'running' });
    expect(view.container.textContent).toBe('忙碌'); cleanup();
    view = render(DriverStateBadge, { phase: 'waiting' });
    expect(view.container.textContent).toBe('就绪'); cleanup();
    langStore.set('fr');
    view = render(DriverStateBadge, { phase: 'waiting', title: 'waiting' });
    expect(view.container.textContent).toBe('Prêt');
    expect(view.container.querySelector('.driver-state')?.getAttribute('title')).toBe('waiting');
  });

  it('draws a mark for every state, and stops moving when motion is reduced', () => {
    for (const state of STATES) expect(SOURCE).toContain(`[data-phase=${state}] .driver-state-mark::before{`);
    expect(SOURCE).not.toMatch(/\[data-phase=stopped\][^}]*animation/);
    expect(SOURCE).toMatch(/@media \(prefers-reduced-motion: reduce\)\{\.driver-state-mark::before,\.driver-state-mark::after\{animation:none;\}\}/);
  });
});
