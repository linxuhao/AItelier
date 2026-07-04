import { writable } from 'svelte/store';

export interface Project {
  id: string;
  name: string;
  // Additional fields can be added as needed — consumers use structural typing
  [key: string]: unknown;
}

export interface ProjectState {
  currentProjectId: string | null;
  currentRepoPath: string | null;
  projects: Project[];
}

export const projectStore = writable<ProjectState>({
  currentProjectId: null,
  currentRepoPath: null,
  projects: [],
});

export function setCurrentProject(id: string | null): void {
  projectStore.update(prev => ({ ...prev, currentProjectId: id }));
}

export function setCurrentRepoPath(path: string | null): void {
  projectStore.update(prev => ({ ...prev, currentRepoPath: path }));
}

export function setProjects(list: Project[]): void {
  projectStore.update(prev => ({ ...prev, projects: list }));
}
