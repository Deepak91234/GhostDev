# Permissions and boundaries

## Actual trust boundary

Native macOS is the user-selected target. Accessibility can inject input into host apps; Screen Recording can see host windows. GhostDev captures only the foreground approved demo window and checks bundle/title/geometry/AX structure before input. Editor capture additionally requires its AXDocument file URL inside this task's copy; browser capture requires a web-area AXURL under this task's registered loopback preview. If those native accessibility attributes are unavailable, capture fails closed and needs local debugging. It does not capture an unapproved app, transmit secure AX text, or allow browser address typing, terminal input, run dialogs, arbitrary hotkeys, or arbitrary command strings.

**This is not a full host GUI sandbox.** Apps may open other files or access menus; same-named windows could satisfy title checks; an approved editor still has filesystem capabilities. Other apps can receive an input if focus changes between the final check and the OS event. The single-worker lease prevents competing GhostDev jobs, not all human/OS interaction. Use a dedicated demo user with no personal documents/accounts and close unrelated windows. Take over before interacting locally. A future production release needs a stronger native OS policy/confirmation design or a disposable guest.

Code checks use a separate restricted Docker container, which cannot make the native host desktop sandboxed. Docker access is needed on the **Mac only for checks**; the local Mac Ollama server does not require Docker. The operator starts Docker Desktop locally, subject to their device policy. No Docker socket is mounted inside the task container.

## Files

The local owner registers roots/allowed patterns/check argv in config.json. The phone submits project IDs. The model receives sanitized text files from a bounded copy. Absolute paths, traversal, symlinks, hidden files, node_modules, controller configuration, verification paths, private keys, and obvious secret filenames are excluded. These exclusions do not detect every secret embedded in ordinary source: register only prepared non-sensitive demo projects.

Editing uses an exact unique substring replacement inside existing registered files. The model cannot author shell commands or change controller-owned check definitions. Review/apply checks the original manifest and Git status/revision. Discard deletes only the task's copied project, preserving logs and evidence.

## Network

Controller default: loopback. Phone: HTTPS proxy on this Mac (Caddy for LAN; optional Tailscale for remote access). Sessions: HttpOnly/SameSite Strict/Secure cookies, local pairing confirmation, single-use secret, bounded pairing attempts, CSRF and Origin checks. Revoked sessions are rejected on HTTP and active WebSocket streams. Secrets never appear in URLs or browser storage.

Model: explicitly configured literal loopback on this Mac; non-loopback IPs, proxies, redirects, remote models, and cloud fallback are rejected. Hostnames are rejected to avoid DNS rebinding. An operator-supplied publisher string is metadata, not cryptographic provenance. The UI does not claim CPU/GPU execution is verified. Disable Ollama cloud features and verify local processing on the Mac.

Checks: no network. Preview: loopback, bounded project paths, no listing, CSP prevents fetch/form submission/frame embedding and isolates preview scripts. The Chrome process may still have OS/network capabilities; offline rehearsal requires actual host egress blocking while retaining LAN. Merely setting a local endpoint or turning off a browser UI feature is not proof of an offline demo.

## Local control and failures

Element-targeted clicks aim at the centre of the element's Accessibility frame, clipped to the captured window. GhostDev does not hit-test that point, so something drawn over the element, such as a sticky page header, would receive the click; the approval card names the targeted element, not whatever is on top. Elements Chrome reports as 1-point strips because they are scrolled out of view are not targetable. Use pages without overlays for the demo.

Each action binds task generation, observation ID, expected focus, exact arguments/digest, and a bounded execution lease. Approvals expire. Time spent awaiting approval extends the task deadline by exactly that wait, which each expiry bounds; time spent paused still counts. Pause/stop/takeover revoke the lease; resume re-observes. Checks are killed on lease revocation. A typing interruption can leave partial text; no uncertain action is automatically replayed. Stop cannot undo external or already completed effects.

Screenshots/action logs may contain prepared source/documents. Stored state is chmod 0700 and the database 0600. There is no at-rest encryption, automatic retention policy, remote account administration, security certification, or audit-proof signature chain in this MVP. Delete this dedicated demo state locally after reviewing evidence if no longer needed.

## Dependency audit

Expo builds include native/development tooling. The current registry audit reported advisories affecting transitive braces and node-forge; available registry versions remained inside the advisory ranges when checked. Patched uuid/decode-uri-component versions are overridden and verified with a rebuilt web export. GhostDev serves the static export through Python, and does not expose Metro, native signing tooling, or an Expo development server. The repository does not claim a zero-advisory dependency tree. Recheck upstream patches before deploying beyond this prepared hackathon environment.
