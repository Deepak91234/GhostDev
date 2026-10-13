# Validation record

Updated: October 13, 2026 UTC. Environment: Linux x86_64, Python 3.12.14, Node 24.19.0. The selected product deployment remains MacBook executor/controller + model inference on that same Mac + phone browser.

| Check | Actual result |
| --- | --- |
| Python unit/integration suite | 54 passing tests; one Starlette TestClient deprecation warning under the pinned dependencies |
| Expo/React Native Web TypeScript | `npm run typecheck` passed |
| Static mobile web export | `npm run build` passed; compiled UI included in archive |
| Browser workflow against actual FastAPI server | Baseline October 11: pairing, local confirmation, cookie session, project loading, task submit, Stop, reload, settings and WebSocket passed. October 13 rerun blocked by the Linux QA Chromium process crashing before page load; updated UI passed TypeScript and export checks |
| Responsive layouts | Baseline October 11: Chromium at 390×844 and 1440×1050 passed without page overflow/errors. This revision changes model labels; fresh browser rendering was not verified because the QA browser could not launch |
| Bundled protected cart harness | Original author-supplied fixture: exit 1, 1/3 cases passing. Curated repaired test fixture: exit 0, 3/3 cases passing. Ran as trusted unit-test fixtures, not as arbitrary agent code |
| Local-only HTTP adapter | Ollama/OpenAI-compatible schema/path handling, cloud/redirect denial, vision-capability rejection tested using HTTP transport fixtures |
| Desktop workflow orchestration | Isolated project, proposal/action/observation records, actual saved fixture verification, and final artifact tested with a **test-only fake Mac adapter and deterministic model fixture** |
| Native Mac capture/click/type/scroll | **Not verified here: no Mac desktop or OS permissions available** |
| Physical Mac model inference, CPU/GPU processing, grounding, latency | **Not verified here: this workspace is not the user's Mac and has no locally downloaded runtime model** |
| Docker runner execution/cancellation on Mac | **Not verified here: Docker daemon unavailable**. Refusal of host-shell fallback is tested |
| Mac-hosted LAN HTTPS / physical iPhone browser | **Not verified here: requires the actual Mac/phone and certificate trust** |
| Internet-egress-blocked hardware rehearsal | **Not performed** |
| Voice | Not implemented; disabled in UI |

The automated suite covers single-use pairing/local confirmation, required authentication/CSRF/Origin, session revocation, rejected WebSockets, literal loopback model endpoints and rejection of external inference machines, path traversal/secret/symlink denial, strict action parsing, stale observations/wrong focus/invalid coordinates, Retina/resized-window coordinate transforms, denied password/browser typing, task idempotency, ordered events, exact approval digest/generation binding, takeover, pause during pending inference and preflight, cancellation before desktop dispatch, task deadline, worker restart without replay, clean-original requirements, diff hash/content conflict rejection, exact apply, loopback preview/CSP boundaries, artifact authorization, and the independent cart harness.

Browser QA caught and fixed a phone layout overlap before final testing. Final mobile layout prioritizes the screen during tasks and keeps a Stop button in the top bar. QA generated only local test screenshots; none are presented as screenshots of the user's Mac.

`ghostdev doctor` in this environment accurately reports native desktop unavailable, model endpoint unreachable, and Docker runner unavailable; it does not manufacture readiness. The runtime has no selectable fake/model-demo mode. Tests live under `tests/` and are explicitly separated from physical evidence.

Dependency audit from October 11: the frontend tree reported transitive build-tool advisories rooted in braces and node-forge, whose published registry versions were not outside their reported advisory ranges. uuid and decode-uri-component are pinned to patched overrides. See PERMISSIONS.md. This is a hackathon MVP and is not certified for unattended use of personal desktops.

Before calling the project a successful local desktop demo, complete docs/DEMO.md on the actual MacBook, record the real model/runtime/evidence, and rehearse with internet egress blocked and LAN connectivity retained.

The Mac-only revision also tests the non-NVIDIA default model, local port 11434, thinking/context payload settings, and migration from a previous remote endpoint while preserving project/phone settings. The model server must be local; no separate inference machine is required.
