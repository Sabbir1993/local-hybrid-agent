/* sec-help.js - "How it works" content for the Settings sections (not Capabilities, see cap-help.js).
   Same modal, keyed by the panel id. Load after cap-help.js. */

// small SVG kit for the section diagrams (uses the .rtf-* classes from settings-pro.css)
const SD = {
  box(x, y, w, h, cls, t1, t2) {
    return `<g class="rtf-node ${cls}"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="10"/>
      <text x="${x + w / 2}" y="${y + (t2 ? h / 2 - 3 : h / 2 + 4)}" text-anchor="middle" class="rtf-t1">${t1}</text>
      ${t2 ? `<text x="${x + w / 2}" y="${y + h / 2 + 12}" text-anchor="middle" class="rtf-t2">${t2}</text>` : ''}</g>`;
  },
  lbl(x, y, t, a = 'start') { return `<text x="${x}" y="${y}" text-anchor="${a}" class="rtf-lbl">${t}</text>`; },
  svg(h, edges, body, label) {
    return `<svg viewBox="0 0 780 ${h}" class="rtf-svg" role="img" aria-label="${label}">
      <defs><marker id="rtf-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="rtf-head"/></marker></defs>
      <g class="rtf-edges" marker-end="url(#rtf-arr)">${edges}</g>${body}</svg>`;
  },
};

function agentsDiagram() {
  const { box, lbl } = SD;
  return SD.svg(380,
    '<path d="M220 38H290"/><path d="M490 38H560"/><path d="M660 66V92H390V118"/><path d="M390 168V200"/>' +
    '<path d="M390 248V274H185V300"/><path d="M390 248V274H595V300"/>',
    box(20, 10, 200, 56, 'rtf-n', 'Create an agent', 'name, persona, instructions') +
    box(290, 10, 200, 56, 'rtf-n', 'Choose its tools', 'what it is allowed to use') +
    box(560, 10, 200, 56, 'rtf-n', 'Give it a work folder', 'on your device') +
    box(290, 118, 200, 50, 'rtf-cpu', 'Saved to your account', 'only you can use it') +
    box(290, 200, 200, 48, 'rtf-main', 'Pick it in Agent mode', 'then send a task') +
    box(60, 300, 250, 56, 'rtf-cpu', 'Thinks on a local model', 'cloud only if an admin allows it') +
    box(470, 300, 250, 56, 'rtf-exec', 'Tools run on your device', 'inside the agent folder only') +
    lbl(250, 268, 'thinking', 'end') + lbl(530, 268, 'doing', 'start'),
    'Custom agent flow: create, choose tools and folder, save, pick in Agent mode, then it runs locally with tools on your device');
}

function knowledgeDiagram() {
  const { box, lbl } = SD;
  const col = (x, items) => items.map(([y, c, a, b]) => box(x, y, 260, 44, c, a, b)).join('');
  return SD.svg(430,
    '<path d="M190 78V110"/><path d="M190 154V186"/>' +
    '<path d="M590 78V104"/><path d="M590 148V170"/><path d="M590 214V236"/><path d="M590 280V302"/><path d="M590 346V368"/>' +
    '<path d="M320 208H390V258H460" class="rtf-esc"/>',
    lbl(190, 20, 'Adding knowledge', 'middle') + lbl(590, 20, 'Asking a question', 'middle') +
    col(60, [[34, 'rtf-n', 'Add a document', 'file, pasted text or URL'], [110, 'rtf-exec', 'Tag with roles', 'who may use it'], [186, 'rtf-cpu', 'Indexed for search', 'ready to use']]) +
    col(460, [[34, 'rtf-main', 'You ask a question', ''], [104, 'rtf-exec', 'Knowledge on for this chat?', 'auto, on or off'], [170, 'rtf-exec', 'Is your role allowed?', 'only tagged roles'],
      [236, 'rtf-cpu', 'Best passages found', 'searched in the index'], [302, 'rtf-exec', 'Card and ID numbers masked', 'checked when read'], [368, 'rtf-main', 'Model answers from them', '']]) +
    lbl(374, 244, 'searched', 'start'),
    'Knowledge base flow: add, tag, index on the left; ask, check role, search, mask and answer on the right');
}

