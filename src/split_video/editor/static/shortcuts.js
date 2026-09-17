// Global playback shortcuts (space bar didn't do anything before this —
// see #12) plus the "?" overlay documenting the full set, including the
// split-editing shortcuts timeline.js owns and binds itself.

const STEP_SECONDS = 5;

const SHORTCUTS = [
  { keys: "Space / K", desc: "Play / pause" },
  { keys: "J / L", desc: "Step back / forward 5s" },
  { keys: "S", desc: "Add a split at the playhead" },
  { keys: "Delete / Backspace", desc: "Remove the selected split" },
  { keys: "← / →", desc: "Nudge the selected split 0.1s (1s with Shift)" },
  { keys: "?", desc: "Toggle this list" },
];

export function createShortcuts({ player, shortcutsBtn, modalEl, overlayEl }) {
  function renderModal() {
    modalEl.innerHTML = "";
    const box = document.createElement("div");
    box.className = "modal-box";

    const title = document.createElement("h2");
    title.textContent = "Keyboard shortcuts";
    box.appendChild(title);

    const list = document.createElement("ul");
    list.className = "shortcuts-list";
    for (const { keys, desc } of SHORTCUTS) {
      const li = document.createElement("li");
      const kbd = document.createElement("kbd");
      kbd.textContent = keys;
      const span = document.createElement("span");
      span.textContent = desc;
      li.appendChild(kbd);
      li.appendChild(span);
      list.appendChild(li);
    }
    box.appendChild(list);

    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const closeBtn = document.createElement("button");
    closeBtn.className = "btn";
    closeBtn.textContent = "Close";
    closeBtn.addEventListener("click", close);
    actions.appendChild(closeBtn);
    box.appendChild(actions);

    modalEl.appendChild(box);
  }

  function isOpen() {
    return !modalEl.classList.contains("hidden");
  }

  function overlayBusy() {
    return !overlayEl.classList.contains("hidden");
  }

  function open() {
    if (overlayBusy()) return; // overlay is owned by something else right now (e.g. an export in progress)
    renderModal();
    modalEl.classList.remove("hidden");
    overlayEl.classList.remove("hidden");
  }

  function close() {
    modalEl.classList.add("hidden");
    modalEl.innerHTML = "";
    overlayEl.classList.add("hidden");
  }

  function toggle() {
    if (isOpen()) close();
    else open();
  }

  shortcutsBtn.addEventListener("click", toggle);
  overlayEl.addEventListener("click", () => {
    if (isOpen()) close();
  });

  document.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    if (e.key === "?") {
      e.preventDefault();
      toggle();
      return;
    }

    if (isOpen()) {
      if (e.key === "Escape") close();
      return;
    }
    if (overlayBusy()) return;

    if (e.key === " " || e.key === "k" || e.key === "K") {
      e.preventDefault();
      player.togglePlay();
    } else if (e.key === "j" || e.key === "J") {
      e.preventDefault();
      player.seek(player.currentTime - STEP_SECONDS);
    } else if (e.key === "l" || e.key === "L") {
      e.preventDefault();
      player.seek(player.currentTime + STEP_SECONDS);
    }
  });

  return { open, close, toggle };
}
