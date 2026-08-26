/**
 * The sign-in affordance. Its whole point is that a reader who CAN become a
 * writer is told where to go — and that a reader who cannot is not offered a
 * link to nowhere. Cloudflare Access issues credentials for an application
 * that exists; when the deployment declares no sign-in route, there is nothing
 * to click and the UI must say nothing.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

const base = {
  canWrite: false, email: null as string | null, permissionResolved: true,
  lang: null, signinUrl: '', authError: null as string | null,
};

async function mount() {
  return render(await import('../../views/AppBar.svelte'));
}

describe('AppBar sign-in', () => {
  beforeEach(() => langStore.set('en'));

  it('offers sign-in to a reader when the deployment declares where', async () => {
    authStore.set({ ...base, signinUrl: 'https://team.example/signin' });
    const { container } = await mount();
    const link = container.querySelector('a.signin-link') as HTMLAnchorElement;
    expect(link).not.toBeNull();
    expect(link.getAttribute('href')).toBe('https://team.example/signin');
    expect(link.textContent?.trim()).toBe('Sign in');
  });

  it('offers NOTHING when the deployment declares no sign-in route', async () => {
    // The state this repo is actually in after an Access application is
    // deleted: gated, read-only, and no issuer left to log in against.
    authStore.set({ ...base, signinUrl: '' });
    const { container } = await mount();
    expect(container.querySelector('a.signin-link')).toBeNull();
  });

  it('says "again" when a credential was presented and refused', async () => {
    // Distinguishable on purpose: "I never signed in" and "I signed in and am
    // still a reader" need different words, or the second reads as a no-op.
    authStore.set({ ...base, signinUrl: 'https://team.example/signin',
                    authError: 'credential_rejected' });
    const { container } = await mount();
    expect(container.querySelector('a.signin-link')?.textContent?.trim())
      .toBe('Sign in again');
  });

  it('shows the identity instead of a sign-in link once signed in', async () => {
    authStore.set({ ...base, canWrite: true, email: 'writer@example.com',
                    signinUrl: 'https://team.example/signin' });
    const { container, getByText } = await mount();
    expect(container.querySelector('a.signin-link')).toBeNull();
    expect(getByText('writer@example.com')).toBeTruthy();
  });

  it('stays quiet until permission has actually resolved', async () => {
    // The store fails closed (canWrite:false) before /api/me answers; flashing
    // "Sign in" at a writer mid-boot would be a lie that then vanishes.
    authStore.set({ ...base, permissionResolved: false,
                    signinUrl: 'https://team.example/signin' });
    const { container } = await mount();
    expect(container.querySelector('a.signin-link')).toBeNull();
  });
});
