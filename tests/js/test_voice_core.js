// node tests/js/test_voice_core.js - static/js/voice-core.js (sentence feeder, VAD, endpointing, barge-in)
const assert = require('assert');
const path = require('path');
const core = require(path.join(__dirname, '..', '..', 'static', 'js', 'voice-core.js'));
const { voiceStripThink, voiceTakeSpeakable, VoiceEnergyVad, VoiceEndpointer, VoiceBargeDetector, voiceIsHallucination,
  voiceLooksUnfinished, voiceFillerLang, voiceHeardText, VOICE_FILLERS } = core;

// think blocks: closed ones removed, an open one hides the rest
assert.strictEqual(voiceStripThink('<think>plan</think>Hello.'), 'Hello.');
assert.strictEqual(voiceStripThink('Hi <think>half a thought'), 'Hi ');

// streaming answer: only complete sentences are taken; the rest waits
{
  let r = voiceTakeSpeakable('Hello there. How are', 0);
  assert.deepStrictEqual(r.pieces, ['Hello there.']);
  assert.strictEqual(r.spoken, 'Hello there. '.length);
  r = voiceTakeSpeakable('Hello there. How are you? Fine', r.spoken);
  assert.deepStrictEqual(r.pieces, ['How are you?']);
  r = voiceTakeSpeakable('Hello there. How are you? Fine', r.spoken, { final: true });
  assert.deepStrictEqual(r.pieces, ['Fine']);
  assert.strictEqual(r.spoken, 'Hello there. How are you? Fine'.length);
}

// a decimal is not a sentence end; end-of-buffer punctuation waits for the next token
assert.deepStrictEqual(voiceTakeSpeakable('It costs 3.5 dollars. Next', 0).pieces, ['It costs 3.5 dollars.']);
assert.deepStrictEqual(voiceTakeSpeakable('Version 3.', 0).pieces, []);
assert.deepStrictEqual(voiceTakeSpeakable('Version 3.', 0, { final: true }).pieces, ['Version 3.']);

// Bengali danda ends a sentence
assert.deepStrictEqual(voiceTakeSpeakable('আমি ভালো আছি। আপনি কেমন', 0).pieces, ['আমি ভালো আছি।']);

// first-clause rule: start at the first comma once ~8 words are in, only for the first piece
{
  const t = 'Sure, I can check that for you right now, and then summarise the result in detail';
  assert.deepStrictEqual(voiceTakeSpeakable(t, 0, { first: true }).pieces, ['Sure, I can check that for you right now,']);
  assert.deepStrictEqual(voiceTakeSpeakable(t, 0, { first: false }).pieces, []);
}

// run-on text without punctuation is cut at a space
{
  const t = ('word '.repeat(80)).trim();
  const r = voiceTakeSpeakable(t, 0);
  assert.ok(r.pieces.length >= 1 && r.pieces[0].length <= 220);
}

// code is never read; the note is said once
{
  const t = 'Here it is.\n```js\nconsole.log(1);\n```\nDone.';
  const r = voiceTakeSpeakable(t, 0, { final: true });
  assert.deepStrictEqual(r.pieces, ['Here it is.', 'The code is on the screen.', 'Done.']);
  // an unclosed fence waits, and is dropped at the end
  let w = voiceTakeSpeakable('Look.\n```js\nlet a', 0);
  assert.deepStrictEqual(w.pieces, ['Look.']);
  w = voiceTakeSpeakable('Look.\n```js\nlet a', w.spoken, { final: true });
  assert.deepStrictEqual(w.pieces, []);
  assert.strictEqual(w.spoken, 'Look.\n```js\nlet a'.length);
}

// list numbers and rules alone are not said
assert.deepStrictEqual(voiceTakeSpeakable('1. First item.\n---\n2. Second.', 0, { final: true }).pieces, ['First item.', 'Second.']);

// energy VAD: adapts to a noisy room, and a higher gate needs a louder voice
{
  const vad = new VoiceEnergyVad();
  const frame = (a) => Float32Array.from({ length: 512 }, (_, i) => a * Math.sin(i / 3));
  assert.strictEqual(vad.isVoiced(frame(0.001), 1, true), false);
  assert.strictEqual(vad.isVoiced(frame(0.2), 1, true), true);
  assert.strictEqual(vad.isVoiced(frame(0.02), 1, false), true);
  assert.strictEqual(vad.isVoiced(frame(0.02), 3, false), false);
  for (let i = 0; i < 200; i++) vad.isVoiced(frame(0.01), 1, true);      // steady hum below the minimum threshold
  assert.ok(vad.floor > 0.003 && vad.floor <= 0.05);
}

