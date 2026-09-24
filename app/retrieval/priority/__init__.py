"""Priority pages: the organisation's own pages, read live at question time.

A fixed list (``data/priority_crawl_pages.json``) names pages the Drupal crawl
cannot reproduce — theme and regional-centre pages built from Views, the people
listings and their profiles — and institutional pages whose stored copy is to be
ignored in favour of the live one. When a question concerns one of them, the
page is fetched, cut into sections, and its most relevant sections lead the
context; when it does not, nothing is fetched.

Documents a page links to (PDFs and the like) are never downloaded here: they
reach the answer as links only.

Reading order:

* :mod:`.registry` — the page list: pages, groups, and the phrases that name them.
"""
