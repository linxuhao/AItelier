<script lang="ts">
  import { t } from '../lib/i18n.svelte';

  let {
    show = $bindable(false),
    title = t('confirm.title'),
    message = '',
    confirmLabel = t('confirm.yes'),
    cancelLabel = t('confirm.no'),
    variant = 'default',
    onConfirm = () => {},
    onCancel = () => {},
  }: {
    show?: boolean;
    title?: string;
    message?: string;
    confirmLabel?: string;
    cancelLabel?: string;
    variant?: 'default' | 'danger';
    onConfirm?: () => void | Promise<void>;
    onCancel?: () => void;
  } = $props();

  let pending = $state(false);

  function handleKeydown(e: KeyboardEvent): void {
    // Only intercept when the dialog is actually visible
    if (!show) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      cancel();
    }
    if (e.key === 'Enter' && !e.repeat && !e.shiftKey) {
      e.preventDefault();
      confirm();
    }
  }

  function handleDialogClose(): void {
    if (show) {
      show = false;
      onCancel();
    }
  }

  async function confirm(): Promise<void> {
    if (pending) return;
    pending = true;
    try {
      const result = onConfirm();
      if (result instanceof Promise) {
        await result;
      }
    } finally {
      pending = false;
    }
    show = false;
  }

  function cancel(): void {
    if (pending) return;
    show = false;
    onCancel();
  }

  // Focus trap: when dialog opens, focus the confirm button
  let dialogEl: HTMLDialogElement;
  $effect(() => {
    if (show && dialogEl) {
      // Focus the first focusable element inside
      requestAnimationFrame(() => {
        const firstButton = dialogEl.querySelector('footer button:first-child') as HTMLButtonElement | null;
        firstButton?.focus();
      });
    }
  });
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- `open` is one-way (Svelte only allows bind:open on <details>);
     dialog-initiated closes sync back through onclose. -->
<dialog
  id="confirm-dialog"
  open={show}
  onclose={handleDialogClose}
  bind:this={dialogEl}
>
  <article>
    <header>
      <h3 id="confirm-title">{title}</h3>
    </header>
    <div id="confirm-message">{message}</div>
    <footer>
      <button
        id="confirm-yes"
        class="contrast"
        class:danger={variant === 'danger'}
        onclick={confirm}
        disabled={pending}
      >
        {pending ? t('confirm.saving') : confirmLabel}
      </button>
      <button
        id="confirm-no"
        class="outline"
        onclick={cancel}
        disabled={pending}
      >
        {cancelLabel}
      </button>
    </footer>
  </article>
</dialog>

<style>
  button.danger {
    background-color: var(--del-color, #d04040);
    border-color: var(--del-color, #d04040);
    color: #fff;
  }

  button.danger:hover {
    opacity: 0.9;
  }
</style>
