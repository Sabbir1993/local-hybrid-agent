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
| Firewall (`tools/runner/lockdown.sh` and the host-only / private network) | only the app server reaches the runner, on one port; nothing goes out |
| Pinned certificate + token | only this app talks to the runner, and the app only talks to this runner; a changed certificate is refused |
| Pairing code bound to the certificate | a man in the middle cannot pair: the proof is `HMAC(code, certificate fingerprint)` |
| bubblewrap per job | the job has no network and sees only its own folder and the read-only system (plus, on Debian/Ubuntu, two read-only entries of /etc that numpy and matplotlib need) |
| CPU / memory / process / file limits, timeout | endless loops, fork bombs, huge files |
| Throwaway folder per job | nothing survives a job; one user's files are never in another's job |
| App-side vetting | card numbers in code, inputs or outputs are refused; only listed file types come back; output text is masked |
| Self-test | the app switches the runner on only when it proves it has no network and no secrets |
| Audit log | each run stores a hash of the code (not the code) and file counts |

What it does **not** do: it cannot read card numbers inside pictures, so a chart image of card data would pass; it does not make the
runner VM part of your PCI scope by itself - tell your QSA the VM exists, that it is segmented from the card-data environment and
that input files containing card numbers are refused. Check the Bangladesh Bank ICT guideline for what it expects of isolated
compute before production.

## Dev: a VM on your own machine (VirtualBox)

1. **Create the VM**: type *Linux*, version *Other Linux (64-bit)*, 2 CPUs, 1024 MB RAM, 8 GB disk (VDI, dynamically allocated).
   Attach the Alpine **"virt" x86_64 ISO** and boot it. Leave *Enable EFI* off unless you want it.
2. **Turn off everything the VM does not need** (VM must be powered off): Settings -> *Audio* off, *USB* off, *Shared Folders* empty,
   *General -> Advanced*: Shared Clipboard and Drag'n'Drop **Disabled**. Do **not** install Guest Additions.
3. **Network, two adapters**:
   - *Adapter 1*: **Host-only Adapter** (VirtualBox Host-Only Ethernet Adapter). This is the only link the app uses. The host-only
     network has no route to the internet. On Windows the host side is usually `192.168.56.1`; check with `ipconfig` under
     "VirtualBox Host-Only Network". That address is your `APP_IP`.
   - *Adapter 2*: **NAT**. Needed **only while `tools/runner/setup.sh` installs packages**. After setup, power off the VM and untick
     *Cable Connected* on Adapter 2 (or set it to *Not attached*). With it gone the VM has no internet at all.
4. **Install Alpine**: log in as `root`, run `setup-alpine` (interface `eth0` for DHCP is fine; pick `sys` mode on the disk), reboot and
   remove the ISO. Note the VM's host-only address with `ip addr show eth0` (usually `192.168.56.x`).
5. **Copy and run the setup** (from your PC, in a terminal; `scp` ships with Windows 11):
   ```
   scp -r tools/runner root@[RUNNER-ADDRESS]:/root/runner
   ```
   then on the VM: `sh /root/runner/setup.sh`. It needs the NAT adapter for `apk` and `pip`.
6. **Lock it down**, on the VM: `APP_IP=[HOST-ONLY-ADDRESS-OF-YOUR-PC] sh /root/runner/lockdown.sh`. The firewall drops everything
   else, including ssh. To keep ssh from your PC (for `pair-code` and updates) add `ADMIN_IP=[HOST-ONLY-ADDRESS-OF-YOUR-PC]`; without it use the
   VirtualBox console. Then power off, detach Adapter 2 as in
   step 3, and start the VM again. Take a **snapshot** now (clean, locked-down, paired-ready state).
7. **Pair**: Settings -> Code Runner, address `https://[RUNNER-ADDRESS]:8470` (the `192.168.56.x` address is private, so it is accepted).

