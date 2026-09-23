# 29_web_press_release_6d2d5ef3-25ab-4ff7-8583-5b9647588d7a

**website** · bundle `press_release` · outcome **indexed**

- document_id: `6d2d5ef3-25ab-4ff7-8583-5b9647588d7a`
- title: TERI Brings WSDS 2027 Curtain Raiser to Guwahati, Focus on Northeast Climate Resilience
- source_key: https://teriin.org/press-release/teri-brings-wsds-2027-curtain-raiser-guwahati-focus-northeast-climate-resilience
- handled in 2.519s (build 0.0s, extract Nones, chunk 0.01s, embed+upsert 0.442s, knowledge 2.012s)

## Files in this folder

- `00_source/record.json` — raw JSON:API resource (with included entities) and the parsed DrupalRecord
- `01_change_record.json` — the ChangeRecord as yielded by detect_drupal_changes, and the handler outcome
- `02_extraction.json` — HTML→text per rich-text field, discovered PDF links, entity refs, scalar metadata
- `03_canonical.json` — CanonicalDocument (all fields, all sections)
- `03_canonical_text.txt` — full_text(): the exact string content_hash covers
- `04_dates.json` — EffectiveDate evidence (page) / ResolvedDate (file), raw CMS values, what was applied
- `05_chunks.json` — every Chunk: text, embed_text, ids, parent link, pages, tokens, the payload it was indexed with
- `05_chunks.md` — the parent/child tree in readable form
- `06_qdrant.json` — every point read back from Qdrant: payload + vector summary
- `07_mysql.json` — every row in every catalog/knowledge table that refers to this document
- `08_knowledge.json` — knowledge StageReport + the mention/decision/claim/rejection/candidate rows
- `09_neo4j.json` — Document stub, chunk stubs, Claim nodes and current-state edges in the graph
- `10_checks.json` — every cross-stage check with expected/actual
- `log.txt` — the pipeline's own log lines while this document was handled

## Stage trace

### 1. Source (fresh fetch)
- JSON:API `node/press_release` uuid `6d2d5ef3-25ab-4ff7-8583-5b9647588d7a` · nid 13387 · created 2026-09-10T04:52:01+00:00 · changed 2026-09-10T05:21:34+00:00
- change detection: status **new** · fingerprint `2026-09-10T05:21:34+00:00` · changed_mark 1789017694 · prior: False

### 2. Extraction
- HTML→text: body 8982 chars from 1 rich-text field(s); 0 PDF link(s) discovered ([])

### 3. Canonical document
- sections 1 · body chars 8982 · content_hash `eb6df799886f767e9bc39929de85f7ac5ef55182f33c08c7bdae6528af73183e` · doc_version 1
- authors [] · tags ['Corporate Sustainability', 'Biodiversity conservation', 'Environmental awareness'] · categories ['Corporate Social Responsibility']
- entity_refs 4 · file_links []
- extra {'bundle': 'press_release', 'nid': 13387, 'changed': '2026-09-10T05:21:34+00:00'}

### 4. Dates
- effective_start_date **2026-09-09T00:00:00+00:00** (precision day, source cms_field) · effective_end_date None (precision None)
- rule `bundle_date_field` · source `cms_field` · fields ['field_pressrelease_date'] · raw values ['2026-09-09T04:30:00+00:00'] · role date · range_issue None
- date_decision rows in MySQL: 1 — action `propose_override` rule `bundle_date_field` candidate 2026-09-09 00:00:00

### 5. Chunks
- 1 parent(s), 6 child(ren) · child tokens min/avg/max 204/318/410 · section types ['None']

### 6. Qdrant
- collection `e2e_audit_documents` · points for this document: 7 · indexer reported 7
- delete_document calls: [{'document_id': '6d2d5ef3-25ab-4ff7-8583-5b9647588d7a', 'keep_ids': 7}]

### 7. MySQL
- `e2e_audit_documents`: 1 row(s)
- `e2e_audit_documents_author`: 0 row(s)
- `e2e_audit_documents_tag`: 3 row(s)
- `e2e_audit_documents_theme`: 1 row(s)
- `e2e_audit_documents_retry`: 0 row(s)
- `e2e_audit_documents_dead_link`: 0 row(s)
- `e2e_audit_documents_date_decision`: 1 row(s)
- `e2e_audit_documents_entity_mention`: 22 row(s)
- `e2e_audit_documents_assertion`: 0 row(s)
- `e2e_audit_documents_assertion_rejection`: 0 row(s)
- `e2e_audit_documents_predicate_candidate`: 0 row(s)
- `e2e_audit_documents_knowledge_run`: 1 row(s)
- `e2e_audit_documents_attachment`: 0 row(s)
- `e2e_audit_documents_attachment (as file)`: 0 row(s)
- `e2e_audit_documents_entity_resolution_decision`: 22 row(s)
- `e2e_audit_documents_entity_extraction`: 6 row(s)
- `e2e_audit_documents_assertion_link`: 0 row(s)
- `e2e_audit_ingest_log`: 1 row(s)
- `e2e_audit_documents_entity (referenced)`: 1 row(s)

