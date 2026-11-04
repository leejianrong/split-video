import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

/** The generated recording: three tones of 32 s, separated by 4 s of silence (about 108 s, well under a megabyte). */
export const TONE_SECONDS = 32;
export const GAP_SECONDS = 4;
export const SONGS = 3;
export const GIG_NAME = 'gig.mp4';

/**
 * Generate the fixture recording with ffmpeg's lavfi sources (never committed). Three sine tones at different pitches,
 * the middle one louder and with a tremolo so the highlights pass has something to rank, each followed by true
 * silence. The video is a small test pattern. `-nostdin` and a hard timeout so a wedged ffmpeg cannot hold CI.
 */
export function generateGig(out: string): void {
  fs.mkdirSync(path.dirname(out), { recursive: true });
  const total = SONGS * TONE_SECONDS + (SONGS - 1) * GAP_SECONDS + GAP_SECONDS;
  const tone = (i: number, freq: number, vol: number, extra = '') =>
    `sine=frequency=${freq}:sample_rate=44100:duration=${TONE_SECONDS},` +
    `volume=${vol}${extra},apad=pad_dur=${GAP_SECONDS},atrim=0:${TONE_SECONDS + GAP_SECONDS}[a${i}]`;
  const filter = [
    tone(0, 330, 0.5),
    tone(1, 440, 0.9, ',tremolo=f=4:d=0.6'),
    tone(2, 550, 0.5),
    '[a0][a1][a2]concat=n=3:v=0:a=1[a]',
  ].join(';');
  const args = [
    '-nostdin',
    '-y',
    '-loglevel',
    'error',
    '-f',
    'lavfi',
    '-i',
    `testsrc2=size=320x240:rate=10:duration=${total}`,
    '-filter_complex',
    filter,
    '-map',
    '0:v',
    '-map',
    '[a]',
    '-t',
    String(total),
    '-c:v',
    'libx264',
    '-preset',
    'ultrafast',
    '-pix_fmt',
    'yuv420p',
    '-c:a',
    'aac',
    '-movflags',
    '+faststart',
    out,
  ];
  execFileSync('ffmpeg', args, { timeout: 120_000, stdio: ['ignore', 'inherit', 'inherit'] });
}

export const EXTRA_NAME = 'extra.mp4';

/** A second, different 8 s recording (colour bars and a 880 Hz tone) for the Media tab import test. */
export function generateExtra(out: string): void {
  fs.mkdirSync(path.dirname(out), { recursive: true });
  const args = [
    '-nostdin',
    '-y',
    '-loglevel',
    'error',
    '-f',
    'lavfi',
    '-i',
    'smptebars=size=320x240:rate=10:duration=8',
    '-f',
    'lavfi',
    '-i',
    'sine=frequency=880:sample_rate=44100:duration=8',
    '-c:v',
    'libx264',
    '-preset',
    'ultrafast',
    '-pix_fmt',
    'yuv420p',
    '-c:a',
    'aac',
    '-shortest',
    out,
  ];
  execFileSync('ffmpeg', args, { timeout: 60_000, stdio: ['ignore', 'inherit', 'inherit'] });
}
