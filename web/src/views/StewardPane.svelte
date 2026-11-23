<script lang="ts">
  import type { DriverStatusResponse, StewardProposal } from '../lib/api';
  import type { StateProjectRow } from '../lib/stateGraph';
  import { t } from '../lib/i18n.svelte';
  import DriverConversationPane from './DriverConversationPane.svelte';
  import StewardProposals from './StewardProposals.svelte';
  import StewardQuestions from './StewardQuestions.svelte';
  import StewardProfile from './StewardProfile.svelte';
  import StewardSchedules from './StewardSchedules.svelte';
  import StewardChannels from './StewardChannels.svelte';
  import StewardCapabilities from './StewardCapabilities.svelte';
  import StewardLists from './StewardLists.svelte';
  import StewardConnections from './StewardConnections.svelte';
  // The steward's pane is the ordinary conversation pane of its project, plus
  // tabs of its own: proposals, every project's open questions, reminders, the family
  // lists, chat channels, its own MCP connections (step 24.1a), profile. Only this pane
  // asks where its messages came from (`sources`).
  const { projectId, projects = [], status = null, questions = {}, onread = () => {}, onrefresh = () => {}, onanswer,
    connect = null }: {
    projectId: string; projects?: StateProjectRow[]; status?: DriverStatusResponse | null;
    questions?: Record<string, number>; onread?: () => void; onrefresh?: () => void;
    /** Leave the steward for another project's conversation. */
    onanswer: (projectId: string) => void;
    /** Step 24.1c: open the connections tab on this connection's secret / sign-in
     *  (the link a connection proposal's result carries, #/steward/connections/<id>/connect). */
    connect?: string | null;
  } = $props();
  // svelte-ignore state_referenced_locally
  let mode = $state(connect ? 'connections' : 'conversation');
  // svelte-ignore state_referenced_locally
  let focus = $state<string | null>(connect);
  function openConnection(connId: string) { focus = connId; mode = 'connections'; }
  let prefill = $state<{text: string} | null>(null);
  const self = $derived([{...(projects.find(p => p.project_id === projectId) ?? {project_id: projectId}), title: t('steward.name')} as StateProjectRow]);
  const tabs = $derived([
    {id: 'proposals', label: t('steward.tabProposals')},
    {id: 'questions', label: t('steward.tabQuestions')},
    {id: 'schedules', label: t('steward.tabSchedules')},
    {id: 'lists', label: t('steward.tabLists')},
    {id: 'channels', label: t('steward.tabChannels')},
    {id: 'connections', label: t('steward.tabConnections')},
    {id: 'profile', label: t('steward.tabProfile')},
  ]);
  // "Change it" is not an endpoint: the owner tells the steward in the
  // conversation, and the steward proposes again (the old one is superseded).
  function revise(row: StewardProposal) {
    prefill = {text: t('steward.changePrefix').replace('{id}', row.proposal_id)};
    mode = 'conversation';
  }
  function answer(id: string) {
    if (id === projectId) mode = 'conversation'; else onanswer(id);
  }
</script>
<DriverConversationPane projects={self} {status} selected={projectId} bind:mode {tabs} {prefill} {onread} {onrefresh} ack sources>
  {#snippet panel(tab: string)}
    {#if tab === 'proposals'}<StewardProposals onchange={onrefresh} onrevise={revise} onconnect={openConnection} />
    {:else if tab === 'questions'}<StewardQuestions {questions} {projects} {status} onanswer={answer} onanswered={onrefresh} />
    {:else if tab === 'schedules'}<StewardSchedules />
    {:else if tab === 'lists'}<StewardLists />
    {:else if tab === 'channels'}<div class="channels-with-capabilities"><StewardChannels /><StewardCapabilities /></div>
    {:else if tab === 'connections'}<StewardConnections {focus} />
    {:else if tab === 'profile'}<StewardProfile />{/if}
  {/snippet}
</DriverConversationPane>

<style>
  .channels-with-capabilities{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;align-items:start;}
  @media (max-width:900px){.channels-with-capabilities{grid-template-columns:1fr;}}
</style>