Notes:
- Prefer a headless start (`VBoxManage startvm "<name>" --type headless`) once it is set up; you need the console (or ssh, if you
  set `ADMIN_IP`) only for `pair-code`.
- Do not use *Bridged Adapter* for the runner: it puts the VM on your office LAN with a route to everything there.
- If Hyper-V / Windows Sandbox / WSL2 is enabled on the PC, VirtualBox runs on top of it and is slower, but still works for a 1 GB
  runner.
- Your own firewall on the PC must allow the app to reach `[RUNNER-ADDRESS]:8470` on the host-only adapter; nothing on the PC
  should be reachable *from* the VM except through the VM's own rules (lockdown.sh allows only `APP_IP`, and only the runner port inbound).

## Production: a dedicated VM

Same steps, on its own VM (preferably another physical host) in a private subnet or security group: inbound from the app server on the
runner port only, no outbound. Do not put it on the application server.

## Platforms

Bubblewrap is **Linux only** (it uses Linux namespaces). The runner host must be Linux: a VM, a separate machine or a cloud instance.
There is no Windows or macOS runner: Windows has no equivalent sandbox here (Job Objects only limit CPU and memory) and macOS's
Seatbelt is not used by this runner. The application server itself can run on any OS; it never runs the code. `tools/runner/setup.sh` is written
for Alpine; the runner (`tools/runner/runner_server.py`) is plain Python and runs on Debian or Ubuntu too, started by hand
(`apt install bubblewrap python3`, then `python3 runner_server.py serve --max-jobs 2`) until an apt setup script exists.

## Sizing

One runner serves many users: every job is a throwaway sandbox on the same host, not a machine per user.

- `--max-jobs N` (default 2) is how many jobs run at once. A request that cannot get a slot within 30 s is answered "busy" and the
  model tells the user to retry. `/health` shows `running` and `max_jobs`.
- Each job may use up to about 1.4 GB, so keep the host's RAM above `max-jobs` x 1.5 GB (4 GB for 2 jobs) plus the OS.
- Most chat requests never reach the runner (PDF, PPTX, DOCX, XLSX, CSV and pictures are built by the app); it is only for custom code.
  If "busy" answers show up in real use, raise `--max-jobs` with the CPU count, or add a second runner host.

## Check the isolation: `tools/runner/verify.py`

On the Linux host, as the runner's unprivileged user (not root), before the runner is ever enabled:

```
python3 tools/runner/verify.py
```

It needs only bubblewrap and python3 (no server, no pairing, no network). It sends hostile programs through the runner's own job
code and prints PASS / FAIL per check: no network, the runner's environment not visible, no `/etc`, `/root`, `/home`, `/var` or state
folder, read-only system, symlinks not returned, fork bomb capped, endless loop killed, memory bomb stopped, oversize file stopped,
output file count capped, job folder wiped, concurrency capped. The exit code is 0 only if every critical check passed. A FAIL is a
real gap: do not enable the runner. The libraries line is only a warning (install the packages from `tools/runner/requirements.txt`).

## Test on Linux without a VM (WSL2 on Windows)

For a Windows-only developer PC, WSL2 gives a real Linux kernel to run `tools/runner/verify.py` and a test runner. It is a test aid, not the
production boundary (WSL has full outbound internet and, by default, access to your Windows drives).

1. `wsl --install -d Ubuntu-24.04` in an elevated PowerShell, reboot if asked, create the Linux user.
2. Limit it in `%UserProfile%\.wslconfig` (or the WSL Settings app): `memory=4GB`, `processors=2`, nested virtualization off, GUI off.
3. In Ubuntu: `sudo apt update && sudo apt install -y bubblewrap python3`.
4. `bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev true && echo works` must print `works`.
5. Run `python3 /mnt/<drive>/<path to repo>/tools/runner/verify.py`.
6. Optional hardening of the test box: `/etc/wsl.conf` with `[automount] enabled=false` and `[interop] enabled=false` after copying
   `tools/runner` into the Linux home, then `wsl --shutdown`.

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