### 8. Knowledge stage
- status **ok** in 2.01s · knowledge_version `kv1:9f993bff646d51c0`
- counts {'chunks_seen': 6, 'chunks_cached': 0, 'mentions': 22, 'entities_auto': 2, 'entities_provisional': 0, 'entities_ambiguous': 2, 'entities_unresolved': 18, 'claims_built': 0, 'claims_staged': 0, 'claims_rejected': 0, 'claims_retracted': 0, 'pending_predicates': 0, 'conflicts_disputed': 0, 'conflicts_superseded': 0}
- projection {'status': 'skipped', 'version': None, 'edges': 0}
  - stage `prelude`: ran · counts {'entities': 2451, 'chunks': 6} · notes [] · errors []
  - stage `supersede`: skipped · counts {} · notes [] · errors []
  - stage `mentions`: ran · counts {'cached': 0, 'mentions': 22} · notes [] · errors []
  - stage `resolution`: ran · counts {'auto': 2, 'provisional': 0, 'ambiguous': 2, 'unresolved': 18, 'cache_recorded': 6} · notes [] · errors []
  - stage `claims`: ran · counts {'llm_calls': 1, 'llm_failures': 0, 'run_calls_used': 47, 'run_calls_remaining': 149953, 'llm': 0, 'unknown_predicates': 0, 'built': 0} · notes [] · errors []
  - stage `validate`: ran · counts {} · notes [] · errors []
  - stage `persist`: ran · counts {'accepted': 0, 'pending_predicates': 0} · notes [] · errors []
  - stage `conflicts`: skipped · counts {} · notes [] · errors []
  - stage `project`: skipped · counts {} · notes [] · errors []

### 9. Neo4j
- Document node: None · chunk stubs: 0 · Claim nodes: 0 · current-state edges from this document's claims: 0

## Checks

**81 pass · 0 fail · 10 n/a · 6 info**

