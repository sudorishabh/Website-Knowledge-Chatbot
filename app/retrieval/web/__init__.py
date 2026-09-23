"""Web retrieval: evidence from the public web, for what the corpus cannot answer.

Isolated by construction, like ``app.retrieval.graph``. Nothing on the default
retrieval path imports this package, and ``web_search_enabled`` is off — so with
the switch down the internal pipeline behaves exactly as it did before the web
existed.

What it is for
--------------
The internal corpus is authoritative and stays the first source. The web is a
fallback for the cases the corpus structurally cannot cover: a page ingestion
never crawls (an expert's profile), something published since the last sweep,
a question that explicitly asks to look something up. It is consulted only when
a question asks for it or the internal evidence is judged insufficient, never on
every query.

What it will not do
-------------------
* Write to the corpus. Fetched pages are ephemeral evidence for one answer; they
  are never indexed into Qdrant or catalogued in MySQL.
* Crawl. Only search-result URLs are fetched, and links inside a fetched page
  are never followed.
* Trust what it reads. A fetched page is untrusted input: its URL is checked
  before every request, its text is data and never instructions.

Modules
-------
:mod:`.safety`  whether a URL may be fetched at all (scheme, credentials, port,
                public addresses only), the domain policy (primary, blocked),
                and the canonical form two URLs are compared by.
:mod:`.cache`   time-limited caches for search results, extracted pages and
                robots.txt — Redis when configured, in-process otherwise.
:mod:`.providers` the search vendor behind one interface (Brave, Tavily), and
                the single ``search`` entry point that caches, retries, applies
                the domain policy, de-duplicates and traces.
"""
