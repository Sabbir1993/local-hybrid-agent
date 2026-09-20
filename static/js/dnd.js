/* drop target: drag a workspace file or code selection into the chat composer */
(function initComposerDrop() {
  const composer = document.querySelector('.composer');
  const input = $('input');
  if (!composer || !input) return;

  const isInternalDrag = e => {
    const types = e.dataTransfer && e.dataTransfer.types;
    return !!types && (types.includes('application/x-agent-file') || types.includes('application/x-agent-code'));
  };

  const insertAtCaret = text => {
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? input.value.length;
    input.value = input.value.slice(0, start) + text + input.value.slice(end);
    const caret = start + text.length;
    input.focus();
    input.setSelectionRange(caret, caret);
    input.dispatchEvent(new Event('input'));
  };

  composer.addEventListener('dragover', e => {
    if (!isInternalDrag(e)) return;
    e.preventDefault();
    composer.classList.add('drag-over');
  });
  composer.addEventListener('dragleave', () => composer.classList.remove('drag-over'));

  composer.addEventListener('drop', e => {
    if (!isInternalDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    composer.classList.remove('drag-over');

    const codeRaw = e.dataTransfer.getData('application/x-agent-code');
    if (codeRaw) {
      try {
        const { path, text, startLine, endLine } = JSON.parse(codeRaw);
        insertAtCaret(`--- CODE from ${path} (lines ${startLine}-${endLine}) ---\n${text}\n--- END ---\n@${path} `);
      } catch (err) { /* malformed payload, ignore */ }
      return;
    }

    const filePath = e.dataTransfer.getData('application/x-agent-file');
    if (filePath) insertAtCaret('@' + filePath + ' ');
  });
})();
