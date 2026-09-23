from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2024-06-01"
    azure_openai_model: str = ""
    llm_structured_temperature: float | None = None
    azure_openai_embedding_model: str = ""
    azure_openai_embedding_key: str = ""
    azure_openai_embedding_endpoint: str = ""
    azure_openai_embedding_api_version: str = "2024-06-01"
    # Output vector size for the embedding model. text-embedding-3-{small,large}
    # support Matryoshka truncation to a smaller dimension; 1536 halves storage
    # and search cost vs 3-large's native 3072 with negligible retrieval loss.
    # Set to None (leave blank) for ada-002, which does not accept this param.
    azure_openai_embedding_dimensions: int | None = 3072
    # Retries the OpenAI SDK spends on one embedding call before it raises. It
    # retries 429 with exponential backoff, honouring Azure's `retry-after`, so
    # this is the whole defence against provisioned-throughput throttling — the
    # library's own `retry_min_seconds`/`retry_max_seconds` are declared but
    # never read. The SDK default of 2 is short of the "retry after 3 seconds"
    # Azure asks for under sustained load; 8 rides out a throttling window
    # instead of losing the document to `documents_retry`.
    azure_openai_embedding_max_retries: int = 8
    # Ceiling on one throttle pause, and the fallback when a 429 arrives without
    # a usable `retry-after`. Caps the damage a wrong or hostile header can do:
    # every embedding thread waits on this, so an unclamped value would stall
    # the whole run rather than one request.
    azure_openai_embedding_max_throttle_seconds: float = 60.0
    azure_document_intelligence_endpoint: str = ""
    azure_document_intelligence_key: str = ""
    # "prebuilt-read" is the OCR-only (basic) model: cheap, text only, no table
    # structure. "prebuilt-layout" costs ~6x more but also reconstructs tables
    # and document structure (and supports Markdown output).
    azure_document_intelligence_model: str = "prebuilt-read"
    pdf_scanned_char_threshold: int = 100
    # PDF extraction routing (app/ingestion/extractors): "hybrid" classifies each
    # page and routes per page — scanned/image pages to Azure OCR, born-digital
    # table pages to Camelot, the rest to PyMuPDF text; "azure_only" always uses
    # Azure OCR; "local_only" uses PyMuPDF text only.
    extraction_mode: str = "hybrid"
    # Camelot table extraction (born-digital table pages). "lattice" reads ruled
    # tables and needs Ghostscript; the extractor falls back to "stream" when
    # lattice finds nothing on a page.
    camelot_flavor: str = "lattice"
    # Per-page table detection (PyMuPDF). find_tables() is the primary, reliable
    # signal — it handles both ruled and borderless tables. The two extra
    # heuristics below are OFF by default: on heavily-designed PDFs (banners,
    # side panels, page borders, multi-column text) they fire on nearly every
    # page and over-route everything to Azure. Enable them only for simpler
    # corpora where biasing harder toward Azure is worth the false positives.
    pdf_detect_ruled_grid: bool = False
    pdf_table_min_grid_lines: int = 3
    pdf_detect_borderless_tables: bool = False
    pdf_borderless_min_aligned_rows: int = 4
    pdf_borderless_min_columns: int = 3
    # A text line repeated on >= this fraction of a document's pages is treated as
    # a running header/footer and stripped from every page (0 disables).
    pdf_running_header_min_fraction: float = 0.5
    # Drop chart/axis "number soup" lines (e.g. "2020 2030 2040 2050") — bare
    # numeric runs from figures that carry no semantic signal.
    pdf_drop_number_soup: bool = True
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "documents"
    redis_url: str = ""
    semantic_cache_enabled: bool = True
    # 0.995: near-verbatim rephrasings only. At the old 0.97 a subtly different
    # question (another year, another theme) could return the wrong cached
    # answer; correctness beats hit rate here. Lookups additionally require the
    # stored facet fingerprint to match.
    semantic_cache_threshold: float = 0.995
    # Semantic cache is backed by a dedicated Qdrant collection: a nearest-neighbor
    # lookup on the query embedding, gated by semantic_cache_threshold (cosine).
    semantic_cache_collection: str = "semantic_cache"
    # Cached-answer lifetime in seconds (stored as expires_at; Qdrant has no TTL).
    semantic_cache_ttl: int = 86400
    # Qdrant has no native TTL, so each entry stores an expires_at that lookups
    # filter out once stale; expired points are deleted every N stores (0 disables
    # the opportunistic prune — rely on lookup-time filtering / a scheduled prune).
    semantic_cache_prune_every: int = 200
    worker_sweep_interval_seconds: int = 3600
    worker_sweep_reconcile: bool = False
    # Cross-store reconciliation (app.ingestion.reconcile) after each sweep:
    # MySQL against Qdrant against the graph, logged and kept for /metrics. It
    # scrolls the whole collection, so it costs one pass over the points per
    # sweep — the price of not discovering silent drift months later. It only
    # ever reads, and never fails a sweep.
    verify_corpus_after_sweep: bool = True
    retrieval_top_k: int = 6
    retrieval_candidate_k: int = 40
    # Website-content preference (see docs/retrieval/04-search-and-fusion.md).
    # A *recall* guarantee, and only that: retrieval runs two pulls — website
    # (source_type == "website") and "not website" — and unions them, so the
    # website's best chunks are fetched even though PDFs dominate the corpus by
    # volume and would otherwise fill a single pull. What happens next is the
    # same for every candidate: one ranking, one admission pass, one answer.
    #
    # It used to mean more than that. The context builder admitted website
    # blocks first under their own cap and relevance floor, gave PDFs two
    # remaining slots plus a conditional third, and emitted them in that order
    # whatever the ranking said; the generation prompt then told the model that
    # "website sources are authoritative" and split the answer in two. Asked who
    # the director general is, that chain led with a three-year-old announcement
    # and filed the document correcting it in a captioned aside underneath.
    # Source kind is now one term of the authority band in the reranker — a
    # tie-break between passages already judged comparably relevant, comparably
    # current and comparably well-matched to the period — and nothing more.
    prefer_website_enabled: bool = True
    # Website-only candidates pulled alongside the (larger) not-website pull.
    website_candidate_k: int = 20
    hybrid_use_sparse: bool = False
    # Multi-query recall expansion: LLM paraphrases of the search query are
    # searched in parallel and RRF-fused with the base pull. Gated per query
    # (qa intent, no explicit filters, non-trivial length). Launches OFF; flip
    # after eval.
    multi_query_enabled: bool = False
    # How many extra perspectives to ask for. Named `paraphrases` because that is
    # what the generator produced before Phase D and the name is already in
    # deployed .env files; it now counts genuinely different angles on the query
    # (see `strategies.perspectives`), not rewordings of it.
    multi_query_paraphrases: int = 2
    # Cosine above which a generated perspective is "the question again" — too
    # close to the original, or to an angle already accepted, to earn its own
    # retrieval leg. Rejecting everything is a valid outcome: the base query is
    # always a leg, so the fallback is the retrieval this query would have had.
    # Same value as `dedup_cosine_threshold` below, which draws the equivalent
    # line between two chunks, for the same reason.
    multi_query_distinct_threshold: float = 0.92
    # Self-consistency routing: number of concurrent query-analysis samples,
    # majority-voted per field. 1 = single pinned-temperature call (today's
    # behavior); >1 samples at exploratory temperature. Flip to 3 only after
    # the routing eval shows a win.
    analysis_votes: int = 1
    # Minimum per-label confidence for a multi-label intent to be kept: the
    # agreement share across samples when analysis_votes > 1, else the model's
    # self-reported score. Terminal intents (chitchat/out_of_scope/…) are gated
    # by the same bar.
    intent_confidence_threshold: float = 0.5
    # Act on the `clarification_needed` intent instead of letting it collapse
    # onto chitchat: ask the user one question back, with catalog-derived options
    # where they exist, and merge their reply into the next turn's query. State
    # rides on the client-echoed history, so no session store is involved. OFF
    # reproduces today's behaviour exactly — the label is still detected and
    # still discarded. Launches OFF; flip after eval.
    clarification_enabled: bool = False
    # The bar the `clarification_needed` label must clear to be *acted on*,
    # separate from `intent_confidence_threshold` because the cost is not
    # symmetric: a wrong content label still retrieves and can still answer,
    # whereas a wrong clarification spends the user's whole turn asking about a
    # question that had an answer. Observed at 0.74 on "director generak of
    # teri" — the model's own rationale was "vague/typo", and TERI's Director
    # General was in the 2022-23 annual report. The label stays on the trace
    # below the bar, so the false-positive rate is still measurable.
    clarification_min_confidence: float = 0.8
    # Act on the temporal intent `temporal_gate.detect_mode` already classifies:
    # rank by how well a document's period fits the time the question is about
    # (a band cut INSIDE the relevance band, so relevance still decides), and
    # gate past-tense questions about scheduled occurrences the way upcoming ones
    # already are. Reads only payload fields that exist today; adds no date
    # filtering — that stays `filters.date_conditions`. OFF leaves ranking and
    # the pre-existing UPCOMING gate exactly as they are. Launches OFF; flip
    # after eval.
    temporal_intent_enabled: bool = False
    # Sub-query planning: turn the requirements `answer_plan.extract_requirements`
    # already extracts into separately retrieved parts — one dense pull each,
    # fused by the existing RRF, plus a graph attempt for any part that names a
    # relationship. Only fires when the extractor found two or more parts, so an
    # ordinary question keeps the single-query path. Adds no decomposition of its
    # own and no extra LLM call, but it does put the existing extraction on the
    # critical path (retrieval has to wait for the plan), which is the cost to
    # weigh. OFF reproduces today's behaviour exactly. Launches OFF; flip after
    # eval.
    subquery_planning_enabled: bool = False
    # How many parts of a multi-part question are retrieved separately. Each one
    # is an embedding plus a Qdrant pull; past a handful the extra rankings are
    # noise against real latency.
    subquery_max: int = 3
    # One-shot corrective retrieval: when the reranked top candidate's raw
    # semantic score is below corrective_min_score, reformulate the query once,
    # search again, RRF-fuse and rerank. Strictly one iteration. Launches OFF;
    # eval must show a recall win within the latency budget before flipping.
    corrective_loop_enabled: bool = False
    corrective_min_score: float = 0.2
    # Keyword leg over the chunk_text full-text index (created by
    # scripts/create_fulltext_index.py): salient query terms drive one extra
    # MatchText-filtered pull fused with the dense pull via RRF. Fails open to
    # dense-only while the index is absent. Launches OFF. (hybrid_use_sparse
    # stays reserved for true sparse vectors, which need ingest-time writes.)
    keyword_leg_enabled: bool = False
    # Database Planner v2: use an LLM to decompose a catalog (database-intent)
    # question into one or more tool calls (comparisons like "2023 vs 2024", a
    # count paired with a list). OFF uses the deterministic single-call v1 plan;
    # any planner failure falls back to v1 as well. Launches OFF; flip after eval.
    database_multi_call_enabled: bool = False
    # Fuzzy entity resolution (app.retrieval.structured.resolve): scores a
    # free-text name against known authors/bundles/themes. OFF means an
    # unresolved theme/tag filter falls through to semantic search exactly as
    # before (today's behavior); ON makes it a terminal, explicit answer
    # ("no theme matching 'X' found") instead, and lets the v2 planner
    # advertise resolve_entity as a callable tool. This is the one switch for
    # the whole feature's change in fall-through behavior. Launches OFF; flip
    # after eval.
    entity_resolution_enabled: bool = False
    # Constrain a structured list by the part of the question the catalog's
    # facets cannot express, instead of answering from the bucket the facets
    # picked. OFF reproduces the previous behaviour exactly — a topic is snapped
    # onto the nearest theme, an unexpressed topic is dropped, a person question
    # is answered with documents, and a truncated list does not say what it
    # truncated. Kept as a switch so the two can be A/B'd on one build; see
    # `app.retrieval.structured.topic`.
    structured_topic_constraint_enabled: bool = True
    reranker_provider: str = "embedding"
    rerank_model: str = ""
    rerank_score_threshold: float = 0.0
    # Candidates the cross_encoder provider will score. It runs one model pass
    # per candidate, so its cost is linear where every other provider's is flat:
    # measured on CPU at ~55ms per candidate, the full fused set (three legs at
    # `retrieval_candidate_k` is routinely 100+) came to 6.6s per query. The head
    # of the incoming order is kept, which is the fused ranking, and the tail is
    # left in place behind it — a candidate the first stage ranked below 40th has
    # never reached the final `retrieval_top_k` (6), so this bounds the latency
    # without changing what gets answered. 0 disables the cap.
    #
    # `_MAX_LLM_CANDIDATES` is the same guard for the llm provider, which instead
    # declines entirely past its limit; that would mean never reranking at all
    # here, since production always exceeds it.
    rerank_max_candidates: int = 40
    # Tokens per query+passage pair the cross-encoder sees. 0 leaves the model
    # default alone (512 for both rerankers wired here).
    #
    # Latency is close to linear in this. Measured at a 10-candidate pool,
    # 512/256/128/64 cost 1370/574/323/181ms — so 64 is 7.6x cheaper than the
    # model default.
    #
    # Ranking quality across those four is NOT measurable on the current
    # benchmark, and the rising MRR column in the phase-3 report must not be read
    # as a trend. `scripts.judge_retrieval` grades each chunk on `text[:700]`,
    # and a census of the 491 chunks in the gold set puts 88.8% of them longer
    # than that (median 1637 chars; the grader saw 42.8% of a median graded
    # chunk). So a reranker reading ~256 chars (64 tokens) sits inside the
    # grader's field of view while one reading ~2048 (512 tokens) is judged
    # partly on text the grader never saw. Win counts are flat — 27/29/28/27
    # across an 8x range — which is what you would expect if the effect were
    # alignment with the gold set rather than relevance.
    #
    # So choose this on latency, and on how much of a chunk you want actually
    # judged. Against the 1637-char median of the graded set, 128 tokens keeps
    # roughly a third of a chunk in view and 64 keeps about a sixth — closer to
    # topic matching than to passage judgement.
    rerank_max_seq_length: int = 0
    # How far apart two candidates' relevance scores may sit and still count as
    # "similarly relevant" — the width of a ranking band. Inside a band the newer
    # document leads; across bands relevance always wins, however old the winner
    # is. Sized for the 0..1 scale every provider returns — cross_encoder emits an
    # unbounded logit but `reranker._cross_encoder_semantic` squashes it onto the
    # same footing, so this needs no adjustment per provider. Widen to let recency
    # decide more often, narrow to make it decide less; measured on the retrieval
    # benchmark, 0.20 cost 0.038 MRR against this default.
    rerank_relevance_tolerance: float = 0.03
    # Multiplier on that tolerance when the query is about something that goes
    # out of date — pricing, an API, a regulation, an announcement (see
    # app.retrieval.search.volatility). A wider band lets the recency tie-break fire
    # more often; it never lets recency cross a band, so relevance still decides.
    # Set to 1.0 to rank volatile and stable topics identically.
    rerank_volatile_tolerance_multiplier: float = 2.0
    # How much more text one passage must hold than another before it counts as
    # "substantially more complete" and leads it — the completeness tier, which
    # sits below relevance and above recency. Length is a proxy: accuracy is not
    # measurable at ranking time, but a chunk cut short carries less of an answer
    # than a full one. Raise it to make completeness matter less; a very large
    # value hands every relevance tie to recency.
    rerank_substance_ratio: float = 1.5
    # Additive boost to a candidate's blended score when it contains a table and
    # the user asked for a table-shaped answer. Soft (not a filter) so a table
    # request still returns non-table results when no table matches.
    rerank_table_boost: float = 0.15
    dedup_cosine_threshold: float = 0.92
    # Max tokens of retrieved context sent to the LLM. Blocks are parent chunks
    # (~1800 tokens each), so this gates roughly context_token_budget / 1800
    # passages; 9000 keeps ~5 diverse sources. Sized when that was 2 website
    # blocks plus ~3 of PDF depth; the split is gone but the budget it implied
    # is the right one, and the blocks are now whichever 5 rank highest.
    # Prefill cost/latency rises only on content-rich queries.
    context_token_budget: int = 9000
    faithfulness_check: bool = False
    # --- Web retrieval (app/retrieval/web) -----------------------------------
    # The master switch and kill switch. With this false the web package is
    # never imported on the request path and every answer comes from the
    # internal corpus exactly as before. With it true, the web is consulted only
    # when a question asks for it (an explicit "search the web", a freshness
    # request) or when the internal evidence is judged insufficient — never on
    # every query.
    web_search_enabled: bool = False
    # Which search API answers web queries: "brave" or "tavily". Empty means no
    # provider, which makes web retrieval a no-op even with the switch on — a
    # deployment cannot reach the web by accident of one flag.
    web_search_provider: str = ""
    web_search_api_key: str = ""
    # Base URL override for the provider's API (a proxy or a regional endpoint).
    # Empty uses the provider's public endpoint.
    web_search_endpoint: str = ""
    # The organisation's own domains, comma-separated. Questions about the
    # organisation are searched here first (site-restricted), and a page on one
    # of these is treated as a primary source rather than third-party coverage.
    # Subdomains are included: "teriin.org" covers "www.teriin.org".
    web_primary_domains: str = "teriin.org"
    # Whether results from outside the primary domains may be used at all. On,
    # but ranked below primary sources and attributed to their site in the
    # answer; off restricts web retrieval to the organisation's own pages.
    web_allow_third_party: bool = True
    # Domains never searched or fetched, comma-separated, subdomains included.
    web_blocked_domains: str = ""
    # How requests identify themselves. A named agent with a contact URL is what
    # robots.txt rules and site operators key on; a browser disguise is not.
    web_user_agent: str = "TERI-Knowledge-Assistant/1.0 (+https://www.teriin.org)"
    # Per-request ceiling for fetching one page (connect + read). A slow site
    # costs this much at most; the answer is built from whatever arrived.
    web_fetch_timeout_seconds: float = 8.0
    # Connections the web client may hold open at once, across all hosts. Bounds
    # how hard one busy minute can hit the sites being read.
    web_max_connections: int = 8
    # Lifetimes of the three web caches, in seconds; 0 disables that cache.
    # Search results go stale fastest, so they are kept for hours; an extracted
    # page and a site's robots.txt change rarely and are kept for a day. None of
    # these is ever written to the corpus.
    web_search_cache_ttl: int = 21600
    web_page_cache_ttl: int = 86400
    web_robots_cache_ttl: int = 86400
    # Entries each in-process cache holds before evicting the least recently
    # used. Only applies without Redis; with `redis_url` set, Redis holds them
    # and expires them itself.
    web_cache_max_entries: int = 512
    metrics_log_enabled: bool = True
    # --- Retrieval logging (debugging / evaluation / analysis) ---------------
    # One switch for the whole per-query retrieval trace: what Qdrant, the graph
    # and MySQL were each asked, what they returned, how long they took, what
    # failed, and the context that finally reached the LLM. Written as one JSON
    # file per query under `retrieval_log_dir` (see
    # app/observability/retrieval_log and docs/retrieval-logging.md).
    #
    # Named `is_retrieval_log` because that is the environment key operators
    # were given; off by default, and with it off the instrumentation is a
    # single boolean read at each call site — nothing is serialized, no
    # directory is touched, no disk is written.
    is_retrieval_log: bool = False
    # Where the trace files go. Empty means <repo root>/logs, so the path does
    # not depend on the working directory the server happens to start in.
    retrieval_log_dir: str = ""
    # Whether retrieved passage text is stored in the trace. On, because
    # judging retrieval quality without the text means judging ids; the corpus
    # is public, so there is nothing here to leak. Off keeps the trace to ids,
    # scores and metadata (a "safe reference" to the content).
    retrieval_log_include_text: bool = True
    # How each retrieved item is written. "compact" (the default) is one line
    # per item — rank, score, where it came from, a snippet; "full" is the
    # structured object with every payload field. Compact because the scale is
    # not optional: one question runs five Qdrant legs of forty hits, and at
    # eight lines a hit that was a 700 KB, 10,000-line file for a single query.
    # Switch to "full" for programmatic analysis that needs every field.
    retrieval_log_detail: str = "compact"
    # Per-string ceiling for anything captured (passage text, SQL, an error
    # message). Longer values are truncated with a marker, so one pathological
    # document cannot produce a megabyte of JSON. Applies to the context blocks
    # and requests; a compact hit's snippet is shorter still (see
    # app/observability/retrieval_log/views.py).
    retrieval_log_max_text_chars: int = 1200
    # Per-event ceilings on how many retrieved items are captured: Qdrant hits /
    # graph rows / SQL rows. The counts recorded alongside are always the true
    # totals, so a truncated sample never misreports recall. Ten is what a
    # person reads; raise it when analysing a whole candidate set.
    retrieval_log_max_results: int = 10
    # Timezone for the log's directory layout: the date folder and the local
    # time in each query's folder name. Everything *inside* a trace stays UTC
    # ISO-8601. Needs the tzdata package (Windows ships no timezone database);
    # an unloadable name degrades to a fixed +05:30, which is correct for IST.
    retrieval_log_timezone: str = "Asia/Kolkata"
    # Write `report.md` beside each `trace.json`: the same trace explained in
    # prose — what the question was taken to mean, which retriever was asked
    # what, which passages reached the LLM, and what each stage is for. Rendered
    # from the same record, so the two cannot disagree. On, because the point of
    # a trace is that someone can read it.
    retrieval_log_report: bool = True
    # Append a one-line-per-query JSONL digest under logs/summary/, for
    # latency/failure analysis in pandas without opening every file.
    retrieval_log_summary: bool = True
    # Max chat generations driven concurrently on the dedicated chat capacity
    # limiter. The /chat pipeline is blocking (LLM + Qdrant + Redis clients),
    # so each active stream occupies a worker thread for most of its life;
    # giving chat its own limiter keeps those long generations from starving
    # the shared request threadpool (~40 threads) that auth dependencies,
    # probes and other sync offloads borrow from. Extra chats queue here.
    chat_stream_max_concurrency: int = 64
    # Expose infrastructure detail (collection name, point counts, tuning
    # values, raw error strings) on /ready and /metrics. Off by default: the
    # retrieval API is public-facing and those bodies fingerprint the
    # deployment — probes only need the status codes. Enable on private /
    # dev deployments for human debugging.
    ops_detail_enabled: bool = False
    # JWT group whose members may read /metrics and /metrics/timings even when
    # ops_detail_enabled is off (e.g. "admin"). Only honored when auth_enabled —
    # without verified tokens every caller is anonymous and a group grant would
    # be meaningless. Empty (default) disables the group grant entirely.
    ops_admin_group: str = ""
    cors_allow_origins: str = "*"
    # Authentication for the public retrieval API (/chat, /search). When enabled,
    # requests must carry a Bearer JWT that the backend verifies; user_groups
    # are taken from the token's claims, never from the request body. Groups do
    # not scope retrieval (the corpus is public) — they only widen access to the
    # ops endpoints. Off by default (anonymous caller = groups ["public"]).
    auth_enabled: bool = False
    # Key used to verify the JWT signature. For HS* algorithms this is the shared
    # secret; for RS*/ES* it is the PEM-encoded public key.
    jwt_secret: str = ""
    # Comma-separated allow-list of accepted signing algorithms. Anything outside
    # this list (including the unsigned "none" algorithm) is rejected.
    jwt_algorithms: str = "HS256"
    # Optional audience / issuer to enforce when set (empty = not checked).
    jwt_audience: str = ""
    jwt_issuer: str = ""
    # Claim name carrying the caller's authorization groups.
    jwt_groups_claim: str = "groups"
    # Authentication for the ingestion control plane (/ingest/*, /reindex),
    # verified with the same JWT machinery as the retrieval API. Deliberately a
    # separate switch from `auth_enabled`, and deliberately ON: these routes
    # crawl the corpus, inject documents into the answer set, queue rebuilds and
    # read back internal ids and error strings. A deployment that has not enabled
    # retrieval auth is precisely the one that would otherwise leave them open.
    # Turn this off only for an ingestion server on a private interface whose
    # operators accept that anyone who can reach it may drive it.
    ingest_auth_enabled: bool = True
    # JWT group required for the *mutating* ingestion routes (crawl, article
    # injection, reindex). Falls back to `ops_admin_group`; when neither is set
    # the group check cannot mean anything, so any authenticated caller may
    # proceed and the gap is logged. Reading the ingest log needs authentication
    # but no group.
    ingest_admin_group: str = ""
    otel_enabled: bool = False
    otel_service_name: str = "agentic-rag"
    otel_exporter_otlp_endpoint: str = ""
    # Neo4j backs the knowledge graph (canonical entities, aliases, claims,
    # provenance). It is a rebuildable projection of MySQL + Qdrant, never a
    # system of record, so an outage degrades the knowledge layer and loses
    # nothing. Community edition offers no role-based access control, so the
    # read-only boundary for retrieval is enforced in code (see
    # app/core/clients/graph.py) rather than by a restricted database user.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    # Community supports exactly one user database, named "neo4j".
    neo4j_database: str = "neo4j"
    neo4j_connection_timeout: float = 10.0
    # Master switch for the knowledge layer (entity/claim extraction, graph
    # projection). OFF: with this false nothing in the app opens a Neo4j
    # connection and ingestion/retrieval behave exactly as they do today.
    # Per-stage flags arrive with the stages they gate.
    knowledge_enabled: bool = False
    # Run the per-document knowledge stage after a document is successfully
    # indexed (app.ingestion.knowledge_sync). OFF, and additionally gated by
    # knowledge_enabled above, so the feature ships inert: with this false
    # `_handle` behaves exactly as it did before the stage existed.
    #
    # Deliberately its own switch rather than riding on knowledge_enabled. That
    # flag means "this deployment has a knowledge layer"; this one means "build
    # it incrementally on the ingest path", which is a separate decision with a
    # separate cost, and a deployment may reasonably want the corpus-level
    # scripts.build_knowledge and nothing else.
    knowledge_process_after_index: bool = False
    # Project the document's own claims into Neo4j at the end of its knowledge
    # stage. Gated by knowledge_process_after_index, so it is inert by default.
    # Off means MySQL still gets the claims and the graph catches up at the next
    # project_after_sweep — a lag, never a loss.
    knowledge_project_per_document: bool = True
    # Wall-clock budget for one document's knowledge stage. Exceeding it ends
    # the run as `partial` rather than raising: what already landed is valid and
    # a retry resumes.
    #
    # Sized from the 200-document canary so that a document using its full
    # per-document call allowance can still reach validate, persist, conflicts
    # and project instead of being cut short by the budget alone. Measured
    # there: per-call latency p50 2.24s / p95 4.36s / max 5.94s, and non-call
    # stage overhead p50 0.22s / p95 3.53s / max 16.11s. Eight calls at the
    # worst observed latency plus the worst observed overhead is 63.6s, so 30s
    # could not fit eight calls above roughly p75 latency — it discarded claims
    # a document had already paid for and marked it retryable, and the retry hit
    # the same wall.
    #
    # Now sized to the per-document call ceiling below, by the same method: 32
    # calls at the worst observed per-call latency (5.94s) plus the worst
    # observed overhead (16.11s) is 206s, and 300s is 1.45x that. A budget that
    # cannot fit the call allowance is not a safety limit; it is a guarantee of
    # truncation.
    #
    # Still a real bound, deliberately. It is what stops a genuinely stuck
    # document — not merely a slow one — from holding a worker indefinitely.
    knowledge_stage_budget_seconds: float = 300.0
    # Ceiling on model calls while extracting claims from ONE document, so a
    # pathologically long document cannot spend the whole run's budget. The
    # claim_llm_max_calls_per_run above stays the corpus-level ceiling.
    #
    # Raised from 8 on measured evidence. A document wants one call per chunk
    # holding a claim-eligible entity, and across the 503 canary documents that
    # distribution is long-tailed but thin: p50 3, p75 9, p90 15, p95 21,
    # p99 31, max 79, with 27% of documents wanting none at all. At 8, only
    # 74.3% of the documents that call anything were examined in full — 38% of
    # policy briefs were truncated — and a truncated document is NOT retried,
    # because a retry stops at the same ceiling. The shortfall was silent and
    # permanent, not deferred.
    #
    # The tail being thin is what makes the fix cheap: 8 -> 32 lifts complete
    # coverage from 74.3% to 99.2% for 3.00 -> 4.51 calls per document, about
    # 50% more calls. Past 32 the curve flattens (96 buys the last 0.8 points,
    # three documents, and would need a ~870s budget), so 32 is the knee rather
    # than a round number.
    knowledge_llm_max_calls_per_document: int = 32
    # Extract and resolve mentions during the per-document knowledge stage.
    #
    # OFF by default, preserving the behaviour this had when it was a hardcoded
    # `StageOptions.with_mentions = False`: mention and resolution rows are
    # audit material that nothing reads at query time, and it is the most
    # expensive deterministic stage.
    #
    # But it is NOT only audit material, and that is why it is a setting now.
    # LLM claim extraction may only reference entities this document's own
    # resolution marked canonical (app.knowledge.claims.extract_llm: the model
    # "cannot name an entity"), so with mentions off it is handed nothing and
    # makes zero calls. With `claim_extraction_enabled` on and this off, claim
    # extraction is silently inert -- measured across 11,238 completed
    # knowledge runs, every bundle except completed_projects (whose claims come
    # from the free deterministic CMS-field path) had staged exactly zero.
    #
    # Turn this on to make an ordinary ingest sweep also build the claim layer.
    # `scripts.knowledge_document --with-mentions` remains the per-document
    # override and does not depend on this.
    knowledge_extract_mentions: bool = False
    # How many times a document's knowledge stage may be retried by the
    # catch-up sweep before it is left alone. The durable-retry-as-state pattern
    # the enrichment and dead-link tables already use, not a job queue.
    knowledge_stage_max_attempts: int = 3
    # Refresh the graph projection at the end of each sweep, so it stops drifting
    # the moment nobody remembers to run scripts.project_graph. Gated by
    # knowledge_enabled, so it is inert on a deployment without a graph, and
    # fail-open in every direction: an unreachable Neo4j costs a log line and
    # never touches the ingestion that already succeeded.
    graph_project_after_sweep: bool = True
    # How old a projection may be before reconciliation calls it stale. Sized
    # well above the sweep interval so an ordinary missed run is not an alarm;
    # what it catches is projection having stopped happening at all.
    graph_projection_max_age_seconds: int = 86400
    # Graph-backed retrieval. Separate from knowledge_enabled because the graph
    # must be built and verified long before any query is allowed to read it.
    graph_retrieval_enabled: bool = False
    # THE KILL SWITCH for graph routing. With this false no query is answered
    # from the graph, whatever the class list below says, and the graph package
    # is not imported on the request path. Flipping it back to false is the
    # complete rollback -- nothing else has to be undone, because existing
    # retrieval was never replaced, only bypassed where the graph can answer.
    #
    # ON as of Phase 11, and since Phase 12 for every approved predicate rather
    # than four hand-written question shapes. Routing is now derived from the
    # closed predicate vocabulary and each predicate's declared domain and
    # range (app.retrieval.graph.plans), so a predicate that is approved into
    # the vocabulary becomes queryable without a new template, route or class.
    graph_routing_enabled: bool = True
    # Query classes routing may use, comma-separated. Empty means the built-in
    # default, which is now *every* known class -- see the note on
    # app.retrieval.graph.policy.DEFAULT_ENABLED_CLASSES for why the previous
    # four-class default could only ever return zero rows on this corpus.
    #
    # The setting survives as a rollout switch: naming classes here still
    # narrows routing to them, which is how a deployment stages the change or
    # isolates one class while debugging. It is no longer the definition of
    # what the graph knows -- that comes from the vocabulary, the reviewed
    # templates and the parameter validation, all of which apply whatever class
    # a route lands in. Legacy class names still gate the legacy templates, so
    # an existing value keeps its exact meaning.
    graph_routing_classes: str | None = None
    # Wall-clock budget for a whole graph attempt. Exceeding it falls back to
    # existing retrieval; measured p95 is far below this.
    graph_routing_budget_seconds: float = 3.0
    # Shadow mode: run graph retrieval beside production and log the comparison,
    # without touching the answer. Separate from graph_retrieval_enabled because
    # the point is to gather evidence on live traffic *before* routing anything.
    # The observation runs on a background thread and returns nothing, so with
    # this on the user's answer is still exactly what production produced.
    graph_shadow_enabled: bool = False
    # Optional JSONL destination for shadow observations. Unset: they go to the
    # application log only.
    graph_shadow_log_path: str | None = None
    # LLM claim extraction. The single most expensive step in the knowledge
    # layer -- one model call per eligible chunk -- so it is gated separately
    # from knowledge_enabled and launches OFF. The deterministic CMS-field
    # extractor needs no flag: it costs nothing and calls no model.
    claim_extraction_enabled: bool = False
    # A claim below this confidence is rejected rather than staged. CMS-field
    # claims assert 1.0, so this only ever bites model-proposed ones.
    claim_min_confidence: float = 0.6
    # Ceiling on model calls in one claim-extraction run, so an accidental
    # full-corpus pass cannot spend without bound. 0 disables the extractor.
    #
    # Enforced per run id since the safeguards work; before that it was declared
    # and never read. 200 was the value from when nothing checked it, and the
    # canary showed what enforcing it costs at that size: 200 calls covered 81
    # of 200 documents and left 68 partial, a backlog of legitimate work rather
    # than a guard against runaway spend.
    #
    # Sized from the canary instead. Measured mean 1.96 calls/document over the
    # uncensored window, so the remaining 11,803 documents are expected to want
    # about 23,100 calls; the theoretical maximum, every document using all
    # eight, is 94,424.
    #
    # Re-sized once the per-document ceiling moved to 32: the same canary
    # documents then want 4.51 calls each, so the remaining ~11,500 project to
    # roughly 51,900 calls. 60,000 would have been only 1.2x that — close
    # enough to bind on legitimate work, which is the failure this setting
    # should prevent rather than cause. 150,000 is 2.9x the projection and 41%
    # of the theoretical maximum (32 x 11,500 = 368,000), so a genuine runaway
    # still stops well short.
    #
    # The projection is an upper bound on the rate: the canary sampled the two
    # most claim-dense bundles, and 95% of what remains is in bundles it never
    # touched.
    claim_llm_max_calls_per_run: int = 150_000
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_database: str = ""
    mysql_connect_timeout: int = 10
    mysql_pool_size: int = 5
    # Max seconds a caller waits for a free pooled connection before failing fast
    # (instead of blocking forever) when every connection is checked out.
    mysql_pool_timeout: int = 30
    drupal_jsonapi_base: str = "https://teriin.org/jsonapi"
    drupal_request_timeout: int = 60
    drupal_page_size: int = 50
    drupal_max_retries: int = 3
    # When true, PDF links in rich text that point to external (non-teriin.org)
    # domains are also downloaded and extracted. Off by default: the corpus
    # stays TERI-authored and the external URL still survives in the body text.
    drupal_ingest_external_pdfs: bool = False
    # Custom blocks (block_content) shorter than this (stripped body) are treated
    # as chrome/boilerplate (Search box, "Follow us" strip) and skipped — unless
    # they carry a harvestable PDF link.
    drupal_block_min_chars: int = 200
    # Evidence-based publication-date resolution for attached PDFs
    # (app.ingestion.date_resolution). With this off, every PDF simply inherits
    # its node's date, which is the behaviour that predates the resolver. With it
    # on, a PDF may carry its own date only when the document states one and every
    # validated gate passes; the decision and its evidence are recorded in
    # `{state}_date_decision` either way.
    date_resolution_enabled: bool = True
    # Absolute (or CWD-relative) path to the theme hierarchy map that
    # app.catalog.theme_taxonomy classifies against. Empty means the shipped
    # default, `app/theme_structure.json`. Exists because the map is load-bearing
    # — an unreadable one degrades every theme row's parent/group — so which file
    # is authoritative has to be stateable rather than implied by a path
    # expression, and a deployment holding it elsewhere must not need a code edit.
    theme_taxonomy_path: str = ""
    ingest_state_table: str = "documents"
    # Append-only audit log of every ingestion event (one row per file/record
    # per run), separate from the overwrite-in-place documents table.
    ingest_log_table: str = "ingest_log"
    ingest_log_enabled: bool = True
    # Whether to record a per-document row for UNCHANGED docs. Off by default:
    # on an incremental sweep almost every doc is unchanged, so logging each one
    # is write amplification (one INSERT+commit per doc) and the main driver of
    # the log's growth. The run-level tally already reports the unchanged count.
    ingest_log_unchanged: bool = False
    # Days to keep ingest-log rows; older rows are pruned after each background
    # sweep. 0 disables pruning (the log then grows without bound).
    ingest_log_retention_days: int = 90
    # Batch controls for large (re)ingests. max_docs_per_run caps how many
    # documents actually get processed per run (new/changed/deleted; unchanged
    # scans are free) before the run stops cleanly; 0 = unlimited. Drupal
    # bundles are always crawled oldest-first, so the changed high-water mark
    # doubles as a resume cursor — a capped or interrupted run continues where
    # it stopped. batch_size/pause throttle within a run: sleep pause seconds
    # after every batch_size processed documents. workers > 1 processes that
    # many documents concurrently (one crawler, a pool of document workers —
    # keep workers below mysql_pool_size); the one-run-at-a-time lock still
    # applies.
    # Ingest-time LLM enrichment (app/ingestion/enrich.py): a per-document
    # abstract, generated once per content hash and cached in the
    # `<state>_enrichment` table. Launches OFF — the first pass over an existing
    # corpus costs real money, so it should be a deliberate act (flip this, or
    # run the backfill CLI) rather than something a scheduled sweep discovers.
    # With it on, the sweep enriches documents as it re-crawls them; documents
    # that never change are the backfill's job.
    enrichment_enabled: bool = False
    # How many times one document may fail enrichment before the sweep stops
    # retrying it. A version change (new prompt or model) resets the budget.
    enrichment_max_attempts: int = 3
    ingest_max_docs_per_run: int = 0
    ingest_batch_size: int = 0
    ingest_batch_pause_seconds: float = 0.0
    ingest_workers: int = 1
    # Delete reconciliation infers deletion from absence, so a live enumeration
    # that merely came back short is indistinguishable from a bundle that was
    # really emptied — and the deletion is immediate and total (points, catalog
    # row, facet rows) with nothing to restore from. HTTP failures already skip
    # the bundle; these two bound the damage a *successful* short response can do.
    #
    # A bundle is left alone when the share of its catalogued documents missing
    # from the live set reaches this fraction: 0.10 means one run may never
    # remove a tenth of a bundle.
    ingest_reconcile_max_missing_ratio: float = 0.10
    # ...except for this many documents, so a genuinely small bundle can still
    # lose one. Kept far below `drupal_page_size`, since the failure being
    # guarded against loses whole pages — an allowance this small cannot hide it.
    ingest_reconcile_min_deletions: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
