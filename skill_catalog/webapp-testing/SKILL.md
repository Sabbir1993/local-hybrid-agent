---
name: webapp-testing
description: Run the web app you built, open it in the agent browser, and find and fix what's broken (console errors, failed requests, layout, flows).
triggers: test the app, check the page, open in browser, does it work, ui bug, frontend bug, verify the site, broken page, playwright
---

# Web App Testing Skill

Use this after building or changing anything a browser renders. Don't declare a web task done until you have looked at it.

1. **Start the app**: find the dev command (package.json `scripts.dev`/`start`, `manage.py runserver`, `php artisan serve`, `vite`, `flask run` ...) and start it with `run_shell` using `background: true` and `wait_for_port` set to the port it serves on (a dev server never exits, so a plain `run_shell` is killed after the timeout and the server dies with it). The result gives the process `pid`. If the app is already running (ask, or check the port), skip this step. If it's a plain HTML file, open it as `file:///<project path>/index.html` instead.
2. **Open it**: `browser_navigate` to `http://localhost:<port>/<page>`. A browser window opens on the user's screen so they can watch, and stays open until `browser_close`. Read the snapshot it returns: headings, buttons, inputs, and their `[ref=eN]` handles.
3. **Check for errors first**: `browser_console`. Every console error, uncaught exception, 404 asset or 5xx API call is a bug to explain or fix.
4. **Exercise the flow** the user asked for: `browser_click` / `browser_type` / `browser_select` / `browser_press` using refs from the latest snapshot (refs change after the page updates, so always use the newest snapshot). After each step, confirm the page changed as expected.
5. **Look at it**: `browser_screenshot` with a pointed `question` (e.g. "is the form aligned and fully visible?") to catch visual problems the snapshot can't show: overlap, cut-off text, missing styles, broken images.
6. **Mobile / responsive**: repeat key pages with `browser_navigate` and `device: "iphone-14"` or `"pixel-7"`.
7. **Fix → re-verify**: edit the code, reload with `browser_navigate` (or wait for hot reload with `browser_wait`), check `browser_console` again. Repeat until clean.
8. **Report**: say what you tested, what you found, what you fixed, and anything left unverified.

Rules:
- Never type passwords, API keys or real card numbers. For login or payment flows, ask the user to enter [PLACEHOLDER] test credentials / the payment gateway's sandbox card themselves, or use the project's own seed/test fixtures that are clearly test data.
- Only open local dev servers or sites the user asked you to test; other sites prompt the user on their machine.
- Close the browser with `browser_close` when finished (say so first if the user may still be looking at it).
- Stop any server you started in the background (`taskkill /PID <pid> /T /F` on Windows) when finished.