// endpointing: speech starts after ~128 ms, ends after 700 ms of silence, short blips are dropped
{
  const ep = new VoiceEndpointer();
  const events = [];
  const feed = (v, n) => { for (let i = 0; i < n; i++) { const e = ep.feed(v); if (e) events.push(e); } };
  feed(false, 10);
  feed(true, 3); assert.deepStrictEqual(events, []);              // 96 ms: not yet
  feed(true, 1); assert.deepStrictEqual(events, ['start']);       // 128 ms
  feed(true, 30); feed(false, 21); assert.deepStrictEqual(events, ['start']);   // 672 ms of silence: still talking
  feed(false, 1); assert.deepStrictEqual(events, ['start', 'end']);
  assert.strictEqual(ep.trailingSilence(), 22);                   // the silent tail that ended it, for trimming
  feed(true, 4); feed(false, 22);                                 // 128 ms of voice only: too short for an utterance
  assert.deepStrictEqual(events, ['start', 'end', 'start', 'short']);
  const long = new VoiceEndpointer({ maxMs: 640 });
  let ev = null;
  for (let i = 0; i < 40 && !ev; i++) ev = long.feed(true);
  assert.strictEqual(ev, 'start');
  for (let i = 0; i < 40 && ev !== 'max'; i++) ev = long.feed(true);
  assert.strictEqual(ev, 'max');
}

// barge-in needs 256 ms of unbroken speech
{
  const b = new VoiceBargeDetector();
  for (let i = 0; i < 7; i++) assert.strictEqual(b.feed(true), false);
  assert.strictEqual(b.feed(true), true);
  b.feed(false);
  assert.strictEqual(b.feed(true), false);
}

// hallucinations
for (const t of ['', ' ', 'Thank you.', 'thanks for watching!', '[BLANK_AUDIO]', '(music)', 'you']) assert.ok(voiceIsHallucination(t), t);
for (const t of ['yes', 'What is my balance?', 'আমার ব্যালেন্স কত']) assert.ok(!voiceIsHallucination(t), t);

// piece end offsets: what was said can be cut back to exactly the pieces that started playing
{
  const text = 'One two. Three four. Five six. Seven';
  const a = voiceTakeSpeakable(text, 0);
  assert.deepStrictEqual(a.pieces, ['One two.', 'Three four.', 'Five six.']);
  assert.deepStrictEqual(a.ends, ['One two. '.length, 'One two. Three four. '.length, 'One two. Three four. Five six. '.length]);
  const b = voiceTakeSpeakable(text, a.spoken, { final: true });
  assert.deepStrictEqual(b.ends, [text.length]);
  const ends = a.ends.concat(b.ends);
  assert.strictEqual(voiceHeardText(text, ends, 0), '');
  assert.strictEqual(voiceHeardText(text, ends, 1), 'One two.');
  assert.strictEqual(voiceHeardText(text, ends, 2), 'One two. Three four.');
  assert.strictEqual(voiceHeardText(text, ends, 9), text);          // more than exist: everything
  const code = voiceTakeSpeakable('A. ```x``` B.', 0, { final: true });
  assert.deepStrictEqual(code.pieces, ['A.', 'The code is on the screen.', 'B.']);
  assert.strictEqual(code.ends.length, 3);
  assert.ok(code.ends[0] < code.ends[1] && code.ends[1] < code.ends[2]);
}

// "not finished yet": trailing off or ending on a connector / filler, in English and Bangla
for (const t of ['I want to...', 'so I was thinking and', 'I need to check my balance because', 'um', 'আমি যেতে চাই কিন্তু', 'wait…'])
  assert.ok(voiceLooksUnfinished(t), t);
for (const t of ['What is my balance?', 'Thanks.', 'ঠিক আছে।', 'yes', 'check the disk space', '']) assert.ok(!voiceLooksUnfinished(t), t);

// fillers follow the language of the person
assert.strictEqual(voiceFillerLang('আমার ব্যালেন্স কত'), 'bn');
assert.strictEqual(voiceFillerLang('What is my balance'), 'en');
assert.ok(VOICE_FILLERS.en.length >= 2 && VOICE_FILLERS.bn.length >= 2);

console.log('OK test_voice_core');
