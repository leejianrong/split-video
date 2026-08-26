// The file picker: browse directories server-side (scoped to whatever root
// `split-video edit` was pointed at) and hand off to `onOpen` once a video
// is chosen. Server enforces the root boundary; this is just the UI over it.

import * as api from "./api.js";

export function createFilePicker({ listEl, pathEl, upBtn, statusEl, statusTextEl, onOpen }) {
  let parentPath = null;

  function render(listing) {
    parentPath = listing.parent;
    pathEl.textContent = "/" + listing.cwd;
    upBtn.disabled = parentPath === null;

    listEl.innerHTML = "";
    if (listing.entries.length === 0) {
      const li = document.createElement("li");
      li.className = "browse-empty";
      li.textContent = "No videos or folders here.";
      listEl.appendChild(li);
      return;
    }

    for (const entry of listing.entries) {
      const li = document.createElement("li");
      li.className = entry.is_dir ? "dir" : "file";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = entry.name;
      li.appendChild(name);
      li.addEventListener("click", () => {
        if (entry.is_dir) {
          load(entry.path);
        } else {
          open(entry.path, entry.name);
        }
      });
      listEl.appendChild(li);
    }
  }

  async function load(path) {
    render(await api.browse(path));
  }

  async function open(path, name) {
    listEl.classList.add("browse-list--busy");
    if (statusEl) {
      statusEl.classList.remove("hidden", "error");
      statusTextEl.textContent = `Opening "${name}"… this can take a while for a long recording.`;
    }
    try {
      await api.openFile(path);
    } catch (err) {
      listEl.classList.remove("browse-list--busy");
      if (statusEl) {
        statusEl.classList.add("error");
        statusTextEl.textContent = `Couldn't open "${name}": ${err.message}`;
      }
      return;
    }
    onOpen();
  }

  upBtn.addEventListener("click", () => {
    if (parentPath !== null) load(parentPath);
  });

  return { load };
}
