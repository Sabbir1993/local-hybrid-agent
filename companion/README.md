# A770 Companion

Local Electron app that lets the Local Agent server run file and shell
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

## File operations added in 0.2.103

- `fs.remove` deletes one file (never a folder); the server uses it to undo a file the agent created.
- `fs.verify` runs `node --check` on a `.js/.mjs/.cjs` file and reports the syntax error, if any. Other
  file types are checked by the server.
- Both go through the same approved-folder check as `fs.write`. An older companion answers "unknown op" and the
  server carries on without them.

## Known limitations (MVP)

- Only one companion per user account at a time — a second login replaces
  the first connection (see `core/companion_bridge.py`'s "second companion...
  replaces" comment).
- `fs.list` / `fs.grep` implement a minimal glob/regex, not full parity with
  Python's `pathlib.glob` — good enough for the common `*.py` / `**/*` cases
  the agent actually uses.
- Office documents in an agent project (xlsx/pptx/docx/pdf, `core/doc_tools/`)
  are read from and written back to this device over `fs.read_b64` /
  `fs.write_b64`; the server parses the bytes in memory and never stores them.
  Only chat-mode attachments live in the user's common space on the server.
