export interface ParsedUserMessage {
  cleanText: string;
  images: string[];
  files: string[];
}

export function parseUserMessage(raw: string): ParsedUserMessage {
  if (!raw) return { cleanText: '', images: [], files: [] };

  const images: string[] = [];
  const files: string[] = [];

  // Match --- IMAGE: filename.png ---
  const imgRegex = /--- IMAGE:\s*([^\n\r]+?)\s*---([\s\S]*?)(?:--- END\s*[^\n\r]+?---|(?=--- IMAGE:)|(?=--- FILE:)|$)/g;
  let imgMatch: RegExpExecArray | null;
  while ((imgMatch = imgRegex.exec(raw)) !== null) {
    const name = imgMatch[1].trim();
    if (name && !images.includes(name)) images.push(name);
  }

  // Match --- FILE: filename.ext ---
  const fileRegex = /--- FILE:\s*([^\n\r]+?)\s*---([\s\S]*?)(?:--- END\s*[^\n\r]+?---|(?=--- IMAGE:)|(?=--- FILE:)|$)/g;
  let fileMatch: RegExpExecArray | null;
  while ((fileMatch = fileRegex.exec(raw)) !== null) {
    const name = fileMatch[1].trim();
    if (name && !files.includes(name)) files.push(name);
  }

  // Match [Attached Files] header if present
  const attFilesRegex = /\[Attached Files\]\s*([^\n\r]+)/g;
  let attMatch: RegExpExecArray | null;
  while ((attMatch = attFilesRegex.exec(raw)) !== null) {
    const name = attMatch[1].trim();
    if (name && !files.includes(name)) files.push(name);
  }

  // Match [Attached Files: filename.png] if present
  const attFilesInlineRegex = /\[Attached Files:\s*([^\n\r\]]+)\]/g;
  let attInlineMatch: RegExpExecArray | null;
  while ((attInlineMatch = attFilesInlineRegex.exec(raw)) !== null) {
    const names = attInlineMatch[1].split(',').map(s => s.trim()).filter(Boolean);
    names.forEach(n => {
      if (/\.(png|jpe?g|webp|gif|bmp|svg)$/i.test(n)) {
        if (!images.includes(n)) images.push(n);
      } else {
        if (!files.includes(n)) files.push(n);
      }
    });
  }

  // Clean the text displayed to the user
  let cleanText = raw
    .replace(/--- IMAGE:\s*[^\n\r]+?\s*---[\s\S]*?(?:--- END\s*[^\n\r]+?---|(?=--- IMAGE:)|(?=--- FILE:)|$)/g, '')
    .replace(/--- FILE:\s*[^\n\r]+?\s*---[\s\S]*?(?:--- END\s*[^\n\r]+?---|(?=--- IMAGE:)|(?=--- FILE:)|$)/g, '')
    .replace(/\[Attached Files\][\s\S]*?(?:--- END [^\n]+ ---|(?=\n\n)|$)/g, '')
    .replace(/\[Attached Files:[^\]]*\]/g, '')
    .trim();

  return { cleanText: cleanText || (images.length || files.length ? '' : raw), images, files };
}
