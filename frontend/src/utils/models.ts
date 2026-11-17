/**
 * Utility to clean and sanitize model names for display.
 * Strips Windows/POSIX filesystem paths and .gguf extensions.
 * E.g.: "E:\AI\Models\Ornith-1.5-35B-A3B-GGUF\Ornith-1.5-35B-Q4_K_M.gguf" -> "Ornith-1.5-35B-Q4_K_M"
 * "/models/qwen2.5-coder-32b.gguf" -> "qwen2.5-coder-32b"
 */
export function cleanModelName(name?: string | null): string {
  if (!name || typeof name !== 'string') return '';
  const trimmed = name.trim();
  if (!trimmed) return '';

  let base = trimmed;
  // If it's a file path with backslashes or slashes, get the file name
  if (base.includes('\\') || base.includes('/')) {
    base = base.split(/[\\/]/).pop() || base;
  }
  // Remove .gguf extension if present (case-insensitive)
  base = base.replace(/\.gguf$/i, '').trim();
  // A split model is launched from its part-1 file ("...-00001-of-00002"): show the model's name, not the part's.
  // Same rule as model_display_name in core/profiles/paths.py.
  base = base.replace(/-\d{5}-of-\d{5}$/i, '').trim();
  return base;
}

/**
 * Strips quantization tags (e.g., -Q4_K_M, _Q4_K_M, .Q8_0, -IQ4_XS) from model names
 * specifically for clean display in the chat screen, composer input box, and model picker.
 * E.g. "Qwen3.8-27B-Q4_K_M" -> "Qwen3.8-27B"
 *      "qwen3.8-27B_Q4_K_M" -> "qwen3.8-27B"
 */
export function formatChatModelDisplay(name?: string | null): string {
  const base = cleanModelName(name);
  if (!base) return '';

  return base
    .replace(/[-_.]gguf$/i, '')
    .replace(/[-_.](?:IQ\d+_[A-Za-z0-9_]+|Q\d+_[A-Za-z0-9_]+|Q\d+_\d+|Q\d+[A-Za-z_][A-Za-z0-9_]*|Q\d+|F(?:P)?(?:16|32)|BF16)(?:[-_.]gguf)?$/i, '')
    .replace(/[-_.]gguf$/i, '')
    .trim();
}
