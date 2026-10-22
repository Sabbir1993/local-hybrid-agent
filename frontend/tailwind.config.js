/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: ['class', '[data-theme]'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        panel: 'var(--panel)',
        panel2: 'var(--panel2)',
        border: 'var(--border)',
        'border-subtle': 'var(--border-subtle)',
        text: 'var(--text)',
        'text-muted': 'var(--text-muted)',
        dim: 'var(--dim)',
        accent: 'var(--accent)',
        'accent-glow': 'var(--accent-glow)',
        'accent-bg': 'var(--accent-bg)',
        green: 'var(--green)',
        amber: 'var(--amber)',
        red: 'var(--red)',
        'bg-input': 'var(--bg-input)',
        'bg-code': 'var(--bg-code)',
        'btn-bg': 'var(--btn-bg)',
        'btn-border': 'var(--btn-border)',
        'user-bubble': 'var(--user-bubble)',
        'user-bubble-border': 'var(--user-bubble-border)',
      },
    },
  },
  plugins: [],
}
