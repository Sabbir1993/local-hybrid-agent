/* history-clip.js - what earlier turns cost on every later turn.
 *
 * An agent turn re-sends the whole chat. Attached file text (up to 12,000 characters per file) and long assistant
 * answers from earlier turns are the bulk of that, and the model has already acted on them: the file is in the
 * project folder and can be read again. So older turns are sent shortened; the newest user message is untouched.
 * The chat on screen and the saved session are not changed, only the copy sent to the server.
 */
const HISTORY_ASSISTANT_CHARS = 1500;

function clipHistoryForRun(hist) {
  const last = hist.length - 1;
  return hist.map((m, i) => {
    if (i === last || !m || typeof m.content !== 'string') return m;
    let c = m.content;
    if (m.role === 'user') {
      // injected attachment blocks (same shapes chat.js strips from the bubble)
      c = c.replace(/--- IMAGE:[\s\S]*?--- END [^\n]+ ---/g, '[image from an earlier turn omitted]')
           .replace(/--- FILE: ([^\n]*?) ---[\s\S]*?--- END [^\n]+ ---/g, (_, n) => `[file ${n.trim()} from an earlier turn omitted: read it from the project folder]`)
           .replace(/\[Attached Files\][\s\S]*?--- END [^\n]+ ---(\s*\[NOTE:.*\])?/g, '[attached files from an earlier turn omitted: read them from the project folder]');
    } else if (m.role === 'assistant' && c.length > HISTORY_ASSISTANT_CHARS) {
      const head = Math.floor(HISTORY_ASSISTANT_CHARS * 0.7), tail = HISTORY_ASSISTANT_CHARS - head;
      c = c.slice(0, head).trimEnd() + '\n…[earlier answer shortened]…\n' + c.slice(-tail).trimStart();
    }
    return c === m.content ? m : { ...m, content: c };
  });
}
if (typeof window !== 'undefined') window.clipHistoryForRun = clipHistoryForRun;
