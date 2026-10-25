/** Small UI state shared across the shell: which overlays are open. Not part of the project, never sent to the server. */
class UiState {
  helpOpen = $state(false);
}

export const ui = new UiState();
