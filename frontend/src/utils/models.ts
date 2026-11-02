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
  return base;
}
