# GhostDev

Phone-controlled native Mac desktop agent. The AI, backend, desktop executor, files, and code-check containers all run on the same MacBook.

This repository contains an implemented first release: Expo mobile web UI, FastAPI controller, durable SQLite worker, native macOS window capture/Quartz input, local Ollama or OpenAI-compatible model adapter, scoped code tools, protected checks, and exact-diff review/apply. It is **not yet a verified physical MacBook demo**. This build environment is Linux, has no Mac desktop permissions, and has no Docker daemon. No separate AI computer is required. See `docs/VALIDATION.md` for exactly what was tested.

## Start on your MacBook

Use a dedicated demo user/session without personal apps/accounts. Requirements: Python 3.11+, Node 24.19+, Google Chrome, and optional Docker Desktop for coding checks. macOS native control is the sole desktop target. An institutional Mac may require IT permission for Accessibility/Screen Recording; No remote shell or separate inference computer is required.

From the extracted `ghostdev` directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e '.[mac]'
ghostdev init
```

The archive includes the compiled phone web UI, so a frontend build is optional. To modify/rebuild it:

```sh
cd apps/mobile
npm ci
npm run typecheck
npm run build
cd ../..
```

`npm ci` is a development/build step. Do not expose an Expo/Metro dev server to other networks. Serve the exported static app through GhostDev.

## Configure inference on this Mac

Install Ollama on the **same MacBook** from https://ollama.com/download/mac. The current Ollama macOS release requires macOS 14 or newer; Apple Silicon supports CPU/GPU inference and Intel Macs use CPU inference. Check the official requirements for your actual machine. No separate GPU or AI computer is required.

The default is a small local text model, `qwen3:4b`, using Accessibility observations rather than screenshot understanding. It is a starting configuration, not a benchmarked promise that this model completes every desktop task. Pull the weights locally:

```sh
ollama pull qwen3:4b
```

Disable Ollama cloud features. For the installed Mac application:

```sh
launchctl setenv OLLAMA_NO_CLOUD 1
launchctl setenv OLLAMA_HOST 127.0.0.1:11434
```

Quit and reopen Ollama after setting those values. If you instead run its server manually, quit the app first to avoid a port conflict, then keep this local process running:

```sh
OLLAMA_NO_CLOUD=1 OLLAMA_HOST=127.0.0.1:11434 ollama serve
```

Defaults in config.json: `model_backend: "ollama"`, `model_url: "http://127.0.0.1:11434"`, `model_name: "qwen3:4b"`, `model_publisher: "Qwen"`, `model_vision: false`, `model_think: false`, and an 8192-token context. Thinking is disabled for the default model to reduce work per action. If another model does not accept that control, set `model_think` to null. Adjust context/model size only after checking your Mac's memory, response time, and actual JSON/action accuracy; a smaller model may reduce task reliability.

**Upgrading an existing configuration from the previous release:** after installing this version, run:

```sh
ghostdev use-local-model
```

This changes only inference settings to the local defaults and preserves registered projects, state-directory location, app permissions, and phone origins. It does not download weights or delete tasks. Reusing an old loopback forwarding port will not move inference onto the Mac; use the new local server directly.

You may select a different downloaded local model with `ghostdev use-local-model --model YOUR_MODEL --publisher ITS_PUBLISHER`. No publisher is required. Text-only models receive bounded Accessibility data; they cannot read screenshots. A compatible VLM can be enabled explicitly with `model_vision: true` after testing its local image API and grounding. The phone still receives actual approved-window screenshots in text mode.

An alternative local OpenAI-compatible server can run **on this Mac** with `model_backend: "openai-compatible"`, a literal loopback `/v1` endpoint and its real model ID. It must support structured JSON responses. Non-loopback endpoints, remote/cloud Ollama models, redirects, and cloud fallbacks are disabled. There is no automatic hardware offload.

Run `ghostdev doctor` to check server/model metadata and native permissions. Run a real task and `ollama ps` on the Mac to see local processing; an offline rehearsal is still required before claiming verified local execution. A loopback URL alone cannot prove that someone has not manually configured a forwarding proxy.

## Phone access and pairing

For the first test, use a browser **on your Mac** at `http://localhost:8765`, set `secure_cookies: false`, and retain only the default loopback origins. Insecure-cookie configurations with non-loopback origins are rejected.

For your phone on the **same Wi-Fi**, use HTTPS hosted by Caddy on the Mac. Edit `infra/Caddyfile.lan.example` to this Mac's actual LAN IP, put the same origin (for example `https://192.168.1.10:8443`) in config.json, and keep `secure_cookies: true`. Start the local proxy:

```sh
caddy run --config infra/Caddyfile.lan.example --adapter caddyfile
```

Caddy's `tls internal` uses a local certificate authority. Install/trust **your own generated Caddy root certificate** on your phone using its OS certificate settings before pairing. Caddy reports its local storage/CA location. Do not share the CA private key. This LAN route requires no remote server or hosted inference, but network/certificate setup needs checking on your actual phone and Mac.

Optional access from outside your Wi-Fi can use Tailscale software on just the Mac and phone, with `tailscale serve --bg http://127.0.0.1:8765` and its exact HTTPS origin in config.json. This is optional remote connectivity, not an inference dependency. It depends on internet/VPN connectivity; the basic same-Wi-Fi/local-AI workflow does not need it. Do not expose a public command server.

