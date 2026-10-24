/* cap-help.js - "How it works" modal for each Capabilities menu (the Router has its own, richer one in capabilities.js).
   Content is plain data below; the modal reuses the .rtm-* styles from settings-pro.css. */
const CAP_HELP = {
  memory: {
    title: 'My memory',
    lead: 'Notes about you that follow you between chats, so you do not repeat yourself.',
    flow: [
      ['You add a note', 'a fact, a preference, a rule', 'you'],
      ['Saved to your account', 'private, never shared', 'cpu'],
      ['Loaded into new chats', 'and agent runs', 'main'],
      ['The model uses it', 'to answer or act', 'agent'],
    ],
    steps: [
      ['Add', 'Pick a topic (profile, preferences and so on), type the note and save. It is stored inline, no extra screen.'],
      ['Use', 'Your profile and preferences are added to the start of each chat. Agent runs can also see the list of your notes.'],
      ['Control', 'Edit or delete a note at any time. Deleted notes are no longer sent to the model.'],
    ],
    notes: [
      ['Private', 'Notes belong to your account. Other users never see them.'],
      ['Keep it short', 'Short, specific notes work best. Long text uses up context in every chat.'],
    ],
  },
  web: {
    title: 'Web browsing',
    lead: 'Lets the agent look things up online and read pages.',
    flow: [
      ['Agent needs fresh info', 'a question or a task', 'you'],
      ['web_search', 'DuckDuckGo, no key needed', 'cpu'],
      ['web_fetch', 'reads the page text', 'cpu'],
      ['Trimmed to the cap', 'then added to context', 'main'],
    ],
    steps: [
      ['Search', 'web_search finds pages. web_fetch opens one and returns its text.'],
      ['Budget', 'The table sets, per model, how many tokens web results may use and how many search rounds one run may take.'],
      ['Trim', 'Page text is cut at the character cap so a long page does not fill the context.'],
    ],
    notes: [
      ['ON / OFF', 'Turning the switch off removes both web tools from the agent.'],
      ['Lower the cap', 'Small local models do better with a lower cap and fewer rounds.'],
    ],
  },
  shell: {
    title: 'Shell execution',
    lead: 'Lets the agent run commands in your workspace, on your own device.',
    flow: [
      ['Agent proposes a command', 'run_shell', 'agent'],
      ['Safety check', 'blocked commands are refused', 'guard'],
      ['Allowed pattern?', 'yes: runs. no: asks you', 'main'],
      ['Runs on your device', 'output returns to the agent', 'cpu'],
    ],
    steps: [
      ['Check', 'Some commands are always refused. Secrets in the environment are not passed on to the command.'],
      ['Ask', 'With "Ask before running" on, you approve each command unless it matches an allowed pattern.'],
      ['Allow', 'Admins keep the global pattern list. You can choose "Always allow for me" in the prompt to add your own.'],
      ['Run', 'The command runs through your companion app in the project or agent folder, never on the server.'],
    ],
    notes: [
      ['Patterns', 'Use wildcards, for example <code>git *</code>. A single <code>*</code> allows everything, so use it with care.'],
      ['Terminal shell', 'The default shell setting only changes which shell the terminal opens.'],
    ],
  },
  skills: {
    title: 'Skills',
    lead: 'Reusable instruction packs the agent can pick up for a specific kind of task.',
    flow: [
      ['You ask for something', 'for example a report', 'you'],
      ['Agent sees skill list', 'names and short descriptions', 'agent'],
      ['Loads the matching skill', 'full instructions', 'cpu'],
      ['Follows it', 'to finish the task', 'main'],
    ],
    steps: [
      ['Listed', 'Only the name and description of each skill sit in context, so many skills cost little.'],
      ['Loaded', 'When a request matches, the agent reads the full skill and follows it.'],
      ['Switch', 'The ON / OFF switch turns all skills on or off for the agent.'],
    ],
    notes: [['Descriptions matter', 'The agent picks a skill by its description. A clear one gets used more reliably.']],
  },
  mcp: {
    title: 'MCP servers',
    lead: 'Connect outside tools and services to the agent through the Model Context Protocol.',
    flow: [
      ['Add a server', 'command or https address', 'you'],
      ['Connects', 'status shows live', 'cpu'],
      ['Tools appear', 'in the agent tool list', 'agent'],
      ['Agent calls them', 'results come back', 'main'],
    ],
    steps: [
      ['Add', 'Use <b>Add MCP server</b> or paste Claude Desktop style JSON.'],
      ['Scope', 'Admins choose Everyone or Just me. Other users add personal servers only. Secrets are stored for the owner only.'],
      ['Check', 'Each server shows its connection status and how many tools it offers.'],
    ],
    notes: [
      ['Packages', 'Non-admins can run npx or uvx servers only for packages an admin approved.'],
      ['Trust', 'A server can read what the agent sends it. Add only servers you trust.'],
    ],
  },
  plugins: {
    title: 'Plugins',
    lead: 'Python add-ons that give the agent extra tools.',
    flow: [
      ['Pick a plugin', 'installed or from the catalog', 'you'],
      ['Reviewed bundle', 'plugin_catalog folder', 'guard'],
      ['Loaded at start', 'from plugins/*/plugin.py', 'cpu'],
      ['New tools', 'available to the agent', 'agent'],
    ],
    steps: [
      ['Browse', 'The catalog lists reviewed bundles that ship with the app.'],
      ['Install', 'Installing copies the bundle into the plugins folder and loads it.'],
      ['Manage', 'Each plugin can be turned on or off from its row.'],
    ],
    notes: [['Runs as code', 'A plugin is Python running on the server. Install only reviewed ones.']],
  },
  task: {
    title: 'Task limits',
    lead: 'Guardrails that stop a run before it uses too much time, tokens or GPU.',
    flow: [
      ['Agent run starts', 'step by step', 'agent'],
      ['Checks every step', 'steps, time, tokens', 'guard'],
      ['Warns the model', 'at 50, 70 and 90% of tokens', 'main'],
      ['Stops cleanly', 'with a Continue button', 'cpu'],
    ],
    steps: [
      ['Max steps', 'The most tool steps one run may take.'],
      ['Wall-clock limit', 'The longest a run may last, in minutes. This is what protects the GPU. 0 turns it off.'],
      ['Token budget', 'Counts only new tokens each step, since the resent history is cached. The model may finish the item it is on before the run stops.'],
      ['Repeat guard', 'A run that repeats the same tool calls or saves the same file again and again is nudged, then stopped early.'],
    ],
    notes: [['Continue', 'A stopped run keeps its work. Press Continue to carry on from where it stopped.']],
  },
  library: {
    title: 'Agent library',
    lead: 'Shared agent profiles and prompt commands for the whole organisation.',
    flow: [
      ['Admin adds files', '.agents/agents and commands', 'you'],
      ['Allow and deny lists', 'choose what is offered', 'guard'],
      ['Users see them', 'as agents and slash commands', 'agent'],
      ['Every change audited', 'who changed what', 'cpu'],
    ],
    steps: [
      ['Profiles', 'An agent profile sets a role, instructions and tools for a kind of work.'],
      ['Commands', 'A command is a saved prompt users run with a slash.'],
      ['Lists', 'Allow and deny lists, with wildcards, decide which items users can see.'],
    ],
    notes: [['Admin only', 'Only admins can change the library. Everyone else sees the result.']],
  },
};

