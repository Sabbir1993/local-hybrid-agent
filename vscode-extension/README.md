# A770 Agent for VS Code (minimal, 0.1.0)

Ask the A770 agent about the code you selected, or give it a task. Progress appears in the **A770 agent** output
panel and every approval (a command, an edit, a connector call, an administrator rule) is asked in a VS Code dialog.
There is no autocomplete, by design.

The agent works on your machine through the SSL Local Agent (Companion). Set the folder it works in (the active
project) in the web app first.

## Use
1. `A770: Set API token` and paste an API token an administrator issued you (`a770_pat_...`). It is kept in VS Code's
   SecretStorage, not in settings.
2. Settings: `a770.baseUrl` (https unless it is this machine; the token is sent with every request).
3. Select code, right-click **A770: Ask about the selection**, or run **A770: Run a task** from the command palette.
   Cancelling the progress notification stops the run on the server too.

## Safety
- Approval dialogs offer *Allow once* (and *Allow for this run* where the server permits it). Saved "always allow" rules
  can only be created in the web app.
- Bypass mode is not offered here.
- Text from the server and the model is stripped of escape and control characters before it is shown.

## Status
The logic in `lib.js` is unit-tested (`node tests/js/test_vscode_lib.js`). The editor wiring in `extension.js` has not
been run inside VS Code yet: press F5 from this folder in VS Code (Extension Development Host) to try it.
Not included: hunk-level diff review, a chat panel, reattaching to a run after a reload.
