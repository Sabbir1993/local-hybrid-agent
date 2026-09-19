"use strict";
/* ---------------- export session as markdown (chat + agent) ---------------- */

function exportActsMarkdown(acts) {
  if (!acts || !acts.length) return '';
  let out = '';
  acts.forEach(a => {
    if (a.type === 'tool_call') {
      out += `\n> 🔧 **${a.tool || 'tool'}**${a.args ? '`(' + JSON.stringify(a.args) + ')`' : ''}\n`;
    } else if (a.type === 'tool_result') {
      const r = typeof a.result === 'string' ? a.result : JSON.stringify(a.result);
      out += `> ↳ ${(r || '').slice(0, 500)}\n`;
    } else if (a.type === 'thought') {
      out += `\n> 💭 ${a.text || a.content || ''}\n`;
    } else if (a.type === 'step') {
      out += `\n**Step:** ${a.title || a.text || ''}\n`;
    } else if (a.type === 'lane') {
      out += `\n_lane: ${a.display || a.model || ''}_\n`;
    }
  });
  return out;
}

function buildSessionMarkdown(hist, { title, isAgentMode, projectName }) {
  const now = new Date();
  let md = `# ${title}\n\n`;
  md += `_Exported ${now.toLocaleString()} · Mode: ${isAgentMode ? 'Agent' : 'Chat'}` +
    (projectName ? ` · Project: ${projectName}` : '') + `_\n\n---\n\n`;

  hist.forEach(m => {
    const who = m.role === 'user' ? '🧑 User' : '🤖 Assistant';
    md += `### ${who}\n\n`;
    if (m.role === 'assistant' && m.modelDisplay) {
      md += `_Model: ${m.modelSource === 'cloud' ? '☁ ' : ''}${m.modelDisplay}_\n\n`;
    }
    if (m.reasoning) {
      md += `<details><summary>Reasoning</summary>\n\n${m.reasoning}\n\n</details>\n\n`;
    }
    if (m.role === 'assistant' && m.acts && m.acts.length) {
      md += exportActsMarkdown(m.acts) + '\n';
    }
    md += `${m.content || ''}\n\n`;
    if (m.role === 'assistant' && m.tps) {
      md += `_${m.ntok || ''} tok · ${m.tps.toFixed(1)} t/s · ${(m.secs || 0).toFixed(1)}s_\n\n`;
    }
    md += `---\n\n`;
  });
  return md;
}

function downloadMarkdown(md, title) {
  const blob = new Blob([md], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const fname = title.replace(/[^a-z0-9_-]+/gi, '_').slice(0, 60) || 'session';
  a.href = url;
  a.download = `${fname}_${new Date().toISOString().slice(0, 10)}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  toast('📄 Exported session as Markdown');
}

/* Export the currently open session (uses in-memory `messages`, already fully hydrated). */
function exportSessionMarkdown() {
  const hist = messages.filter(m => (m.role === 'user' || m.role === 'assistant') &&
    ((m.content || '').trim() || (m.acts && m.acts.length)));
  if (!hist.length) { toast('Nothing to export yet', true); return; }
  const title = (curSession && curSession.title) || (curProject && curProject.name) ||
    (agentMode ? 'Agent Session' : 'Chat Session');
  const md = buildSessionMarkdown(hist, { title, isAgentMode: agentMode, projectName: curProject && curProject.name });
  downloadMarkdown(md, title);
}

/* Export an arbitrary session from the sidebar list (may not be the one currently open). */
async function exportSessionMarkdownById(sid, titleHint) {
  try {
    const d = await (await fetch(`/control/sessions/${sid}/messages`)).json();
    const hist = (d.messages || [])
      .filter(m => (m.role === 'user' || m.role === 'assistant'))
      .map(m => {
        const meta = m.meta || {};
        return {
          role: m.role,
          content: m.content || '',
          reasoning: meta.reasoning || '',
          acts: meta.acts || [],
          tps: meta.tps,
          ntok: meta.ntok,
          secs: meta.secs,
          modelDisplay: meta.modelDisplay || undefined,
          modelSource: meta.modelSource || undefined,
        };
      })
      .filter(m => m.content.trim() || (m.acts && m.acts.length));
    if (!hist.length) { toast('Nothing to export in this session', true); return; }
    const title = titleHint || (agentMode ? 'Agent Session' : 'Chat Session');
    const md = buildSessionMarkdown(hist, { title, isAgentMode: agentMode, projectName: curProject && curProject.name });
    downloadMarkdown(md, title);
  } catch (e) {
    toast('Export failed: ' + e.message, true);
  }
}

window.exportSessionMarkdown = exportSessionMarkdown;
window.exportSessionMarkdownById = exportSessionMarkdownById;
