import { beforeEach, describe, expect, it, vi } from 'vitest';
import { reportEditError } from './editErrors';
import { project } from './project.svelte';
import { toasts } from './toasts.svelte';

beforeEach(() => {
  toasts.clear();
  project.editError = null;
  project.conflict = null;
});

describe('reportEditError', () => {
  it('shows a plain refusal as an error with its fix', () => {
    project.editError = { code: 'x', message: 'No.', fix: 'Fix: yes.', status: 422 };
    reportEditError();
    expect(toasts.items[0]).toMatchObject({ kind: 'error', message: 'No. Fix: yes.' });
  });

  it('turns a conflict into a prompt that applies the change again', async () => {
    const retry = vi.fn().mockResolvedValue(true);
    project.editError = { code: 'revision.conflict', message: 'm', fix: 'f', status: 409 };
    project.conflict = { retry };
    reportEditError();
    const toast = toasts.items[0];
    expect(toast).toMatchObject({ kind: 'warning', duration: null });
    expect(toast?.action?.label).toBe('Apply my change again');
    toast?.action?.onclick();
    expect(retry).toHaveBeenCalledTimes(1);
  });
});
