/**
 * WorkspaceBrowser — nested foldable folder tree (vanilla-UI parity):
 * top level shows only root entries; folders expand on click down to files.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';

const mockApi = vi.hoisted(() => ({
  workspaceTree: vi.fn(),
  workspaceFile: vi.fn(),
  workspaceRawUrl: vi.fn((pid: string, path: string, root: string) =>
    `/api/projects/${pid}/workspace/raw?path=${path}&root=${root}`),
}));
vi.mock('../../lib/api', () => mockApi);

describe('WorkspaceBrowser folder tree', () => {
  it('renders collapsed folders and expands to reveal nested files', async () => {
    mockApi.workspaceTree.mockResolvedValue({
      tree: ['README.md', 'src/app.py', 'src/utils/helpers.py', 'docs/guide.md'],
    });

    const { container, getByText, queryByText } = render(
      await import('../../views/WorkspaceBrowser.svelte'),
      { props: { projectId: 'p1', root: 'code', title: 'Repo', startOpen: true } });

    await waitFor(() => {
      expect(getByText('README.md')).toBeTruthy();
    });

    // Folders visible, their contents hidden until expanded
    expect(getByText('src')).toBeTruthy();
    expect(getByText('docs')).toBeTruthy();
    expect(queryByText('app.py')).toBeNull();
    expect(queryByText('guide.md')).toBeNull();

    // Expand src → app.py and the nested utils folder appear (still folded)
    await fireEvent.click(getByText('src'));
    expect(getByText('app.py')).toBeTruthy();
    expect(getByText('utils')).toBeTruthy();
    expect(queryByText('helpers.py')).toBeNull();

    // Expand utils → helpers.py appears
    await fireEvent.click(getByText('utils'));
    expect(getByText('helpers.py')).toBeTruthy();

    // Collapse src again → nested entries disappear
    await fireEvent.click(getByText('src'));
    expect(queryByText('app.py')).toBeNull();
    expect(queryByText('helpers.py')).toBeNull();

    // Clicking a file opens the content dialog
    mockApi.workspaceFile.mockResolvedValue({ content: 'hello', truncated: false });
    await fireEvent.click(getByText('README.md'));
    await waitFor(() => {
      expect(container.querySelector('.ws-file-dialog')).not.toBeNull();
    });
    expect(mockApi.workspaceFile).toHaveBeenCalledWith('p1', 'README.md', 'code');
  });
});

describe('WorkspaceBrowser image preview', () => {
  it('renders an <img> for an image file instead of reading it as text', async () => {
    mockApi.workspaceTree.mockResolvedValue({ tree: ['assets/logo.png'] });
    mockApi.workspaceFile.mockClear();  // shared vi.fn across tests in this file

    const { container, getByText } = render(
      await import('../../views/WorkspaceBrowser.svelte'),
      { props: { projectId: 'p1', root: 'code', title: 'Repo', startOpen: true } });

    await waitFor(() => expect(getByText('assets')).toBeTruthy());
    await fireEvent.click(getByText('assets'));
    await fireEvent.click(getByText('logo.png'));

    const img = await waitFor(() => {
      const el = container.querySelector('.ws-file-dialog img') as HTMLImageElement | null;
      expect(el).not.toBeNull();
      return el as HTMLImageElement;
    });
    expect(img.getAttribute('src')).toBe(
      '/api/projects/p1/workspace/raw?path=assets/logo.png&root=code');
    // The text reader would only produce mojibake here — it must not be used.
    expect(mockApi.workspaceFile).not.toHaveBeenCalled();
  });
});
