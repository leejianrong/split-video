import { playbackApi, type ChunkApi } from './playbackApi';
import { playhead } from './playhead.svelte';

/**
 * Chunk length of the playback grid, in seconds. Chunks start at multiples of it, so a replay or a seek asks for ranges
 * the server already cached (SPL-70 keys chunks by exact range). 8 s renders in about 1.5 s (about 5x real time), so a
 * cold start or a seek to an uncached place is ready in under 2 s, while a chunk plays for 8 s: the next one is always
 * ready long before it is needed. Longer chunks would make the first wait longer; shorter ones swap more often.
 */
export const CHUNK_SECONDS = 8;
/** Chunks rendered ahead of the one playing. With the one playing and the two ahead, 3 of the server's 8 cached chunks are in use. */
export const LOOKAHEAD = 2;
/**
 * Start the next chunk this long before the playing one ends. A paused element needs about this long after `play()` to
 * put out sound and the next frames, so the old one is left running (hidden) until then rather than going silent.
 */
const SWAP_LEAD = 0.1;
/** A tail shorter than this is not worth a chunk (the server refuses ranges under 0.1 s). */
const MIN_TAIL = 0.1;
/** How long the old element stays loaded after a swap, so its last 0.1 s plays out under the next one's start. */
const RELEASE_MS = 300;

/** One `<video>` element, as far as playback needs it. */
export interface VideoSlot {
  /** Resolves once the first frame is decodable, paused at 0. Rejects when the file cannot be played. */
  load(src: string): Promise<void>;
  seek(time: number): Promise<void>;
  play(): Promise<void>;
  pause(): void;
  /** Drop the source and free its decoder. */
  clear(): void;
  /** The `src` it is loaded with, or null. Lets a late `load()` answer be told apart from the one that matters. */
  readonly source: string | null;
  readonly time: number;
  readonly duration: number;
  readonly ended: boolean;
}

/** The main track's own file, for the draft shown while a chunk renders. `at` is the source time of the playhead. */
export interface DraftSource {
  url: string;
  at: number;
  /** The timeline second where this file stops being the picture (the main clip ends). */
  until: number;
  toTimeline(sourceTime: number): number;
}

export interface PlaybackDeps {
  api: ChunkApi;
  getTime(): number;
  setTime(seconds: number): void;
  /** Call `callback` about every frame; returns the stop function. */
  everyFrame(callback: () => void): () => void;
  objectUrl: { create(blob: Blob): string; revoke(url: string): void };
}

interface Chunk {
  index: number;
  start: number;
  end: number;
  status: 'starting' | 'rendering' | 'fetching' | 'ready' | 'failed';
  jobId: string | null;
  abort: AbortController;
  url: string | null;
}

export type PlaybackState = 'paused' | 'buffering' | 'playing';
/** What the frame shows: the SPL-71 still, a chunk's video (also when paused on it), or the draft main-track video. */
export type Showing = 'still' | 'chunk' | 'draft';

const REAL: PlaybackDeps = {
  api: playbackApi,
  getTime: () => playhead.time,
  setTime: (t) => playhead.advance(t),
  everyFrame: (callback) => {
    const timer = setInterval(callback, 16);
    return () => clearInterval(timer);
  },
  objectUrl: { create: (b) => URL.createObjectURL(b), revoke: (u) => URL.revokeObjectURL(u) },
};

const message = {
  blocked: 'The browser blocked playback. Press play to start the preview.',
  failed: 'The preview could not be prepared. Press play to try again.',
};

/**
 * Plays the composite of the timeline by chaining server-rendered chunks (SPL-70) on a fixed grid. Two `<video>`
 * elements are double-buffered: the chunk playing is in one, the next is loaded and paused at its first frame in the
 * other, and at the join the next one starts and the old one is released. Chunks up to `LOOKAHEAD` ahead are
 * requested one render at a time (the server has one preview worker), the current one first; chunks no longer needed
 * are cancelled for real (the job and its ffmpeg stop). While the wanted chunk renders, the main track's own file plays
 * as a draft (no overlays) when the browser can, otherwise the still stays with "Preparing preview".
 *
 * While playing, the video is the single source of truth for the playhead; a user seek (`seeked`) takes over.
 * An edit (a new revision) drops every chunk and rebuffers at the same time: playback goes on, as a draft or the
 * still, until the edited chunk is ready.
 */
export class PlaybackStore {
  state = $state<PlaybackState>('paused');
  showing = $state<Showing>('still');
  /** Which of the two chunk elements is on screen, or null. */
  activeSlot = $state<0 | 1 | null>(null);
  message = $state<string | null>(null);