function guardDiagram() {
  const { box, lbl } = SD;
  return SD.svg(560,
    '<path d="M390 56V80"/><path d="M390 128V170"/><path d="M290 192H220"/><path d="M390 214V250"/><path d="M290 272H220"/>' +
    '<path d="M390 294V340"/><path d="M390 388V420"/><path d="M390 468V500"/>',
    box(290, 10, 200, 46, 'rtf-main', 'Your prompt', 'text and files') +
    box(290, 80, 200, 48, 'rtf-exec', 'Input rules start', 'for your role and user') +
    box(290, 170, 200, 44, 'rtf-n', 'Prohibited prompt type?', '') +
    box(20, 170, 200, 44, 'rtf-exec', 'Refused', 'no model is called') +
    box(290, 250, 200, 44, 'rtf-n', 'Cloud-block match?', 'only when a cloud model is used') +
    box(20, 250, 200, 44, 'rtf-exec', 'Blocked from the cloud', 'text stays on this PC') +
    box(290, 340, 200, 48, 'rtf-main', 'Model answers', 'local or cloud') +
    box(290, 420, 200, 48, 'rtf-exec', 'Output rules', 'sensitive parts redacted') +
    box(290, 500, 200, 46, 'rtf-main', 'You see the cleaned answer', '') +
    lbl(258, 186, 'yes', 'end') + lbl(398, 238, 'no', 'start') + lbl(258, 266, 'yes', 'end') + lbl(398, 322, 'no', 'start'),
    'Sanitizer flow: input rules refuse or block, the model answers, output rules redact');
}