function capHelpHtml(key) {
  const t = CAP_HELP[key];
  const kindCls = { you: 'rtf-n', agent: 'rtf-main', cpu: 'rtf-cpu', main: 'rtf-main', guard: 'rtf-exec' };
  const flow = t.flow.map(([a, b, k], i) => `${i ? '<span class="cph-arr" aria-hidden="true"></span>' : ''}
      <div class="cph-node ${kindCls[k] || 'rtf-n'}"><b>${a}</b><span>${b}</span></div>`).join('');
  const steps = t.steps.map(([a, b], i) => `<li><span class="rtm-n">${i + 1}</span><div><b>${a}</b><p>${b}</p></div></li>`).join('');
  const notes = t.notes.map(([a, b]) => `<div class="rtm-knob"><b>${a}</b><span>${b}</span></div>`).join('');
  return `<div class="rtm-back" id="rt-help" role="dialog" aria-modal="true" aria-labelledby="rtm-title">
    <div class="rtm">
      <div class="rtm-head">
        <div><h2 id="rtm-title">How ${t.title} works</h2><p>${t.lead}</p></div>
        <button class="rtm-x" data-rt-close aria-label="Close">&times;</button>
      </div>
      <div class="rtm-body">
        <section><h3>The path</h3>${t.diagram ? `<div class="rtf-wrap">${t.diagram()}</div><div class="rtf-legend"><span class="rtf-k cpu"></span>Done by the app <span class="rtf-k exec"></span>Check or limit <span class="rtf-k main"></span>Model or you</div>` : `<div class="cph-flow">${flow}</div>`}</section>
        <section><h3>Step by step</h3><ol class="rtm-steps">${steps}</ol></section>
        <section><h3>Good to know</h3><div class="rtm-knobs">${notes}</div></section>
      </div>
    </div>
  </div>`;
}

function capHelpOpen(key, opener) {
  document.getElementById('rt-help')?.remove();
  document.body.insertAdjacentHTML('beforeend', capHelpHtml(key));
  const m = document.getElementById('rt-help');
  const prev = document.body.style.overflow;
  document.body.style.overflow = 'hidden';
  const close = () => { m.remove(); document.body.style.overflow = prev; document.removeEventListener('keydown', onKey); opener && opener.focus(); };
  const onKey = e => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  m.addEventListener('click', e => { if (e.target === m || e.target.closest('[data-rt-close]')) close(); });
  m.querySelector('.rtm-x').focus();
}

// adds a "How it works" button to every pane that has no help button yet
function capHelpAttach(box) {
  box.querySelectorAll('.cap-pane').forEach(p => {
    const key = p.dataset.pane;
    if (!CAP_HELP[key] || p.querySelector('.cap-how')) return;
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'btn ghost cap-how'; b.textContent = 'How it works';
    b.onclick = e => { e.stopPropagation(); capHelpOpen(key, b); };
    const card = p.querySelector('.cap-pane-card');
    if (card) card.insertBefore(b, card.querySelector('.cap-toggle, .cap-toggle-lib'));
    else if (p.querySelector('.am-full')) {
      // My memory: sit beside "Open full editor" so the pane starts at the same line as the others
      const full = p.querySelector('.am-full');
      const grp = document.createElement('div'); grp.className = 'am-btns';
      full.replaceWith(grp); grp.append(b, full);
    } else { const bar = document.createElement('div'); bar.className = 'cap-how-bar'; bar.appendChild(b); p.prepend(bar); }
  });
}
