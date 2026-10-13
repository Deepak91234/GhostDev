# Implementation references

Updated October 13, 2026 (Mac-only deployment). These describe APIs and setup patterns; they are not evidence of this repository working on physical hardware.

- Local Ollama chat API: https://docs.ollama.com/api/chat
- Ollama structured output schemas: https://docs.ollama.com/capabilities/structured-outputs
- Ollama image requests: https://docs.ollama.com/capabilities/vision
- Expo web exports: https://docs.expo.dev/workflow/web/
- FastAPI WebSocket/session patterns: https://fastapi.tiangolo.com/advanced/websockets/
- Apple screen-capture preflight: https://developer.apple.com/documentation/coregraphics/cgpreflightscreencaptureaccess()
- Private HTTPS/Tailscale Serve: https://tailscale.com/docs/features/tailscale-serve
- Agent S local model integration evaluated for architecture: https://github.com/simular-ai/Agent-S/blob/main/models.md

Implementation uses a custom strict JSON action loop with native Accessibility observations, avoiding execution of Agent S's generated Python action code. Agent S is not installed/integrated. The default is locally downloaded qwen3:4b on the Mac. Other local models remain configurable; compatibility and desktop reliability must be tested on the actual Mac.

- Ollama macOS requirements: https://docs.ollama.com/macos
- Default local Qwen model: https://ollama.com/library/qwen3:4b
- Ollama local-only server configuration: https://docs.ollama.com/faq
- Thinking controls: https://docs.ollama.com/capabilities/thinking
- Mac-hosted LAN TLS: https://caddyserver.com/docs/caddyfile/directives/tls
