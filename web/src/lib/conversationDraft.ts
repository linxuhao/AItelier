import type { DriverConversationEntry } from './api';
import type { OpenQuestion } from './questions';

/** In-memory draft, owned by the workbench and scoped to one project. */
export interface ConversationDraft {
  text: string;
  replyTo: DriverConversationEntry | null;
  answering: OpenQuestion | null;
}
