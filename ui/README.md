# Chatbot UI

A dependency-free, no-build front-end for the Agentic RAG Chatbot API: one
embeddable widget script.

| File | What it is | Use for |
| --- | --- | --- |
| `script.js` | **Embeddable widget** — one `<script>` tag injects a floating launcher + chat panel, fully CSS-isolated in a Shadow DOM. | Production / **teriin.org (Drupal)** |
| `index.html` | Mock host page that loads `script.js` over a screenshot backdrop. | Local preview |

## Embeddable widget (recommended for teriin.org)

Drop a single tag onto any page — that's the whole install:

```html
<script
  src="https://chatbot.teriin.org/ui/script.js"
  data-api-base="https://chatbot.teriin.org"
  data-title="TERI AI SARTHI"></script>
```

| `data-*` attribute | Meaning | Default |
| --- | --- | --- |
| `data-api-base` | Backend origin the widget calls. On an `https` host page an `http://` base is auto-upgraded to `https://` (localhost/loopback exempt) so requests aren't blocked as mixed content. | `http://localhost:8000` |
| `data-title` | Header / launcher label | `TERI AI SARTHI` |
| `data-top-k` | Optional override for chunks retrieved per question | server default |

The widget self-injects a launcher button (bottom-right), a welcome screen with
TERI-relevant suggestion prompts, a **New chat** button (which also cancels any
in-flight answer), streamed answers, and inline source chips. All markup and styles live
inside a Shadow DOM, so the host site's CSS can't leak in and the widget's
styles can't leak out. On phones (≤480px) it expands to full screen.

### TERI branding

Theme colors are CSS variables at the top of the `STYLES()` block in
`script.js` (`--teri-green`, `--teri-green-dark`, etc.). Adjust them to the
confirmed brand hex — the current values are a TERI-green approximation.

### Drupal integration

The widget needs no Drupal module — it's just a static script tag. Pick one:

1. **Block (no code):** create a *Custom Block* of type *Full HTML* (or a Block
   Layout custom block), paste the `<script>` tag, and place it in the *Footer*
   region so it loads site-wide.
2. **Theme template:** add the tag before `</body>` in your theme's
   `html.html.twig` (e.g. `themes/custom/<theme>/templates/html.html.twig`).
3. **Library (cleanest):** copy `script.js` into your theme/module, declare it
   in `<name>.libraries.yml`, and attach the library globally via
   `<theme>.info.yml` (`libraries: - <name>/chatbot`) or
   `hook_page_attachments()`.

Serve `script.js` from an origin the browser can reach and make sure the API's
`CORS_ALLOW_ORIGINS` includes `https://teriin.org` (see [CORS](#cors)).

## Local preview

1. Start the API:

   ```
   uvicorn app.main:app --reload
   ```

2. Serve this folder and open the mock host page:

   ```
   python -m http.server 5500 --directory ui
   ```

   Visit `http://localhost:5500` — `index.html` is a fake article page with the
   widget launcher in the bottom-right corner.

## CORS

A browser blocks cross-origin requests by default, so the API enables CORS via
the `cors_allow_origins` setting (comma-separated origins, default `*`; the
server logs a startup warning while the wildcard is active). When the widget
runs on teriin.org but the API is on another origin, that host must be allowed.
Set it in `.env`, e.g.:

```
CORS_ALLOW_ORIGINS=https://teriin.org,https://www.teriin.org,http://localhost:5500
```

## Endpoints used

| Endpoint | Purpose |
| --- | --- |
| `POST /chat` | Streamed answer (SSE: `token` / `sources` / `done`; a terminal `error` event when generation fails mid-stream) |

Sources render inline: each `[n]` marker in the answer becomes a chip naming the
site it links to (`teriin`); hovering or focusing it opens a card with the
source's site, title and page. Citation
links are absolute and point at the source site: a web page cites its own URL, a
PDF cites the attachment URL it was downloaded from (plus `#page=N`). A citation
with no resolvable URL — the knowledge graph, a catalog lookup — gets no chip at
all, and a refusal shows no sources.

## Files

- `script.js` — **embeddable widget**: self-contained launcher + Shadow-DOM panel, streaming, inline source chips (the Drupal drop-in)
- `index.html` — mock host page for local preview
- `Screenshot 2026-07-01 155054.png` — backdrop image used by `index.html`