  #deps: PlaybackDeps;
  #slots: [VideoSlot, VideoSlot] | null = null;
  #draftSlot: VideoSlot | null = null;
  #draftFor: (time: number) => DraftSource | null = () => null;

  #path: string | null = null;
  #revision: number | null = null;
  #duration = 0;

  #wanted = false;
  #epoch = 0;
  // Plain bookkeeping, not UI state: nothing renders from it.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #chunks = new Map<number, Chunk>();
  #loaded: [Chunk | null, Chunk | null] = [null, null];
  #active: { slot: 0 | 1; chunk: Chunk } | null = null;
  #next: { slot: 0 | 1; chunk: Chunk } | null = null;
  #retiring: { slot: 0 | 1; chunk: Chunk } | null = null;
  #preloading = false;
  #activating = false;
  #draft: DraftSource | null = null;
  #drafting = false;
  // Plain bookkeeping, not UI state: nothing renders from it.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #draftFailed = new Set<string>();
  // Plain bookkeeping, not UI state: nothing renders from it.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #cancels = new Set<Promise<unknown>>();
  #stopTick: (() => void) | null = null;
  #written = NaN;

  constructor(deps: PlaybackDeps = REAL) {
    this.#deps = deps;
  }

  /** Give the store the elements it drives (`draft` plays the main track's own file) and how to find the draft. */
  attach(
    slots: [VideoSlot, VideoSlot],
    draft: VideoSlot | null,
    draftFor: (time: number) => DraftSource | null,
  ): void {
    this.#slots = slots;
    this.#draftSlot = draft;
    this.#draftFor = draftFor;
  }

