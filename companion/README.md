# A770 Companion

Local Electron app that lets the A770 Dual Runtime server run file and shell
operations on *your* machine instead of the server's. See the server-side
half of this in `core/companion_bridge.py`.

## Run in dev

```
cd companion
npm install
set A770_SERVER_URL=https://your-server.example.com
npm start
```

On launch it opens a native window pointed at your server's root URL — the
server itself redirects to `/login` when you're not authenticated, so the
same window carries you through login and into the full chat/agent app.
Once logged in it connects a WebSocket back to the server using your session
cookie and sits in the system tray; closing the window hides it to the tray
instead of quitting (use the tray menu's "Quit" to actually exit).

## Package a Windows installer

```
npm run dist
```

Uses `electron-builder` (see the `build` key in `package.json`). Before
shipping to non-technical users:

- Replace `trayIcon()` in `main.js` with a real `.ico`/`.png` asset (currently
  a placeholder empty image).
- Set a real `publish.url` for `electron-updater` (a static file server or
  GitHub Releases) so installed copies auto-update.
- Code-sign the build so Windows SmartScreen doesn't warn on install.

## Known limitations (MVP)

- Only one companion per user account at a time — a second login replaces
  the first connection (see `core/companion_bridge.py`'s "second companion...
  replaces" comment).
- `fs.list` / `fs.grep` implement a minimal glob/regex, not full parity with
  Python's `pathlib.glob` — good enough for the common `*.py` / `**/*` cases
  the agent actually uses.
- Binary document extraction (xlsx/pdf/docx via `core/file_tools.py`) still
  runs server-side only; only plain-text read/write/edit/list/grep and shell
  commands are remoted to the companion in this version.
