import { describe, expect, it, vi } from 'vitest';
import type { ClipOut, TrackOut } from '../api/client';
import { ClipDrag, DRAG_THRESHOLD_PX } from './drag.svelte';
import type { Lane } from './lanes';

const clip = (id: string, start: number, dur: number, extra: Partial<ClipOut> = {}): ClipOut =>
  ({
    id,
    asset: 'a_1',
    source: 'gig.mp4',
    in_point: start,
    out_point: start + dur,
    start,
    end: start + dur,
    duration: dur,
    source_duration: 3600,
    kind: 'media',
    label: '',
    text: '',
    ...extra,
  }) as ClipOut;

const lane = (
  id: string,
  clips: ClipOut[],
  magnetic: boolean,
  kind: Lane['kind'],
  top = 0,
): Lane => ({
  id,
  kind,
  name: id,
  track: {
    id,
    kind: kind === 'music' ? 'audio' : 'video',
    magnetic,
    clips,
    locked: false,
  } as TrackOut,
  clips,
  height: 40,
  top,
  canHide: false,
  canMute: false,
  main: magnetic,
});

function setup(pxPerSecond = 10) {
  const mainClips = [clip('a', 0, 100), clip('b', 100, 100), clip('c', 200, 100)];
  const main = lane('main', mainClips, true, 'video');
  const overlayA = lane(
    'ov1',
    [clip('t', 50, 20, { kind: 'text', in_point: 0, out_point: 20, source_duration: null })],
    false,
    'text',
    40,
  );
  const overlayB = lane('ov2', [], false, 'text', 80);
  const lanes = [overlayA, overlayB, main];
  const commit = vi.fn(() => Promise.resolve(true));
  const drag = new ClipDrag({
    pxPerSecond: () => pxPerSecond,
    lanes: () => lanes,
    laneAtY: (y) => lanes.find((l) => y >= l.top && y < l.top + l.height) ?? null,
    commit,
  });
  return { drag, commit, main, overlayA, overlayB, mainClips };
}

describe('ClipDrag', () => {
  it('a press that does not travel is a click and commits nothing', async () => {
    const { drag, commit, main, mainClips } = setup();
    drag.begin('move', main, mainClips[0]!, 100, 10);
    drag.update(100 + DRAG_THRESHOLD_PX - 1, 10, 10);
    expect(await drag.finish()).toBe('click');
    expect(commit).not.toHaveBeenCalled();
    expect(drag.active).toBeNull();
  });

  it('dragging the end edge commits one trim_clip with the new out point, measured in seconds', async () => {
    const { drag, commit, main, mainClips } = setup(10);
    drag.begin('trim-end', main, mainClips[1]!, 500, 10);
    drag.update(500 - 250, 10, 10); // 250 px at 10 px/s: 25 s shorter
    expect(drag.active).toMatchObject({ start: 100, end: 175, hint: 'Length 75.0 s' });
    expect(await drag.finish()).toBe('committed');
    expect(commit).toHaveBeenCalledTimes(1);
    expect(commit).toHaveBeenCalledWith([{ op: 'trim_clip', clip: 'b', out: 175 }], 'Trimmed');
  });

  it('dragging the start edge trims the in point', async () => {
    const { drag, commit, main, mainClips } = setup(10);
    drag.begin('trim-start', main, mainClips[1]!, 0, 10);
    drag.update(300, 10, 10);
    await drag.finish();
    expect(commit).toHaveBeenCalledWith([{ op: 'trim_clip', clip: 'b', in: 130 }], 'Trimmed');
  });

  it('moving a main-track clip past its neighbours commits a move_clip to the new position', async () => {
    const { drag, commit, main, mainClips } = setup(10);
    drag.begin('move', main, mainClips[0]!, 0, 10);
    drag.update(1500, 10, 10); // 150 s later: a's middle is now at 200, past b (150), before c (250)
    expect(drag.active).toMatchObject({ marker: 200, hint: 'Position 2 of 3', targetLaneId: null });
    expect(await drag.finish()).toBe('committed');
    expect(commit).toHaveBeenCalledWith([{ op: 'move_clip', clip: 'a', to_index: 1 }], 'Moved');
  });

  it('moving a clip on a free track changes its start, and to another lane names the track', async () => {
    const { drag, commit, overlayA, overlayB } = setup(10);
    const title = overlayA.clips[0]!;
    drag.begin('move', overlayA, title, 0, 50);
    drag.update(100, 50, 50); // 10 s later, same lane
    await drag.finish();
    expect(commit).toHaveBeenLastCalledWith([{ op: 'place_clip', clip: 't', start: 60 }], 'Moved');

    drag.begin('move', overlayA, title, 0, 50);
    drag.update(-1000, 90, 90); // before the start of the timeline, and down onto the second text lane
    expect(drag.active).toMatchObject({ start: 0, targetLaneId: overlayB.id });
    await drag.finish();
    expect(commit).toHaveBeenLastCalledWith(
      [{ op: 'place_clip', clip: 't', start: 0, track: 'ov2' }],
      'Moved',
    );
  });

  it('a clip cannot be dragged onto the main lane, and a move that changes nothing commits nothing', async () => {
    const { drag, commit, overlayA, main } = setup(10);
    const title = overlayA.clips[0]!;
    drag.begin('move', overlayA, title, 0, 50);
    drag.update(0, 130, 130); // over the main lane
    expect(drag.active?.targetLaneId).toBeNull();
    drag.update(30, 50, 50);
    drag.update(0, 50, 50);
    drag.update(0, 50 + DRAG_THRESHOLD_PX, 50);
    expect(await drag.finish()).toBe('nothing');
    expect(commit).not.toHaveBeenCalled();
    expect(main.track.magnetic).toBe(true);
  });

  it('reports a refused commit, and cancel leaves nothing behind', async () => {
    const { drag, commit, main, mainClips } = setup();
    commit.mockResolvedValueOnce(false);
    drag.begin('trim-end', main, mainClips[0]!, 0, 0);
    drag.update(-100, 0, 0);
    expect(await drag.finish()).toBe('rejected');
    drag.begin('trim-end', main, mainClips[0]!, 0, 0);
    drag.update(-100, 0, 0);
    drag.cancel();
    expect(drag.active).toBeNull();
    expect(await drag.finish()).toBe('nothing');
  });
});
