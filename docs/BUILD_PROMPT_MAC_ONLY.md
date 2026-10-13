# GhostDev — Mac-only build instructions

Continue building the provided GhostDev source as a phone-controlled native Mac desktop agent. This revision replaces the previous separate-inference-computer architecture. All product computation and storage run on the user's MacBook; the phone supplies the UI and commands. Do not require separate AI hardware, a remote shell, a hosted GPU, NVIDIA weights, or a cloud model API.

## Runtime and setup

- Run the FastAPI controller, durable SQLite worker, native macOS capture/input adapter, task workspaces, artifact store, browser preview, and code-check containers on the Mac.
- Run Ollama on that same Mac at `http://127.0.0.1:11434`. Default to locally downloaded `qwen3:4b`, text/Accessibility mode, thinking disabled, 8192-token context. Inspect the actual Mac OS/CPU/memory and benchmark a small real task; do not claim this default is universally sufficient. Larger or vision models are optional only after measured local compatibility.
- Require loopback inference endpoints. Reject remote/cloud Ollama models, external IPs, redirects, and silent cloud fallback. Disable Ollama cloud features. Verify local processing with the serving process and `ollama ps`, plus a run with internet egress blocked while LAN remains available.
- Migrate old configurations using `ghostdev use-local-model`; preserve project registration, phone origins, permissions, and saved task state. Never leave an old forwarding endpoint as the default deployment.
- The first phone route is same-Wi-Fi HTTPS through Caddy on the Mac, with the user's local CA trusted on their phone. Keep cookie/session/CSRF/Origin protection. Optional Tailscale runs on the phone and Mac for remote connectivity; it is not required for the basic workflow.
- Docker Desktop on the Mac is optional for desktop-only tasks and required for protected coding checks. No Docker means coding checks fail clearly; never execute arbitrary project code on the host as a fallback.

## Preserve the original product

The phone sends natural-language requests. The model observes Accessibility data (and screenshots only when a verified local VLM is enabled), proposes one strictly typed action, and the trusted controller validates and executes it. Actual native clicks, Unicode typing, scrolling, keyboard shortcuts, app launching/focusing, screenshots, approvals, and Stop remain core requirements. Do not replace desktop use with a file-only or Playwright-only demo. Use one display and the registered TextEdit/Chrome workflows first.

Preserve single-use pairing with local confirmation, revocable owner sessions, authenticated events/artifacts, exact action approvals, app/document/preview boundaries, stale observation rejection, generation/lease revocation, fresh observation after resume, and no input replay after interruption. Never eval/exec model code or allow terminal typing, arbitrary commands, password entry, or external submissions.

Native Mac GUI permissions are broad and are not a VM sandbox. Keep editor AXDocument inside the task copy and browser AXURL inside the registered preview; fail closed when attributes are unavailable. Verify actual Accessibility and Screen Recording permissions locally. If device management blocks them, report that blocker without circumventing it.

For coding: use a sanitized task copy, fixed protected checks outside model-writable paths, a restricted non-root/no-network runner, a real browser capture and GUI evidence, then review/apply only an exact diff hash with unchanged original content/base. Do not hardcode a repair or fabricate test results.

## Verify and deliver

Run the included automated suite, TypeScript checks and Expo web build. Then physically test capture, Retina/resize mapping, click/type/scroll, save/preview verification, Stop during inference, pause/resume, takeover, reconnect, and conflict-safe apply. Desktop tests with fixtures are not physical model evidence. Measure latency and memory on the actual Mac; reduce task length if the small local model is unreliable.

Keep voice disabled until the text-to-desktop workflow works. A later speech implementation must also run on the Mac and require explicit Send. Native app-store packaging is optional; the supported first client is the mobile browser.

Deliver runnable source, updated local configuration/setup commands, honest test evidence, and limitations. Preserve existing work. Implement and verify the next useful slice instead of stopping with a plan.
