/**
 * Lightweight i18n: t(key) helper. Add translations as needed.
 */
import { get } from 'svelte/store';
import { langStore } from '../stores/i18n';

const translations: Record<string, Record<string, string>> = {
  en: {
    'chat.title': 'Chat',
    'chat.placeholder': 'Ask anything or describe what to build...',
    'chat.send': 'Send',
    'chat.mode.butler': 'Butler',
    'chat.mode.coding': 'Coding',
    'dashboard.title': 'Dashboard',
    'dashboard.no_projects': 'No projects yet.',
    'project.tasks': 'Tasks',
    'project.workspace': 'Workspace',
    'lang.selector': 'Language',
  },
  'zh-CN': {
    'chat.title': '对话',
    'chat.placeholder': '描述你想构建的内容...',
    'chat.send': '发送',
    'chat.mode.butler': '管家',
    'chat.mode.coding': '编码',
    'dashboard.title': '仪表盘',
    'dashboard.no_projects': '暂无项目。',
    'project.tasks': '任务',
    'project.workspace': '工作区',
    'lang.selector': '语言',
  },
};

export function t(key: string): string {
  const lang = get(langStore);
  const map = translations[lang] || translations[lang.split('-')[0]];
  if (map && map[key]) return map[key];
  return translations['en'][key] || key;
}
