/* ---------------- prompt-dots.js ----------------
 * A slim row of dots above the chat, one per prompt you sent. Hover shows the prompt, click scrolls to it, and the
 * dot for the prompt you are reading is highlighted as you scroll. Built from `messages`; chat.js calls
 * refreshPromptDots() after every render (the DOM child index of #chat-inner equals the messages index). */
(function () {
  const bar = document.getElementById('prompt-dots');
  const chat = document.getElementById('chat');
  if (!bar || !chat) return;

  let dots = [];        // [{ idx, el }] in prompt order
  let active = -1;
  let raf = 0;

  // the prompt as the bubble shows it: injected file/image blocks stripped
  function promptText(m) {
    let t = m.displayContent || m.content || '';
    if (t.includes('--- IMAGE:')) t = t.replace(/--- IMAGE:[\s\S]*?--- END [^\n]+ ---/g, '');
    if (t.includes('--- FILE:')) t = t.replace(/--- FILE:[\s\S]*?--- END [^\n]+ ---/g, '');
    if (t.includes('[Attached Files]')) t = t.replace(/\[Attached Files\][\s\S]*?--- END [^\n]+ ---(\s*\[NOTE:.*\])?/g, '');
    t = t.replace(/\s+/g, ' ').trim();
    return t ? (t.length > 90 ? t.slice(0, 90) + '…' : t) : '(attachment)';
  }

  function userIndexes() {
    const out = [];
    if (typeof messages === 'undefined') return out;
    for (let i = 0; i < messages.length; i++) if (messages[i].role === 'user' && !messages[i].compact) out.push(i);
    return out;
  }

  // force = the conversation may have changed (session switch, clear...); otherwise rebuild only when a prompt was added or removed
  function refresh(force) {
    const idxs = userIndexes();
    const same = !force && idxs.length === dots.length && (!idxs.length || idxs[idxs.length - 1] === dots[dots.length - 1].idx);
    if (same) return;
    bar.hidden = idxs.length < 2;
    bar.textContent = '';
    dots = idxs.map((idx, n) => {
      const el = document.createElement('button');
      el.type = 'button';
      el.className = 'pd-dot';
      el.dataset.idx = String(idx);
      const label = `Prompt ${n + 1} of ${idxs.length}: ${promptText(messages[idx])}`;
      el.title = label;
      el.setAttribute('aria-label', label);
      bar.appendChild(el);
      return { idx, el };
    });
    active = -1;
    schedule();
  }

  function target(idx) {
    const inner = document.getElementById('chat-inner');
    return inner && inner.children[idx];
  }

  function setActive(n) {
    if (n === active || n < 0 || n >= dots.length) return;
    if (active >= 0 && dots[active]) dots[active].el.classList.remove('on');
    active = n;
    const el = dots[n].el;
    el.classList.add('on');
    // keep the highlighted dot visible inside the strip (scrollLeft only: scrollIntoView could move the page)
    const left = el.offsetLeft - bar.offsetLeft, right = left + el.offsetWidth;
    if (left < bar.scrollLeft + 16) bar.scrollLeft = Math.max(0, left - 16);
    else if (right > bar.scrollLeft + bar.clientWidth - 16) bar.scrollLeft = right - bar.clientWidth + 16;
  }

  // the prompt being read = the last one whose top edge is above ~30% of the chat height (binary search on its position)
  function updateActive() {
    raf = 0;
    if (!dots.length || bar.hidden) return;
    const top = chat.getBoundingClientRect().top, line = chat.clientHeight * 0.3;
    let lo = 0, hi = dots.length - 1, found = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1, el = target(dots[mid].idx);
      if (el && el.getBoundingClientRect().top - top <= line) { found = mid; lo = mid + 1; } else hi = mid - 1;
    }
    setActive(found);
  }

  function schedule() {
    if (!raf) raf = requestAnimationFrame(updateActive);
  }

  bar.addEventListener('click', e => {
    const b = e.target.closest('.pd-dot');
    if (!b) return;
    const el = target(parseInt(b.dataset.idx, 10));
    if (!el) return;
    // the view is now where the user put it: a streaming answer must not pull it back down
    if (typeof chatUserScrolledUp !== 'undefined') chatUserScrolledUp = true;
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    const n = dots.findIndex(d => d.el === b);
    if (n >= 0) setActive(n);
  });
  chat.addEventListener('scroll', schedule, { passive: true });
  window.addEventListener('resize', schedule);

  window.refreshPromptDots = refresh;
  refresh(true);
})();
