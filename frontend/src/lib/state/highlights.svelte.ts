import { announce } from '../a11y/announce';
import {
  ApiError,
  getHighlightReviewsApiV1AssetsAssetIdHighlightReviewsGet,
  getHighlightsApiV1AssetsAssetIdHighlightsGet,
  getVideoTimelineApiV1ProjectTimelineGet,
  makeHighlightClipsApiV1ProjectHighlightsPost,
  putHighlightReviewApiV1AssetsAssetIdHighlightReviewsKeyPut,
  ifMatch,
  type AssetOut,
  type MadeOut,
  type ReviewsOut,
} from '../api/client';
import { baseName, formatClock } from '../format';
import { sourceToTimeline, type MappableClip } from '../timeline/rangeMap';
import { clipSelection as defaultClipSelection } from './clipSelection.svelte';
import { jobs as defaultJobs, type JobsStore } from './jobs.svelte';
import { playhead as defaultPlayhead } from './playhead.svelte';
import {
  project as defaultProject,
  toProjectError,
  type ProjectError,
  type ProjectStore,
} from './project.svelte';
import { rangeSelection as defaultRange } from './rangeSelection.svelte';

export type Preset = 'stream' | 'gig' | 'lecture';
export const PRESETS: readonly { id: Preset; label: string }[] = [
  { id: 'stream', label: 'Stream' },
  { id: 'gig', label: 'Gig' },
  { id: 'lecture', label: 'Lecture' },
];

/** How many suggestions to ask for. The engine returns fewer when no more windows stand out. */
export const SUGGESTION_COUNT = 12;

export type ReviewState = 'pending' | 'accepted' | 'dismissed';
export type LoadStatus = 'idle' | 'loading' | 'ready' | 'missing' | 'error';

/** One suggestion card: the engine's candidate plus the review the sidecar holds for it. */
export interface Suggestion {
  /** `h_<start ms>_<end ms>` of the scored window (docs/highlights.md): the identity the engine reviews by. */
  key: string;
  rank: number;
  /** The window the engine scored. */
  start: number;
  end: number;
  /** 0 to 100. */
  score: number;
  title: string;
  /** Why it was chosen, in the engine's words, best first. */
  reasons: string[];
  state: ReviewState;
  /** The edges in force: the user's adjusted ones, else the scored window. */
  from: number;
  to: number;
  adjusted: boolean;
}

/** The engine's key of a window (`highlight_review.key_of`): times have at most 3 decimals, so this is exact. */
export const keyOf = (start: number, end: number): string =>
  `h_${Math.round(start * 1000)}_${Math.round(end * 1000)}`;

const num = (value: unknown, fallback = 0): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback;

