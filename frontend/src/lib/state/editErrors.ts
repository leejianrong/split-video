import { project } from './project.svelte';
import { toasts } from './toasts.svelte';

/**
 * Tell the user why the last edit did not apply. A lost race (409) is not an error to dismiss but a question: the
 * latest version is already on screen, so the toast offers to send the same change again on top of it.
 */
export function reportEditError(): void {
  const error = project.editError;
  if (!error) return;
  const conflict = project.conflict;
  if (!conflict) {
    toasts.error(`${error.message} ${error.fix}`.trim());
    return;
  }
  toasts.show({
    kind: 'warning',
    message: 'Changed somewhere else first, so your change was not applied.',
    duration: null,
    action: {
      label: 'Apply my change again',
      onclick: () => {
        void conflict.retry().then((ok) => {
          if (!ok && project.editError) reportEditError();
        });
      },
    },
  });
}
