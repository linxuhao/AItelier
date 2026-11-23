import type { DriverConversationEntry } from './api';
import type { OpenQuestion } from './questions';

/** One file in the composer (step 24.2b). `upload_id` is set once the server staged it;
 *  `refused` is the server's code when it would not (never sent with the message);
 *  `progress` (0..1) while it is still uploading. `preview` is a `data:` URL, images only
 *  (the page CSP lets `img-src` take `data:`, not `media-src`). */
export interface DraftAttachment {
  key: string; name: string; size: number;
  kind?: string; mime?: string; upload_id?: string; refused?: string; progress?: number; preview?: string;
}

/** In-memory draft, owned by the workbench and scoped to one project. */
export interface ConversationDraft {
  text: string;
  replyTo: DriverConversationEntry | null;
  answering: OpenQuestion | null;
  /** The files picked for the next message. The composer reads and writes them here
   *  only, so they survive switching projects like the text does. */
  attachments: DraftAttachment[];
}