/** Candidates (docs/highlights.md, "The result") merged with the review document. */
export function toSuggestions(
  candidates: readonly Record<string, unknown>[],
  reviews: ReviewsOut['reviews'],
): Suggestion[] {
  return candidates.map((cand, i) => {
    const start = num(cand.start);
    const end = num(cand.end);
    const key = keyOf(start, end);
    const review = reviews[key];
    const reasons = Array.isArray(cand.reasons)
      ? cand.reasons
          .map((r) =>
            r && typeof r === 'object' ? String((r as { note?: unknown }).note ?? '') : '',
          )
          .filter(Boolean)
      : [];
    const hint = typeof cand.title_hint === 'string' ? cand.title_hint.trim() : '';
    const state = review?.state;
    return {
      key,
      rank: num(cand.rank, i + 1),
      start,
      end,
      score: Math.round(num(cand.score) * 100),
      title: hint || `Highlight ${num(cand.rank, i + 1)}`,
      reasons,
      state: state === 'accepted' || state === 'dismissed' ? state : 'pending',
      from: review?.adjusted?.start ?? start,
      to: review?.adjusted?.end ?? end,
      adjusted: review?.adjusted != null,
    };
  });
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/**
 * The Highlights tab's data (ux-spec #highlights): the engine's suggestions for one asset (`GET /assets/{id}/highlights`,
 * stored signals only, so cheap and repeatable) merged with the review state in the video's sidecar
 * (`GET /assets/{id}/highlight-reviews`, revisioned). Accepting, dismissing and adjusting write that sidecar; making a
 * short is one engine call that adds labelled 9:16 clips to the open timeline and accepts them. Nothing is kept in
 * the browser, so a reload shows the same cards in the same state.
 */
export class HighlightsStore {
  asset = $state<AssetOut | null>(null);
  status = $state<LoadStatus>('idle');
  error = $state<ProjectError | null>(null);
  suggestions = $state<Suggestion[]>([]);
  notes = $state<string[]>([]);
  preset = $state<Preset | null>(null);
  /** The keyword the engine looks for in speech and on-screen text; '' is none. */
  keyword = $state('');
  selectedKey = $state<string | null>(null);
  showDismissed = $state(false);
  /** Why the last review or make did not apply, with the fix hint; null after a good one. */
  actionError = $state<ProjectError | null>(null);
  busy = $state(false);

  visible = $derived(
    this.suggestions.filter((s) => (this.showDismissed ? true : s.state !== 'dismissed')),
  );
  dismissedCount = $derived(this.suggestions.filter((s) => s.state === 'dismissed').length);
  /** The suggestions nobody has acted on yet: what "Make a short from every highlight" makes. */
  pending = $derived(this.suggestions.filter((s) => s.state === 'pending'));

  #revision = 0;
  #ticket = 0;
  #queue: Promise<unknown> = Promise.resolve();
  #project: ProjectStore;
  #playhead: typeof defaultPlayhead;
  #range: typeof defaultRange;
  #clips: typeof defaultClipSelection;

  constructor(
    jobs: JobsStore = defaultJobs,
    project: ProjectStore = defaultProject,
    playhead = defaultPlayhead,
    range = defaultRange,
    clips = defaultClipSelection,
  ) {
    this.#project = project;
    this.#playhead = playhead;
    this.#range = range;
    this.#clips = clips;
    // "Find highlights" finished for the asset we show: read the new suggestions.
    jobs.onFinished((job) => {
      const shown = this.asset;
      if (shown && job.kind === 'analysis' && job.state === 'done' && job.label === shown.path) {
        void this.load(shown);
      }
    });
  }

  /** Read the suggestions and reviews of `asset` (again). */
  async load(asset: AssetOut): Promise<void> {
    const ticket = ++this.#ticket;
    if (this.asset?.id !== asset.id) {
      this.suggestions = [];
      this.selectedKey = null;
      this.#range.set(null);
    }
    this.asset = asset;
    this.status = 'loading';
    this.error = null;
    try {
      const path = { asset_id: asset.id };
      const [found, reviews] = await Promise.all([
        getHighlightsApiV1AssetsAssetIdHighlightsGet({
          path,
          query: {
            count: SUGGESTION_COUNT,
            ...(this.preset ? { preset: this.preset } : {}),
            ...(this.keyword ? { keywords: this.keyword } : {}),
          },
        }),
        getHighlightReviewsApiV1AssetsAssetIdHighlightReviewsGet({ path }),
      ]);
      if (ticket !== this.#ticket) return;
      this.#revision = reviews.data.revision;
      this.suggestions = toSuggestions(found.data.candidates, reviews.data.reviews);
      this.notes = found.data.notes;
      this.status = 'ready';
    } catch (cause) {
      if (ticket !== this.#ticket) return;
      this.suggestions = [];
      // 409 `analysis.missing`: nothing is stored to score yet, which is the empty state, not a failure.
      if (cause instanceof ApiError && cause.status === 409 && cause.code !== 'revision.conflict') {
        this.status = 'missing';
        return;
      }
      this.error = toProjectError(cause);
      this.status = 'error';
    }
  }

  setPreset(preset: Preset | null): Promise<void> {
    this.preset = preset;
    return this.asset ? this.load(this.asset) : Promise.resolve();
  }

  setKeyword(word: string): Promise<void> {
    this.keyword = word.trim();
    return this.asset ? this.load(this.asset) : Promise.resolve();
  }

  /** Make the card the one being looked at: the playhead goes to its start on the timeline and its range shows. */
  select(key: string | null, clips: readonly MappedClip[] = []): void {
    this.selectedKey = key;
    this.#showRange(key);
    const card = this.suggestions.find((s) => s.key === key);
    if (!card || !this.asset) return;
    const first = sourceToTimeline(clips, this.asset.id, card.from, card.to)[0];
    if (first) this.#playhead.seek(first.start);
  }

  /** The pointer is over a card (or left it): its range shows on the timeline meanwhile, the playhead stays. */
  hover(key: string | null): void {
    this.#showRange(key ?? this.selectedKey);
  }

  #showRange(key: string | null): void {
    const card = this.suggestions.find((s) => s.key === key);
    if (!card || !this.asset) this.#range.set(null);
    else this.#range.set({ assetId: this.asset.id, start: card.from, end: card.to });
  }

  /** Accept, dismiss or clear (pending again) one card; the sidecar is written, then the card shows it. */
  review(key: string, state: ReviewState): Promise<boolean> {
    const card = this.suggestions.find((s) => s.key === key);
    if (!card) return Promise.resolve(false);
    return this.#write(card, state, card.from, card.to);
  }

  /** Keep the user's own edges for a card (`from`, `to` in seconds of the recording); equal to the scored window resets. */
  adjust(key: string, from: number, to: number): Promise<boolean> {
    const card = this.suggestions.find((s) => s.key === key);
    if (!card) return Promise.resolve(false);
    return this.#write(card, card.state, from, to);
  }

  #write(card: Suggestion, state: ReviewState, from: number, to: number): Promise<boolean> {
    const assetId = this.asset?.id;
    if (!assetId) return Promise.resolve(false);
    const run = async (): Promise<boolean> => {
      this.actionError = null;
      const send = () =>
        putHighlightReviewApiV1AssetsAssetIdHighlightReviewsKeyPut({
          path: { asset_id: assetId, key: card.key },
          body: {
            state: state === 'pending' && from === card.start && to === card.end ? null : state,
            start: card.start,
            end: card.end,
            adjusted_start: from,
            adjusted_end: to,
          },
          ...ifMatch(this.#revision),
        });
      try {
        let response;
        try {
          response = await send();
        } catch (cause) {
          // Another writer (an agent, the CLI) saved reviews meanwhile: this write is per card, so take theirs and retry.
          if (!(
            cause instanceof ApiError &&
            cause.isRevisionConflict &&
            cause.currentRevision !== null
          )) {
            throw cause;
          }
          this.#revision = cause.currentRevision;
          response = await send();
        }
        this.#apply(response.data);
        return true;
      } catch (cause) {
        this.actionError = toProjectError(cause);
        announce(`${this.actionError.message} ${this.actionError.fix}`.trim());
        return false;
      }
    };
    const result = this.#queue.then(run, run);
    this.#queue = result;
    return result;
  }

  #apply(doc: ReviewsOut): void {
    this.#revision = doc.revision;
    for (const s of this.suggestions) {
      const review = doc.reviews[s.key];
      s.state =
        review?.state === 'accepted' || review?.state === 'dismissed' ? review.state : 'pending';
      s.from = review?.adjusted?.start ?? s.start;
      s.to = review?.adjusted?.end ?? s.end;
      s.adjusted = review?.adjusted != null;
    }
  }

  /**
   * Make cards into clips on the open timeline: `short` crops each to 9:16, `clip` ("Add to timeline") keeps the frame.
   * One engine call (`POST /project/highlights`, `If-Match: <project revision>`), so it is one new revision and one
   * undo step later. The made cards are accepted by the server. Resolves with what was made, or null.
   */
  async make(keys: readonly string[], kind: 'short' | 'clip'): Promise<MadeOut[] | null> {
    const asset = this.asset;
    const id = this.#project.id;
    const cards = keys
      .map((k) => this.suggestions.find((s) => s.key === k))
      .filter((s): s is Suggestion => s !== undefined);
    if (!asset || id === null || cards.length === 0 || this.busy) return null;
    this.busy = true;
    this.actionError = null;
    let made: MadeOut[] = [];
    const ok = await this.#project.edit(async (match) => {
      const { data } = await makeHighlightClipsApiV1ProjectHighlightsPost({
        query: { path: id },
        body: {
          asset: asset.id,
          kind,
          items: cards.map((c) => ({
            start: c.from,
            end: c.to,
            title: c.title.startsWith('Highlight ') ? '' : c.title,
            scored: { start: c.start, end: c.end },
          })),
        },
        ...match,
      });
      made = data.made;
      this.#apply(data.reviews);
      const { data: timeline } = await getVideoTimelineApiV1ProjectTimelineGet({
        query: { path: id },
      });
      return timeline;
    });
    this.busy = false;
    if (!ok) {
      this.actionError = this.#project.editError;
      return null;
    }
    const first = made[0];
    const clip = first
      ? this.#project.timeline?.tracks.flatMap((t) => t.clips).find((c) => c.id === first.clip)
      : undefined;
    if (clip) {
      this.#clips.select(clip.id);
      this.#playhead.seek(clip.start);
    }
    const what = kind === 'short' ? plural(made.length, 'short') : plural(made.length, 'clip');
    announce(
      `Added ${what} to the timeline${clip ? `, starting at ${formatClock(clip.start)}` : ''}`,
    );
    return made;
  }

  name(): string {
    return this.asset ? baseName(this.asset.path) : '';
  }
}

/** The part of a timeline clip that selecting a card needs (`ClipOut` fits). */
export type MappedClip = MappableClip;

export const highlights = new HighlightsStore();