Object.assign(CAP_HELP, {
  'sec-sampling': {
    title: 'Chat and sampling',
    lead: 'The system prompt and the sampling settings used for every chat and agent request.',
    flow: [['You send a message', 'chat or agent', 'you'], ['System prompt added', 'your standing instructions', 'main'], ['Sampling settings', 'temperature, top-p and so on', 'guard'], ['Model answers', 'in that style', 'cpu']],
    steps: [['System prompt', 'Text placed at the start of every request. Use it for tone, language and rules the model should always follow.'], ['Sampling', 'A low temperature gives steady, repeatable answers. Higher values give more varied ones.'], ['Apply', 'Changes are used from the next request.']],
    notes: [['Change one at a time', 'Change one value at a time, so you can tell what helped.']],
  },
  'sec-live-monitor': {
    title: 'Realtime monitor and VRAM',
    lead: 'A live view of what the models and the hardware are doing right now.',
    flow: [['Model servers run', 'on this PC', 'agent'], ['Telemetry collected', 'load, memory, speed', 'cpu'], ['Shown live', 'in this section', 'main']],
    steps: [['Inference', 'See which model is running, its speed in tokens per second and how well the prompt cache is working.'], ['Hardware', 'GPU load and video memory (VRAM) use, per GPU.'], ['Act on it', 'If VRAM is nearly full, use a smaller model or a shorter context in Model launch config.']],
    notes: [['Read only', 'This page shows state. It changes nothing.']],
  },
  'sec-cfg': {
    title: 'Model launch config',
    lead: 'The start-up settings for the local model server (llama-server).',
    flow: [['Edit a setting', 'context size, GPU layers', 'you'], ['Saved to the profile', 'a JSON file', 'cpu'], ['Restart the model', 'to apply', 'guard'], ['Server starts', 'with the new values', 'main']],
    steps: [['Adjust', 'Change launch parameters such as context length or how many layers go to the GPU.'], ['Save', 'Settings are written to the model profile.'], ['Restart', 'They apply only when the model server starts again. Running models are not changed.']],
    notes: [['Memory', 'A longer context and more GPU layers both use more VRAM. Check the Realtime monitor after a change.']],
  },
  'sec-lanes': {
    title: 'Models and jobs',
    lead: 'Choose which model does each job, on this PC or in the cloud, and whether answers get double-checked.',
    flow: [['A request arrives', 'chat, agent, image', 'you'], ['Job decides the lane', 'main, executor, vision', 'guard'], ['Model runs it', 'local or cloud', 'main'], ['Answer check', 'optional second look', 'cpu']],
    steps: [['Assign', 'For each job pick a local model or a cloud model.'], ['Cloud', 'Cloud models come from the providers you add in Cloud Models.'], ['Check', 'Turn on the answer check to have a model review answers before you see them.']],
    notes: [['Missing model', 'If a cloud model is not listed, add it to its provider in Cloud Models first.']],
  },
  'sec-cloud': {
    title: 'Cloud models',
    lead: 'Connect cloud providers that use the OpenAI-compatible API, and say which lane each one serves.',
    flow: [['Add a provider', 'web address and API key', 'you'], ['Key stored for you', 'kept out of git', 'cpu'], ['Models listed', 'you choose which to use', 'guard'], ['Bound to a lane', 'in Models and jobs', 'main']],
    steps: [['Provider', 'Enter the provider address and your API key.'], ['Models', 'Add the model names you want to use.'], ['Lanes', 'Choose the lane each model serves, then pick it in Models and jobs.']],
    notes: [['Your keys', 'Providers and keys are saved to your own settings file, not shared with other users.'], ['Data leaves this PC', 'Anything sent to a cloud model goes to that provider. Sensitive text can be blocked in the Input and Output Sanitizer.']],
  },
  'sec-devices': {
    title: 'Companion devices',
    lead: 'The machines paired with your account. Agent tools that touch files or commands run on them.',
    flow: [['Install the companion', 'on your PC', 'you'], ['Pair it', 'each gets its own key', 'guard'], ['Agent tools run there', 'files, shell, browser', 'cpu'], ['Revoke any time', 'if lost or sold', 'main']],
    steps: [['Pair', 'Open the companion app on a machine and pair it with your account.'], ['Use', 'Agent tools that touch files or commands run on that device, never on the server.'], ['Revoke', 'Remove a device here and its key stops working.']],
    notes: [['One key per machine', 'Revoking one device does not affect the others.']],
  },
  'sec-custom-agents': {
    diagram: agentsDiagram,
    title: 'Custom agents',
    lead: 'Create your own agents, each with a persona, chosen tools and a work folder.',
    flow: [['Create an agent', 'name, persona, tools', 'you'], ['Pick tools and lanes', 'what it may use', 'guard'], ['Choose a work folder', 'on your device', 'cpu'], ['Use it in Agent mode', 'select it in chat', 'main']],
    steps: [['Design', 'Write the persona and instructions, then choose which tools the agent can use.'], ['Folder', 'Give it a work folder so its files stay in one place.'], ['Run', 'Select the agent in Agent mode. By default custom agents run on local models.']],
    notes: [['Per user', 'Agents belong to the user who made them.']],
  },
  'sec-knowledge': {
    diagram: knowledgeDiagram,
    title: 'Knowledge base',
    lead: 'Company documents, pasted text and web pages the assistant can draw on, tagged by role.',
    flow: [['Add a document', 'file, text or URL', 'you'], ['Tagged by role', 'who may see it', 'guard'], ['Indexed', 'ready to search', 'cpu'], ['Used in answers', 'when relevant', 'main']],
    steps: [['Add', 'Upload files, paste text or give a URL.'], ['Tag', 'Tag each item with the roles allowed to use it. Only those users get answers from it.'], ['Use', 'When a question relates to an item, the assistant reads it and answers from it.']],
    notes: [['Sensitive data', 'Card and ID numbers are checked when knowledge is read for an answer. Keep documents free of them anyway.']],
  },
  'sec-guard': {
    diagram: guardDiagram,
    title: 'Input and output sanitizer',
    lead: 'Admin-set rules that check what goes into a model and what comes back.',
    flow: [['You send a prompt', 'text and files', 'you'], ['Input rules', 'cloud-block, prohibited types', 'guard'], ['Model answers', 'local or cloud', 'main'], ['Output rules', 'redactions applied', 'cpu']],
    steps: [['Block', 'Cloud-block patterns stop matching text from being sent to cloud models.'], ['Refuse', 'Prohibited prompt types are not answered.'], ['Redact', 'Model replies are scrubbed of sensitive patterns before you see them.'], ['Scope', 'Rules can apply to a role or to a single user.']],
    notes: [['Test first', 'These rules protect payment and personal data. Test a rule on sample text before you rely on it.']],
  },
  'sec-users': {
    title: 'Users and roles',
    lead: 'Create users, give them roles, and decide what each role may do.',
    flow: [['Create a user', 'name and password', 'you'], ['Assign a role', 'a set of permissions', 'guard'], ['User signs in', 'sees what the role allows', 'main'], ['Actions audited', 'who did what', 'cpu']],
    steps: [['Users', 'Add people and set their role.'], ['Roles', 'A role is a list of permissions. Change it and every user with that role is affected.'], ['Audit', 'Sensitive changes are written to the audit log.']],
    notes: [['Least privilege', 'Give people the smallest role that lets them do their job. Some powerful permissions are never given automatically.']],
  },
  'sec-session': {
    title: 'Session security',
    lead: 'Signs users out after a period of inactivity.',
    flow: [['User is idle', 'no activity', 'you'], ['Timer runs', 'the minutes you set', 'guard'], ['Signed out', 'must sign in again', 'cpu']],
    steps: [['Turn on', 'Switch idle sign-out on and set the minutes.'], ['Activity', 'A running chat or agent task counts as activity, so long jobs are not cut off.'], ['Expire', 'After the idle time the session ends and the sign-in page appears.']],
    notes: [['Shared machines', 'A shorter time is safer on shared computers.']],
  },
  'sec-runner': {
    title: 'Code runner',
    lead: 'An isolated machine that runs the code the chat writes for charts and data work.',
    flow: [['Chat writes code', 'for a chart or analysis', 'agent'], ['Sent to the runner', 'a separate machine', 'guard'], ['Runs there', 'isolated', 'cpu'], ['Result returns', 'chart or table', 'main']],
    steps: [['Pair', 'Pair a runner machine with this app.'], ['Run', 'When the chat needs code run, it sends it to the runner, never to this server.'], ['Without one', 'If no runner is paired, the chat has no code tool.']],
    notes: [['Why separate', 'Generated code is not trusted, so it runs away from your data and the app.']],
  },
  'sec-mfa': {
    title: 'Two-factor authentication',
    lead: 'A code from an authenticator app at sign-in, plus single-use backup codes.',
    flow: [['Enter password', 'first step', 'you'], ['Enter app code', 'changes often', 'guard'], ['Signed in', 'session starts', 'main'], ['Lost the phone?', 'use a backup code once', 'cpu']],
    steps: [['Set up', 'Scan the QR code with an authenticator app and confirm with a code.'], ['Backup codes', 'Save the backup codes somewhere safe. Each works once.'], ['Require', 'Admins can require two-factor sign-in for everyone.']],
    notes: [['Keep backups', 'Without the app and the backup codes, an admin has to reset your access.']],
  },
});

// puts a "How it works" button in a Settings section header (called from settings-page.js)
window.secHelpAttach = function (h2, id) {
  if (!CAP_HELP[id] || h2.querySelector('.sec-how')) return;
  const b = document.createElement('button');
  b.type = 'button'; b.className = 'btn ghost sec-how'; b.textContent = 'How it works';
  b.onclick = e => { e.stopPropagation(); capHelpOpen(id, b); };
  h2.appendChild(b);
};