| status | category | check | detail | expected | actual |
| --- | --- | --- | --- | --- | --- |
| PASS | outcome | handler_outcome_is_indexed | Every fresh document is expected to end 'indexed'. | indexed | indexed |
| PASS | outcome | no_exception | - | - | - |
| PASS | canonical | title_equals_node_title | - | TERI Brings WSDS 2027 Curtain Raiser to Guwahati, Focus on Northeast … | TERI Brings WSDS 2027 Curtain Raiser to Guwahati, Focus on Northeast … |
| PASS | canonical | body_is_single_section | Website body becomes one section. | 8982 | 8982 |
| PASS | canonical | raw_meta_equals_record_metadata | - | - | - |
| PASS | canonical | article_uuid_is_node_uuid | - | 6d2d5ef3-25ab-4ff7-8583-5b9647588d7a | 6d2d5ef3-25ab-4ff7-8583-5b9647588d7a |
| PASS | canonical | file_links_equal_record_files | - | - | - |
| PASS | canonical | extra_carries_bundle_nid_changed | - | - | {'bundle': 'press_release', 'nid': 13387, 'changed': '2026-09-10T05:2… |
| PASS | canonical | facets_match_drupal_facets_rule | - | {'categories': ['Corporate Social Responsibility'], 'tags': ['Corpora… | {'categories': ['Corporate Social Responsibility'], 'tags': ['Corpora… |
| PASS | canonical | content_hash_is_sha256_of_body_text | - | - | eb6df799886f767e9bc39929de85f7ac5ef55182f33c08c7bdae6528af73183e |
| PASS | canonical | body_text_non_empty | - | >0 chars | 8982 |
| PASS | dates | effective_start_date_present | Undated documents are excluded from date filters (pipeline warns). | a date | 2026-09-09T00:00:00+00:00 |
| PASS | dates | effective_date_evidence_present | - | - | - |
| INFO | dates | website_date_rule | rule=bundle_date_field source=cms_field fields=['field_pressrelease_date'] | - | {'rule': 'bundle_date_field', 'source': 'cms_field', 'fields': ['fiel… |
| PASS | dates | canonical_date_equals_evidence_value | - | 2026-09-09T00:00:00+00:00 | 2026-09-09T00:00:00+00:00 |
| PASS | dates | canonical_end_equals_evidence_end | - | - | - |
| PASS | dates | date_source_equals_evidence_source | - | cms_field | cms_field |
| PASS | dates | date_decision_row_presence | Written only when the bundle maps to a real CMS date field. | True | 1 |
| PASS | chunks | chunks_produced | - | >0 children | {'parents': 1, 'children': 6} |
| PASS | chunks | chunk_ids_unique | - | 7 | 7 |
| PASS | chunks | every_parent_reference_resolves | - | - | - |
| PASS | chunks | parents_have_at_least_two_children | A parent is emitted only when it groups more than one child. | ['b4b29c3f-fdf3-56ca-b926-3877b2a5c917'] | {'b4b29c3f-fdf3-56ca-b926-3877b2a5c917': 6} |
| PASS | chunks | children_carry_document_id | - | - | - |
| PASS | chunks | children_carry_doc_version | - | 1 | [1] |
| PASS | chunks | children_within_token_limit | child_max_tokens=480 | 480 | {'max_child_tokens': 410, 'over': []} |
| PASS | chunks | parents_within_token_limit | parent_max_tokens=2200 | 2200 | {'max_parent_tokens': 1734, 'over': []} |
| PASS | chunks | content_hash_is_sha256_of_text | - | - | - |
| PASS | chunks | child_index_is_contiguous | - | [0, 1, 2, 3, 4, 5] | [0, 1, 2, 3, 4, 5] |
| PASS | chunks | source_lines_covered_by_chunks | 100.000% of 18 source lines appear verbatim in some chunk | >=98% | {'coverage': 1.0, 'missing_sample': []} |
| PASS | chunks | children_have_embed_text_with_breadcrumb | embed_text = 'title › heading' + text | - | - |
| PASS | qdrant | point_count_equals_chunks | - | {'chunks': 7, 'indexer_reported': 7} | 7 |
| PASS | qdrant | every_chunk_id_is_a_point | - | 7 | {'missing': [], 'extra': []} |
| PASS | qdrant | payload_equals_chunk_payload_plus_stamps | Stored payload == Chunk.to_payload() plus created_at/updated_at/embed_model. | - | - |
| PASS | qdrant | children_have_real_vectors | - | - | - |
| PASS | qdrant | parents_have_zero_vectors | - | - | - |
| PASS | qdrant | vector_dimension_matches_setting | - | 3072 | [3072] |
| PASS | qdrant | children_stamped_with_embed_model | - | - | ['text-embedding-3-large:3072'] |
| PASS | qdrant | points_carry_document_id | - | - | - |
| PASS | qdrant | points_carry_effective_start_date | - | 2026-09-09T00:00:00+00:00 | ['2026-09-09T00:00:00+00:00'] |
| PASS | qdrant | points_carry_year_precision_only_when_year | - | day | ['None'] |
| PASS | qdrant | points_are_current_and_versioned | - | 1 | ['1'] |
| PASS | qdrant | points_stamped_with_pipeline_version | - | c1.i1.p2.e1 | ['c1.i1.p2.e1'] |
| PASS | qdrant | swap_deleted_with_keep_ids | delete_document(id, keep_ids=<new chunk ids>) ran after the upsert. | 7 | [{'document_id': '6d2d5ef3-25ab-4ff7-8583-5b9647588d7a', 'keep_ids': … |
| PASS | mysql | state_row_exists | - | - | - |
| PASS | mysql | row_source_type | - | website | website |
| PASS | mysql | row_source_key | - | https://teriin.org/press-release/teri-brings-wsds-2027-curtain-raiser… | https://teriin.org/press-release/teri-brings-wsds-2027-curtain-raiser… |
| PASS | mysql | row_fingerprint | - | 2026-09-10T05:21:34+00:00 | 2026-09-10T05:21:34+00:00 |
| PASS | mysql | row_content_hash | - | eb6df799886f767e9bc39929de85f7ac5ef55182f33c08c7bdae6528af73183e | eb6df799886f767e9bc39929de85f7ac5ef55182f33c08c7bdae6528af73183e |
| PASS | mysql | row_doc_version | - | 1 | 1 |
| PASS | mysql | row_pipeline_version | - | c1.i1.p2.e1 | c1.i1.p2.e1 |
| PASS | mysql | row_bundle | - | press_release | press_release |
| PASS | mysql | row_entity_type | - | node | node |
| PASS | mysql | row_changed_mark | - | 1789017694 | 1789017694 |
| PASS | mysql | row_title | - | TERI Brings WSDS 2027 Curtain Raiser to Guwahati, Focus on Northeast … | TERI Brings WSDS 2027 Curtain Raiser to Guwahati, Focus on Northeast … |
| PASS | mysql | row_url | - | https://teriin.org/press-release/teri-brings-wsds-2027-curtain-raiser… | https://teriin.org/press-release/teri-brings-wsds-2027-curtain-raiser… |
| PASS | mysql | row_effective_start_date | - | 2026-09-09T00:00:00+00:00 | 2026-09-09 00:00:00 |
| PASS | mysql | row_date_source | - | cms_field | cms_field |
| PASS | mysql | row_start_precision | - | day | day |
| PASS | mysql | row_effective_end_date | - | - | - |
| PASS | mysql | row_end_precision | - | - | - |
| PASS | mysql | row_indexed_at_set | - | not null | 2026-09-10 10:30:55 |
| PASS | mysql | row_raw_meta_equals_canonical | - | - | - |
| PASS | mysql | author_facet_rows | - | - | - |
| PASS | mysql | tag_facet_rows | - | ['Biodiversity conservation', 'Corporate Sustainability', 'Environmen… | ['Biodiversity conservation', 'Corporate Sustainability', 'Environmen… |
| PASS | mysql | theme_rows_match_classification | documents_theme = theme_taxonomy.classify(categories) | [('Corporate Social Responsibility', 'primary', None, 'other', 'Corpo… | [('Corporate Social Responsibility', 'primary', None, 'other', 'Corpo… |
| PASS | mysql | attachment_link_rows | - | - | - |
| PASS | mysql | ingest_log_indexed_row | - | 1 | ['indexed'] |
| PASS | mysql | ingest_log_chunk_count | - | 7 | 7 |
| PASS | mysql | ingest_log_hash_and_version | - | - | - |
| PASS | mysql | no_retry_marker | - | - | - |
| INFO | knowledge | stage_report | status=ok {'chunks_seen': 6, 'chunks_cached': 0, 'mentions': 22, 'entities_auto': 2, 'ent… | - | {'document_id': '6d2d5ef3-25ab-4ff7-8583-5b9647588d7a', 'doc_version'… |
| PASS | knowledge | run_row_written | - | 1 | [(1, 'ok')] |
| PASS | knowledge | run_row_matches_report | - | {'status': 'ok', 'mentions': 22, 'claims_staged': 0} | {'status': 'ok', 'mentions': 22, 'claims_staged': 0} |
| PASS | knowledge | mention_rows_equal_report | - | 22 | 22 |
| PASS | knowledge | decision_rows_equal_report | - | 22 | 22 |
| PASS | knowledge | mentions_point_at_own_chunks | - | - | - |
| PASS | knowledge | staged_claim_rows_equal_report | - | 0 | 0 |
| PASS | knowledge | claims_cite_own_chunks_or_document | - | - | - |
| PASS | knowledge | rejection_rows_equal_report | - | 0 | 0 |
| INFO | knowledge | claims_by_predicate | {} | - | - |
| INFO | knowledge | claims_by_method | {} | - | - |
| INFO | knowledge | cms_author_values | 0 author value(s) in metadata | - | - |
| INFO | knowledge | cms_partner_values | 0 partner value(s) in metadata | - | - |
| N/A | knowledge | authored_is_cms_only | AUTHORED must never come from the LLM. | cms_field | - |
| N/A | knowledge | partner_of_is_cms_only | PARTNER_OF must never come from the LLM. | cms_field | - |
| N/A | knowledge | authored_records_its_source_field | - | ['field_authors', 'field_rpaper_author', 'field_article_authors', 'fi… | - |
| N/A | knowledge | partner_of_records_its_source_field | - | ['field_completed_partners', 'field_ongoing_partners'] | - |
| N/A | knowledge | authored_points_at_this_document | The object identifies the document; the entity link is the claim's own provenance. | 6d2d5ef3-25ab-4ff7-8583-5b9647588d7a | - |
| N/A | knowledge | partner_of_points_at_an_organization | - | - | - |
| PASS | knowledge | authored_never_exceeds_its_source | An author claim per stated author at most; unresolved names are skipped, never invented. | <= 0 | 0 |
| PASS | knowledge | partner_of_never_exceeds_its_source | - | <= 0 | 0 |
| N/A | knowledge | authored_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | partner_of_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | no_duplicate_claim_identity | - | 0 | 0 |
| N/A | knowledge | claim_ids_are_unique | - | 0 | 0 |
| PASS | knowledge | extraction_cache_recorded | One cache row per child chunk content hash. | 6 | 6 |
| PASS | graph | nothing_projected_when_skipped | projection skipped: no touched claims | - | {'claims_in_graph': 0} |

