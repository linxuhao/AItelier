/**
 * The repo link. Small, but two of its attributes are load-bearing now that
 * the UI is publicly readable: it must open away from the app, and it must not
 * hand the opened page a live `window.opener` back into this one.
 */
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';

describe('AppBar GitHub link', () => {
  it('points at the repo and opens it safely', async () => {
    const { container } = render(await import('../../views/AppBar.svelte'));
    const link = container.querySelector('a.gh-link') as HTMLAnchorElement;
    expect(link).not.toBeNull();
    expect(link.getAttribute('href')).toBe('https://github.com/linxuhao/aitelier');
    expect(link.getAttribute('target')).toBe('_blank');
    const rel = link.getAttribute('rel') ?? '';
    expect(rel).toContain('noopener');
    expect(rel).toContain('noreferrer');
    // An icon-only link needs a name, or it reads as "link" to a screen reader.
    expect(link.getAttribute('aria-label')).toBeTruthy();
    expect(link.querySelector('svg')).not.toBeNull();
  });

  it('names itself in the reader’s language', async () => {
    langStore.set('zh-CN');
    const { container } = render(await import('../../views/AppBar.svelte'));
    const link = container.querySelector('a.gh-link') as HTMLAnchorElement;
    expect(link.getAttribute('aria-label')).toBe('在 GitHub 上查看源码');
    langStore.set('en');
  });
});
