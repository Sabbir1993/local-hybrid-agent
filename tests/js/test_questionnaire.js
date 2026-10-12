// node tests/js/test_questionnaire.js - static/js/markdown.js parseQuestionnaire (typed ```questionnaire card)
const assert = require('assert');
const path = require('path');
const { parseQuestionnaire } = require(path.join(__dirname, '..', '..', 'static', 'js', 'markdown.js'));

const block = (json) => '```questionnaire\n' + json + '\n```';
const good = JSON.stringify({
  title: 'Tell me about your cat',
  questions: [
    { id: 'age', type: 'radio', q: 'How old is it?', options: ['Under 1', '1-7', 'Over 7'] },
    { id: 'signs', type: 'checkbox', q: 'Any of these?', options: ['Vomiting', 'Poor coat'] },
    { id: 'w', type: 'text', q: 'Weight?' },
  ],
  submit_label: 'Get my plan',
});

// no fence -> null (caller keeps its old behaviour)
assert.strictEqual(parseQuestionnaire('Just an answer.'), null);

// closed block: prose without it, validated card
{
  const r = parseQuestionnaire('## Plan\nFeed well.\n\n' + block(good));
  assert.strictEqual(r.partial, false);
  assert.strictEqual(r.prose, '## Plan\nFeed well.');
  assert.strictEqual(r.quiz.title, 'Tell me about your cat');
  assert.strictEqual(r.quiz.submit, 'Get my plan');
  assert.deepStrictEqual(r.quiz.questions.map(q => q.kind), ['radio', 'checkbox', 'text']);
  assert.strictEqual(r.quiz.questions[1].multi, true);
  assert.deepStrictEqual(r.quiz.questions[2].options, []);
  assert.strictEqual(r.quiz.questions[0].qId, 'How old is it?');   // answers go back labelled by the question
}

// still streaming: hidden until the fence closes
{
  const r = parseQuestionnaire('Intro text.\n```questionnaire\n{"title":"x","quest');
  assert.strictEqual(r.partial, true);
  assert.strictEqual(r.quiz, null);
  assert.strictEqual(r.prose, 'Intro text.');
}

// invalid JSON / no usable question: the text is kept, nothing is lost
{
  const bad = 'Hi\n' + block('{not json');
  const r = parseQuestionnaire(bad);
  assert.strictEqual(r.quiz, null);
  assert.strictEqual(r.prose, bad);
  const empty = parseQuestionnaire(block('{"questions":[{"q":""}]}'));
  assert.strictEqual(empty.quiz, null);
}

// limits: 6 questions, 8 options, option text clipped; a choice question without options becomes text
{
  const many = { questions: Array.from({ length: 9 }, (_, i) => ({ q: 'Q' + i, options: Array.from({ length: 12 }, (_, j) => 'o' + j) })) };
  const r = parseQuestionnaire(block(JSON.stringify(many)));
  assert.strictEqual(r.quiz.questions.length, 6);
  assert.strictEqual(r.quiz.questions[0].options.length, 8);
  const t = parseQuestionnaire(block('{"questions":[{"q":"Pick","type":"radio","options":[]}]}'));
  assert.strictEqual(t.quiz.questions[0].kind, 'text');
  const long = parseQuestionnaire(block(JSON.stringify({ questions: [{ q: 'x'.repeat(500), options: ['y'.repeat(500)] }] })));
  assert.strictEqual(long.quiz.questions[0].qTitle.length, 200);
  assert.strictEqual(long.quiz.questions[0].options[0].length, 120);
}

console.log('questionnaire parsing: ok');
