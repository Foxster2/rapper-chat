# AI Chat Wrapper — Project Brief

## Goal
A quick-and-dirty v1 of an AI chatbot wrapper (like the many "AI wrapper" products people have built fortunes on). Priority is **simple, working, iterable** — not polished or production-hardened. We will give feedback and improve incrementally after this first pass.

## Stack decisions

| Concern | Decision | Notes |
|---|---|---|
| Backend framework | **Django** (Python) | Templates + views, no separate frontend framework/build step |
| Auth | **Clerk** (https://clerk.com/) | Hosted sign-in/sign-up pages. No official Django SDK — verify Clerk's session JWT manually via their JWKS endpoint in a small Django middleware; attach the Clerk user ID to `request` |
| LLM orchestration | **LangChain** (Python) — `langchain` + `langchain-openai` | Use `ChatOpenAI` class pointed at OpenRouter's base URL |
| LLM provider | **OpenRouter**, free models only (`:free` suffix) | OpenAI-compatible API — base URL `https://openrouter.ai/api/v1` |
| Default model | `meta-llama/llama-3.3-70b-instruct:free` | Chosen as the more stable general-purpose chat pick vs. OpenRouter's coding-specialized free models. **Free-tier model IDs rotate over time** — treat this as an env variable, not a hardcoded value, so it's a one-line swap later |
| Database | **SQLite** to start | Django ORM abstracts this — trivial to swap to Postgres later without code changes |
| Streaming | **None for v1** — plain request/response, show a loading state while waiting | SSE/streaming is an explicit non-goal for this pass |
| UI | Plain Django templates + vanilla JS/CSS | Loosely modeled on the Discourse AI Chatbot UI Kit (Figma, used as a wireframe reference only — not pixel-matched): https://www.figma.com/community/file/1569991105423787535/discourse-ai-chatbot-ui-kit-free |

## Data model (initial)

- `Conversation`
  - `owner` — Clerk user ID (string)
  - `title`
  - `created_at`, `updated_at`
- `Message`
  - `conversation` — FK to `Conversation`
  - `role` — `user` / `assistant`
  - `content`
  - `created_at`

## Request flow

1. Browser (chat UI, Django template) — user types a message and submits
2. Clerk middleware — verifies the signed-in user's session JWT before the request reaches the view
3. Django view (`/chat` or similar) — receives the message, saves it to the DB, loads conversation history
4. LangChain (`ChatOpenAI`) — formats the message history into an OpenRouter-compatible request
5. OpenRouter — routes to the free model, returns a completion
6. Response is saved to the DB and returned as JSON to the browser, which appends it to the chat thread

No SSE — the browser just waits for the JSON response and renders it.

## Explicit non-goals for v1
- ~~No streaming (SSE)~~ — **implemented in Day 2** (token-by-token streaming via SSE)
- No pixel-perfect UI (Figma kit is a wireframe reference only)
- No production-grade auth hardening beyond basic Clerk JWT verification
- No paid OpenRouter models
- No Postgres/deployment infra yet — SQLite and local dev is fine

## Working style
- Build iteratively — get a working v1 end to end (auth → chat → LLM response → persisted history) before polishing anything
- Keep the OpenRouter model ID and API key in environment variables, never hardcoded
- Favor the simplest implementation that works over "correct" architecture — this will be revised after feedback

## Day 2 updates
Layered onto the v1 above (see `day2-plan.md` for full detail):
- **SSE streaming** — replies stream token-by-token. `create_message` saves the user turn and returns a message pair whose assistant bubble opens an `EventSource` to `stream_reply`, which emits `token` events then a final `done` event with server-rendered markdown.
- **Auth** — switched to cookie-only via Clerk's `__session` cookie (so header-less `EventSource`/htmx requests authenticate too). The middleware is attempt-only and never hard-fails; `@require_clerk_auth` enforces.
- **Frontend** — rebuilt on **htmx + Alpine + htmx-sse**: server-rendered partials, no vanilla-JS fetch layer. Alpine owns shell state (welcome↔chat, theme); a small delegated-JS layer covers the actions popover, autosize, Enter-to-send, highlight and autoscroll.
- **UI** — reskinned with **daisyUI** (v4 full CSS + Tailwind Play CDN, no build step); the hand-rolled cream/puddle CSS was retired. Added animations (message entrance, thinking dots → streaming cursor, theme crossfade, press feedback), all gated by `prefers-reduced-motion`.
- Still dev/localhost on `runserver`; an ASGI move (uvicorn) is deferred to a production pass.
