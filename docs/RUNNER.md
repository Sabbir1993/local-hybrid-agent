# Isolated code runner

The chat can write Python for things the document tools cannot do (a custom chart, a calculation over an uploaded file, a merge or
a clean-up). **The app never runs that code itself.** It sends the code to a *runner*: a small service on a VM of its own, with no
internet, no secrets and no route to the application. With no runner paired, the chat has no code tool at all.

```
Users --https--> App server (app, database, keys, llama, builders)
                     |  one port, pinned certificate, token
                     v
                 Runner VM (private network, no internet, no secrets)
                     jobs: throwaway folder, bubblewrap, no network, limits
```

Agent mode is a different thing: it runs code on the *user's own PC* through the companion, with the user's approval.

## What protects what

| Layer | What it stops |
|---|---|
| Separate VM on a private network | the code can reach nothing of the app: no database, no keys, no llama servers |
| Firewall (`tools/runner/lockdown.sh` and the virtual switch) | only the app server reaches the runner, on one port; nothing goes out |
| Pinned certificate + token | only this app talks to the runner, and the app only talks to this runner; a changed certificate is refused |
| Pairing code bound to the certificate | a man in the middle cannot pair: the proof is `HMAC(code, certificate fingerprint)` |
| bubblewrap per job | the job has no network and sees only its own folder and the read-only system |
| CPU / memory / process / file limits, timeout | endless loops, fork bombs, huge files |
| Throwaway folder per job | nothing survives a job; one user's files are never in another's job |
| App-side vetting | card numbers in code, inputs or outputs are refused; only listed file types come back; output text is masked |
| Self-test | the app switches the runner on only when it proves it has no network and no secrets |
| Audit log | each run stores a hash of the code (not the code) and file counts |

What it does **not** do: it cannot read card numbers inside pictures, so a chart image of card data would pass; it does not make the
runner VM part of your PCI scope by itself - tell your QSA the VM exists, that it is segmented from the card-data environment and
that input files containing card numbers are refused. Check the Bangladesh Bank ICT guideline for what it expects of isolated
compute before production.

## Dev: a VM on your own machine (Hyper-V, Windows 11 Pro)

1. Create a **Generation 2** VM, 2 vCPU, 1 GB RAM (dynamic off), 8 GB disk, with the Alpine "virt" ISO. Secure Boot: set the template
   to *Microsoft UEFI Certificate Authority* (or turn Secure Boot off).
2. Network: an **internal or private virtual switch**, not the external one. The app server must reach the VM; the VM must not reach
   the internet. (Give it the external switch only while `tools/runner/setup.sh` installs packages, then move it.)
3. VM settings -> Integration Services: turn off *Guest services* and everything you do not need. No shared folders, no enhanced
   session, no clipboard sharing.
4. Install Alpine (`setup-alpine`), then copy `tools/runner` to the VM and run `sh tools/runner/setup.sh` as root.
5. `APP_IP=<address of your PC on that switch> sh lockdown.sh`.

## Production: a dedicated VM

Same steps, on its own VM (preferably another physical host) in a private subnet or security group: inbound from the app server on the
runner port only, no outbound. Do not put it on the application server.

## Pairing

On the runner: `python3 /opt/a770-runner/runner_server.py pair-code --state-dir /var/lib/a770-runner` prints a code valid for 10
minutes (5 wrong tries burn it). In the app: **Settings -> Code Runner**, enter the runner address (`https://[RUNNER-ADDRESS]:8470`)
and the code. The app pins the certificate, stores the token in the server's keychain (never in a file), runs the self-test and
switches the tool on when it passes. Re-pairing replaces the token.

Dev and production differ only in the address: set `RUNNER_URL` in the environment (it wins) or pair against the other address.
The token is stored per address. The address must be a private one (RFC 1918 / loopback); `sandbox.allow_public_runner: true` in
`config/app.json` lifts that only if your firewall guarantees the isolation.

## Fail closed

The chat offers `run_sandbox_python` only when the runner is paired, switched on, answering, and reports a working sandbox
(checked every 30 s). Anything else - unreachable, certificate changed, self-test failed - and the tool simply is not there.
The app never falls back to running code on its own host.

## Operating

- Logs: `/var/log/a770-runner.log` on the runner (no code, no file names, no request bodies).
- Re-test: Settings -> Code Runner -> *Test connection*.
- Forget: *Forget this runner* removes the token and the pinned certificate.
- Rotate: run `pair-code` on the runner and pair again.
- Update libraries: on the runner, with temporary internet, `pip install -U -r requirements.txt` and `apk upgrade`, then lock down again.