  /** What is being played: a different path or revision drops every chunk. */
  sync(input: { path: string | null; revision: number | null; duration: number }): void {
    const changed = input.path !== this.#path || input.revision !== this.#revision;
    this.#duration = input.duration;
    if (!changed) return;
    const projectChanged = input.path !== this.#path;
    this.#path = input.path;
    this.#revision = input.revision;
    this.#invalidate();
    if (input.path === null || input.duration <= 0 || projectChanged) {
      this.#halt();
      this.showing = 'still';
      return;
    }
    if (this.#wanted) this.#rebuffer();
    else this.showing = 'still';
  }

  toggle(): void {
    if (this.state === 'paused') this.play();
    else this.pause();
  }

  play(): void {
    if (this.#wanted || this.#path === null || this.#duration <= 0 || !this.#slots) return;
    if (this.#deps.getTime() >= this.#duration - MIN_TAIL) this.#setTime(0);
    this.#wanted = true;
    this.message = null;
    this.state = 'buffering';
    this.#epoch++;
    this.#pump();
    void this.#settle();
  }

  /** Stop where the picture is and cancel the renders still running; chunks already loaded stay for a quick resume. */
  pause(): void {
    if (this.#wanted || this.state !== 'paused') this.#halt();
  }

  /** The user moved the playhead (a click on the ruler, Home/End). */
  seeked(time: number): void {
    if (time === this.#written || this.#path === null) return;
    this.#dropRetiring();
    this.#epoch++;
    this.#stopDraft();
    const slots = this.#slots;
    const inside = (c: Chunk) => time >= c.start && time < c.end;
    const holder = [this.#active, this.#next].find((h) => h && inside(h.chunk));
    if (holder && slots) {
      // Already in memory: a seek inside the video, no request.
      if (holder === this.#next) this.#swapTo(holder);
      const epoch = this.#epoch;
      const slot = slots[holder.slot];
      void slot.seek(time - holder.chunk.start).then(() => {
        if (epoch === this.#epoch && this.state === 'playing') void this.#playSlot(slot);
      });
    } else {
      this.#pauseActive();
      this.#active = null;
      this.activeSlot = null;
      this.showing = 'still';
      if (this.#wanted) this.state = 'buffering';
    }
    this.#pump();
    if (this.#wanted && this.state !== 'playing') void this.#settle();
  }

  /** The still for the paused playhead has arrived: stop showing the frozen video. */
  release(): void {
    if (this.state === 'paused') this.showing = 'still';
  }

  dispose(): void {
    this.#halt();
    this.#invalidate();
    this.#unload(0);
    this.#unload(1);
    this.#stopDraft();
    this.showing = 'still';
  }

  // ---- chunks ----

  #bounds(index: number): { start: number; end: number } | null {
    const start = index * CHUNK_SECONDS;
    if (start >= this.#duration - MIN_TAIL) return null;
    return { start, end: Math.min(start + CHUNK_SECONDS, this.#duration) };
  }

  /** The chunk record for `index`, created (not started) if the grid has one there. */
  #ensure(index: number): Chunk | null {
    const known = this.#chunks.get(index);
    if (known) return known;
    const b = this.#bounds(index);
    if (!b) return null;
    const chunk: Chunk = {
      index,
      ...b,
      status: 'starting',
      jobId: null,
      abort: new AbortController(),
      url: null,
    };
    return chunk;
  }

  /** Start what is wanted, one render at a time, and let go of what is not. */
  #pump(): void {
    if (this.#path === null) return;
    const cur = Math.floor(this.#deps.getTime() / CHUNK_SECONDS);
    // Plain bookkeeping, not UI state: nothing renders from it.
    // eslint-disable-next-line svelte/prefer-svelte-reactivity
    const wanted = new Set<number>();
    for (let i = cur; i <= cur + LOOKAHEAD; i++) if (this.#bounds(i)) wanted.add(i);
    for (const h of [this.#active, this.#next, this.#retiring]) if (h) wanted.add(h.chunk.index);
    for (const chunk of [...this.#chunks.values()]) if (!wanted.has(chunk.index)) this.#drop(chunk);
    if (!this.#wanted) return;
    const busy = [...this.#chunks.values()].some(
      (c) => c.status === 'starting' || c.status === 'rendering',
    );
    if (busy) {
      this.#preload();
      return;
    }
    for (let i = cur; i <= cur + LOOKAHEAD; i++) {
      const b = this.#bounds(i);
      if (!b || this.#chunks.has(i)) continue;
      const chunk = this.#ensure(i)!;
      this.#chunks.set(i, chunk);
      void this.#fetch(chunk);
      break;
    }
    this.#preload();
  }

  async #fetch(chunk: Chunk): Promise<void> {
    const path = this.#path!;
    const { signal } = chunk.abort;
    try {
      await Promise.allSettled([...this.#cancels]);
      const started = await this.#deps.api.start(
        path,
        { start: chunk.start, end: chunk.end },
        signal,
      );
      chunk.jobId = started.jobId;
      if (signal.aborted) {
        // Dropped while the request was in flight: the server queued the render anyway, so stop it.
        if (!started.cached) void this.#deps.api.cancel(started.jobId).catch(() => undefined);
        return;
      }
      if (!started.cached) {
        chunk.status = 'rendering';
        await this.#deps.api.finished(started.jobId, signal);
      }
      chunk.status = 'fetching';
      const blob = await this.#deps.api.fetch(path, started.key, signal);
      if (signal.aborted) return;
      chunk.url = this.#deps.objectUrl.create(blob);
      chunk.status = 'ready';
    } catch {
      if (signal.aborted) return;
      chunk.status = 'failed';
    }
    this.#pump();
    if (chunk.status === 'failed' && this.#wanted && chunk === this.#chunkAtTime()) {
      this.#halt();
      this.message = message.failed;
      return;
    }
    void this.#settle();
  }

  #chunkAtTime(): Chunk | undefined {
    return this.#chunks.get(Math.floor(this.#deps.getTime() / CHUNK_SECONDS));
  }

  /** Forget a chunk: stop its request, cancel its render for real, free its blob and its element. */
  #drop(chunk: Chunk): void {
    this.#chunks.delete(chunk.index);
    chunk.abort.abort();
    if (chunk.jobId && (chunk.status === 'rendering' || chunk.status === 'starting')) {
      const job = this.#deps.api.cancel(chunk.jobId).catch(() => undefined);
      this.#cancels.add(job);
      void job.then(() => this.#cancels.delete(job));
    }
    for (const i of [0, 1] as const) if (this.#loaded[i] === chunk) this.#unload(i);
    if (this.#active?.chunk === chunk) {
      this.#active = null;
      this.activeSlot = null;
    }
    if (this.#next?.chunk === chunk) this.#next = null;
    if (chunk.url) this.#deps.objectUrl.revoke(chunk.url);
    chunk.url = null;
  }

  /** Cut short the old element that is still finishing under a swap (a seek, pause or edit must not leave its sound). */
  #dropRetiring(): void {
    const old = this.#retiring;
    this.#retiring = null;
    if (old) this.#drop(old.chunk);
  }

  #invalidate(): void {
    this.#dropRetiring();
    this.#epoch++;
    this.#draftFailed.clear();
    this.#pauseActive();
    for (const chunk of [...this.#chunks.values()]) this.#drop(chunk);
    this.#active = null;
    this.#next = null;
    this.activeSlot = null;
  }

  #unload(i: 0 | 1): void {
    this.#slots?.[i].clear();
    this.#loaded[i] = null;
  }

  // ---- starting and swapping ----

  /** Get picture and sound going for the playhead if it is wanted and not already there. */
  async #settle(): Promise<void> {
    if (!this.#wanted || this.#activating || !this.#slots) return;
    if (this.state === 'playing' && this.#active) return;
    const time = this.#deps.getTime();
    const chunk = this.#chunks.get(Math.floor(time / CHUNK_SECONDS));
    if (chunk?.status === 'ready') await this.#activate(chunk);
    else if (!this.#drafting && !this.#draft) await this.#startDraft();
  }

  async #activate(chunk: Chunk): Promise<void> {
    const slots = this.#slots!;
    const epoch = this.#epoch;
    this.#activating = true;
    try {
      const known = ([0, 1] as const).find((i) => this.#loaded[i] === chunk);
      // Not in memory: load it beside the frame on screen (which stays until the new one plays), dropping the preloaded next.
      const slot: 0 | 1 = known ?? (this.#active?.slot === 0 ? 1 : 0);
      if (known === undefined) {
        this.#next = null;
        this.#unload(slot);
        await slots[slot].load(chunk.url!);
        if (epoch !== this.#epoch || chunk.status !== 'ready' || slots[slot].source !== chunk.url)
          return epoch === this.#epoch ? this.#unload(slot) : undefined;
        this.#loaded[slot] = chunk;
      }
      await slots[slot].seek(Math.max(0, this.#deps.getTime() - chunk.start));
      if (epoch !== this.#epoch) return;
      if (this.#active && this.#active.slot !== slot) slots[this.#active.slot].pause();
      if (this.#next?.slot === slot) this.#next = null;
      this.#draftSlot?.pause(); // the composite carries all the sound: never both at once
      await slots[slot].play().catch((cause: unknown) => {
        throw new BlockedError(cause);
      });
      if (epoch !== this.#epoch) return;
      this.#stopDraft();
      this.#active = { slot, chunk };
      this.activeSlot = slot;
      this.showing = 'chunk';
      this.state = 'playing';
      this.message = null;
      this.#startTick();
      this.#pump();
    } catch (cause) {
      if (epoch !== this.#epoch) return;
      this.#halt();
      this.message = cause instanceof BlockedError ? message.blocked : message.failed;
    } finally {
      this.#activating = false;
    }
    if (epoch !== this.#epoch) void this.#settle();
  }

  /** Load the chunk after the playing one into the idle element, paused at its first frame. */
  #preload(): void {
    const slots = this.#slots;
    const active = this.#active;
    if (!slots || !active || this.#next || this.#preloading || this.#retiring) return;
    const chunk = this.#chunks.get(active.chunk.index + 1);
    if (chunk?.status !== 'ready') return;
    const slot: 0 | 1 = active.slot === 0 ? 1 : 0;
    const epoch = this.#epoch;
    this.#preloading = true;
    if (this.#loaded[slot]) this.#unload(slot);
    let retry = false;
    const src = chunk.url!;
    slots[slot]
      .load(src)
      .then(() => {
        // Another load may have taken the element meanwhile (its `loadeddata` answers this call too): leave it alone.
        if (slots[slot].source !== src) return;
        if (epoch !== this.#epoch || chunk.status !== 'ready' || this.#active !== active) {
          slots[slot].clear();
          retry = true;
          return;
        }
        this.#loaded[slot] = chunk;
        this.#next = { slot, chunk };
      })
      .catch(() => slots[slot].clear())
      .finally(() => {
        this.#preloading = false;
        if (retry) this.#preload();
      });
  }

  /** The next chunk becomes the one playing, without starting it (a seek into it). */
  #swapTo(next: { slot: 0 | 1; chunk: Chunk }, atJoin = false): void {
    const old = this.#active;
    this.#active = next;
    this.#next = null;
    if (atJoin) return; // the old element stays on screen until the new one plays (a hidden element draws no frames)
    this.activeSlot = next.slot;
    if (old) this.#slots![old.slot].pause();
    this.#pump();
  }

  #startTick(): void {
    this.#stopTick ??= this.#deps.everyFrame(() => this.#onFrame());
  }

  #onFrame(): void {
    const slots = this.#slots;
    if (!slots) return;
    if (this.#draft && this.state !== 'paused' && !this.#active) return this.#onDraftFrame();
    const active = this.#active;
    if (!active || this.state !== 'playing') return;
    const slot = slots[active.slot];
    const length = Math.min(slot.duration || Infinity, active.chunk.end - active.chunk.start);
    this.#setTime(active.chunk.start + Math.min(slot.time, length));
    if (length - slot.time > SWAP_LEAD && !slot.ended) return;
    if (active.chunk.end >= this.#duration - MIN_TAIL) return this.#finish();
    const next = this.#next;
    if (!next) return this.#rebuffer(); // the next chunk is not ready: underrun
    this.#epoch++; // the old chunk's async work is stale
    this.#swapTo(next, true);
    this.#setTime(next.chunk.start); // the window of chunks to keep is that of the new one
    void this.#playSlot(slots[next.slot]).then(() => {
      if (this.#active === next) this.activeSlot = next.slot;
    });
    this.#retiring = active;
    setTimeout(() => this.#release(active), RELEASE_MS);
    this.#pump();
  }

  async #playSlot(slot: VideoSlot): Promise<void> {
    try {
      await slot.play();
    } catch {
      this.#halt();
      this.message = message.blocked;
    }
  }

  /** The chunk that just finished playing is not needed any more: free its blob and element. */
  #release(old: { slot: 0 | 1; chunk: Chunk }): void {
    if (this.#retiring === old) this.#retiring = null;
    if (this.#loaded[old.slot] === old.chunk && this.#active?.chunk !== old.chunk) {
      this.#drop(old.chunk);
    }
    this.#pump();
  }

  #finish(): void {
    this.#halt();
    this.#setTime(this.#duration);
  }

  /** Playback wants a chunk that is not there yet: hold the last frame, play the draft when there is one. */
  #rebuffer(): void {
    this.#dropRetiring();
    this.#epoch++;
    this.#stopDraft();
    this.#pauseActive();
    this.#active = null;
    this.activeSlot = null;
    this.showing = 'still';
    this.state = 'buffering';
    this.#pump();
    void this.#settle();
  }

  // ---- the draft: the main track's own file while a chunk renders ----

  async #startDraft(): Promise<void> {
    const slot = this.#draftSlot;
    const epoch = this.#epoch;
    const source = slot ? this.#draftFor(this.#deps.getTime()) : null;
    if (!slot || !source || this.#draftFailed.has(source.url)) return;
    this.#drafting = true;
    try {
      await slot.load(source.url);
      if (epoch !== this.#epoch || !this.#wanted) return slot.clear();
      await slot.seek(source.at);
      if (epoch !== this.#epoch) return slot.clear();
      await slot.play();
      if (epoch !== this.#epoch) return slot.pause();
      this.#draft = source;
      this.activeSlot = null;
      this.showing = 'draft';
      this.#startTick();
    } catch {
      this.#draftFailed.add(source.url); // not playable here (mkv, HEVC, a token): keep the still
      slot.clear();
    } finally {
      this.#drafting = false;
    }
  }

  #onDraftFrame(): void {
    const draft = this.#draft!;
    const slot = this.#draftSlot!;
    const time = draft.toTimeline(slot.time);
    this.#setTime(time);
    if (time < draft.until - SWAP_LEAD && !slot.ended) return;
    this.#stopDraft(); // the main clip ended: the next clip's draft, or the still until the chunk is ready
    void this.#startDraft();
  }

  #stopDraft(): void {
    if (this.#draft || this.#drafting) this.#draftSlot?.clear();
    this.#draft = null;
    if (this.showing === 'draft') this.showing = 'still';
  }

  // ---- stopping ----

  #pauseActive(): void {
    if (this.#active) this.#slots?.[this.#active.slot].pause();
  }

  /** Stop playing at the picture's own time and cancel the renders still running (loaded chunks stay). */
  #halt(): void {
    if (this.#wanted && this.#active && this.#slots) {
      const slot = this.#slots[this.#active.slot];
      this.#setTime(this.#active.chunk.start + slot.time);
    } else if (this.#draft && this.#draftSlot) {
      this.#setTime(this.#draft.toTimeline(this.#draftSlot.time));
    }
    this.#wanted = false;
    this.#dropRetiring();
    this.#epoch++;
    this.#pauseActive();
    this.#stopDraft();
    this.#stopTick?.();
    this.#stopTick = null;
    this.state = 'paused';
    this.message = null;
    for (const chunk of [...this.#chunks.values()]) if (chunk.status !== 'ready') this.#drop(chunk);
    if (this.showing === 'draft') this.showing = 'still';
  }

  #setTime(seconds: number): void {
    this.#deps.setTime(seconds);
    this.#written = this.#deps.getTime();
  }
}

class BlockedError extends Error {
  constructor(cause: unknown) {
    super('play() was rejected', { cause });
  }
}

export const playback = new PlaybackStore();
