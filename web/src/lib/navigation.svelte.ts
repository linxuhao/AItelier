import { get } from 'svelte/store';
import { langStore } from '../stores/i18n';
let lang = $state(get(langStore));
langStore.subscribe(value => { lang = value; });
const en: Record<string,string> = {
  projects:'Projects', runs:'Runs', pipelines:'Pipelines', repositories:'Repositories',
  home:'Project dashboard', pick:'Current project', allProjects:'Browse projects',
  runIntro:'Every workflow execution, including retries, authoring and repository-free work.',
  pipelineIntro:'Reusable workflow definitions. Execution history lives on the Runs page.',
  search:'Search', status:'Run status', all:'All', workflow:'Workflow', execution:'Execution workspace',
  updated:'Updated', open:'Open run', runningStep:'Running step', emptyRuns:'No matching runs.', emptyPipelines:'No matching pipelines.',
  more:'Load more', reload:'Reload', retry:'Retry', results:'results',
  stale:'Refresh failed; the last successful results remain visible.',
  countNote:'Run completion is not goal verification.', repoTools:'Repository tools & legacy executions',
  graph:'Show definition', notes:'Workflow notes', noNotes:'No workflow notes',
  definition:'Current definition · not a run', generated:'Generated', native:'Built-in',
  archive:'Legacy repository tools', currentOnly:'This project',
};
const zh: Record<string,string> = {
  projects:'项目', runs:'运行记录', pipelines:'流水线', repositories:'仓库',
  home:'项目工作台', pick:'当前项目', allProjects:'浏览全部项目',
  runIntro:'每一次真实工作流执行，包含重试、流水线生成任务和无仓库任务。',
  pipelineIntro:'可复用的工作流定义；实际执行历史在“运行记录”页面。',
  search:'搜索', status:'运行状态', all:'全部', workflow:'工作流', execution:'执行工作区',
  updated:'更新时间', open:'打开 Run', runningStep:'正在运行的步骤', emptyRuns:'没有匹配的运行记录。', emptyPipelines:'没有匹配的流水线。',
  more:'加载更多', reload:'刷新', retry:'重试', results:'条结果',
  stale:'刷新失败；仍显示上一次成功获取的结果。', countNote:'Run 完成不等于目标已验收。',
  repoTools:'仓库工具与旧执行项目', graph:'查看定义', notes:'流水线笔记', noNotes:'尚无流水线笔记',
  definition:'当前定义 · 不是某次执行', generated:'生成的', native:'内置', archive:'旧仓库工具', currentOnly:'此项目',
};
const fr: Record<string,string> = {
  projects:'Projets', runs:'Exécutions', pipelines:'Pipelines', repositories:'Dépôts',
  home:'Tableau de bord projet', pick:'Projet actuel', allProjects:'Parcourir les projets',
  runIntro:'Chaque exécution réelle, y compris les nouvelles tentatives et le travail sans dépôt.',
  pipelineIntro:'Définitions réutilisables. Historique dans la page Exécutions.', search:'Rechercher', status:'État', all:'Tous',
  workflow:'Workflow', execution:'Espace d’exécution', updated:'Mis à jour', open:'Ouvrir', runningStep:'Étape en cours',
  emptyRuns:'Aucune exécution correspondante.', emptyPipelines:'Aucun pipeline correspondant.', more:'Charger la suite',
  reload:'Actualiser', retry:'Réessayer', results:'résultats', stale:'Actualisation échouée ; les derniers résultats restent affichés.',
  countNote:'Une exécution terminée ne valide pas un objectif.', repoTools:'Outils de dépôt et anciens projets',
  graph:'Voir la définition', notes:'Notes du workflow', noNotes:'Aucune note', definition:'Définition actuelle · pas une exécution',
  generated:'Généré', native:'Intégré', archive:'Anciens outils de dépôt', currentOnly:'Ce projet',
};
export function nt(key: string): string {
  return (lang.startsWith('zh') ? zh : lang.startsWith('fr') ? fr : en)[key] ?? en[key] ?? key;
}
export interface RunRow {
  id:string; project_id:string; execution_name:string; config_name:string; status:string;
  current_node:string|null; active_step:{step_id:string;status:string;instance_id:number|null;loop_item:string|null;source:string}|null; updated_at:string|null; created_at:string|null;
  graph_version:number|null; repo_path:string|null; state_project_id:string|null; state_node_key:string|null;
}
const LAST_PROJECT='aitelier_last_state_project';
export function readLastProject(): string { try { return localStorage.getItem(LAST_PROJECT) ?? ''; } catch { return ''; } }
export function rememberProject(id:string): void { try { localStorage.setItem(LAST_PROJECT,id); } catch { /* preference only */ } }


export function navArea(hash:string): 'projects'|'runs'|'pipelines'|'chat'|'' {
  const path=hash.replace(/^#/,'') || '/';
  if(path==='/pipelines')return 'pipelines';
  if(path.startsWith('/runs') || path.startsWith('/state-runs/') || path.startsWith('/repos') || /^\/projects\/.+/.test(path))return 'runs';
  if(path==='/chat')return 'chat';
  return path==='/' || path==='/projects' || path.startsWith('/state-projects')?'projects':'';
}