Enable **Accessibility** and **Screen Recording** in macOS System Settings → Privacy & Security for the Terminal/Python host that starts the worker. Restart that terminal/worker after enabling them. macOS may also ask for local Automation approval when an app is focused. If MDM prevents grants, use an authorized personal/teammate Mac; do not circumvent device management.

```sh
ghostdev doctor
./scripts/start.sh
```

In another activated Terminal in this repository:

```sh
ghostdev pair
```

Open GhostDev's HTTPS address on your phone. Enter the one-time code, compare the request ID displayed on the phone with `ghostdev pending-pairs`, then locally confirm your own matching request:

```sh
ghostdev confirm PAIRING_ID_FROM_YOUR_PHONE
```

The phone completes the exchange automatically. The code expires in five minutes and is single use. Authentication uses revocable HttpOnly cookies; state-changing requests require Origin and CSRF checks. WebSockets and artifact downloads require the same owner session.

## First desktop task

On the phone, select **Cart demo → Desktop task → Approve each GUI action**. Set exact expected note text, for example `Local AI keeps my work on my own machines`, and request:

> Open GhostDev Report.txt in the editor. Click its text field, add the exact expected note, and save. Start the local report preview, open it in the browser, and scroll to the summary. Finish only when the saved note is visible there.

The agent proposes JSON actions, validates them, executes one action, and observes again. It can launch/focus TextEdit or an isolated Chrome profile; click/double-click; type Unicode in the prepared editor; scroll; and use a small shortcut allowlist. Typing into browser address fields, Terminal, password fields, or unapproved windows is disabled. Screenshots cover the approved application window, never the full personal desktop. TextEdit should use plain text for the `.txt` fixture; disable smart substitutions during the demo if they alter the note.

Completion requires actual GUI click/type/scroll evidence across editor and browser, a real final browser capture, the expected text in the saved task document, and matching browser Accessibility text. A model saying “done” cannot satisfy these checks. Desktop edits remain in the task copy. The original project is not modified by this workflow.

Native host GUI controls are **not a security sandbox**. Window/title/focus allowlists are weaker boundaries than a dedicated VM. Use only the prepared session. See `docs/PERMISSIONS.md`.

## Coding task

Install/start Docker Desktop on the Mac if permitted, then prebuild the runner image before the offline demo:

```sh
docker build -t ghostdev-runner:local -f infra/Dockerfile.runner .
```

Choose **Fix & verify** and ask:

> Open the cart project in the editor. Fix calculateTotal so quantity 3 at $10 gives $30. Run the registered cart check. Perform a meaningful editor click and text action, then start and open the local cart preview in the browser. Click the quantity controls, scroll to the summary, and return the screenshot and proposed diff.

The worker records a protected failing baseline, lets the model inspect/edit permitted source, and requires the unchanged protected three-case harness to pass against the current patch. The harness is controller-owned, mounted read-only outside the task project. Code executes as a non-root user in a container with no network, read-only mounts/root, bounded memory/CPU/processes/time, no host Docker socket, and a small temporary filesystem. **No Docker means no code checks; there is no host-shell fallback.** The local browser preview uses a constrained loopback server and CSP; native browser/GUI interaction still has the limitations documented above.

Results show the real diff and its hash, checks/output, GUI events, and final screenshot. **Apply reviewed changes** refuses altered original contents, a dirty Git working tree, a changed base revision, or a different patch hash. Changes are staged from a sanitized project copy, not applied during inference. Apply is a local multi-file operation with a cooperative lock, not a filesystem transaction; an unrelated external editor can still race it. Do not edit the original while applying.

The sample's intentional bug is in `demo/cart-app/cart.js`. An actual code repair is produced by your local model, never hardcoded in the agent. The initial fixture is included unchanged. To repeat a demo after applying, restore only this sample file manually to its included original; no destructive Git-reset command is provided.

## Stop and takeover

Phone: Pause / Resume / Take control / Stop task. Resume discards pending proposals and observes again. In another activated Mac terminal:

```sh
ghostdev takeover
ghostdev revoke
```

Takeover revokes the desktop lease. Revocation is checked before dispatch and during bounded typing/waits/checks. Already dispatched clicks and saved changes cannot be undone. Worker restart marks active jobs INTERRUPTED and never replays input; start a new task after inspecting any partial effects. A disconnected phone does not stop a task, and reconnect uses stored event sequence numbers.

## Tests

```sh
python -m pip install -e '.[test]'
pytest
cd apps/mobile
npm run typecheck
npm run build
```

See `docs/VALIDATION.md` and `docs/DEMO.md`. Model and desktop test fixtures exist only under `tests/`; they cannot be selected in the shipped CLI. Neither those fixtures nor a passing build proves local GPU inference or native Mac input.

## Scope

One owner, one Mac, one active task, one display, TextEdit + Google Chrome, existing bounded text/source files. Mobile browser is the supported frontend target; native iOS/Android packaging is not shipped. Voice, Agent S integration, a desktop VM boundary, general file browsing, arbitrary shell/terminal input, external submissions, auto-deployment, and unattended recovery are not implemented. Windows/Linux desktop control is not claimed.

Repository: `ghostdev/` backend/executor/worker, `apps/mobile/` Expo source and exported web build, `demo/` prepared target, `verification/` protected harness, `infra/` restricted runner, `tests/` automated checks, `docs/` setup/limits/evidence.
