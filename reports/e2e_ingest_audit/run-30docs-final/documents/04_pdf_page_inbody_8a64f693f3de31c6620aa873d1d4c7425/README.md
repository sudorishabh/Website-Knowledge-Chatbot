# 04_pdf_page_inbody_8a64f693f3de31c6620aa873d1d4c7425

**pdf_attachment** · bundle `page` · outcome **indexed**

- document_id: `inbody:8a64f693f3de31c6620aa873d1d4c742561566cb`
- title: TERI Alumni Association
- source_key: https://teriin.org/sites/default/files/files/TERI-Alumni-Quarterly-Inaugural-Issue.pdf
- parent page: `04107bb4-fefe-4ffa-be5d-f88ac9310dd6` — TERI Alumni Association (https://teriin.org/alumni)
- file: TERI-Alumni-Quarterly-Inaugural-Issue.pdf · origin `inbody` · description -
- handled in 8.702s (build 4.322s, extract 1.995s, chunk 0.168s, embed+upsert 4.02s, knowledge 0.167s)

## Files in this folder

- `00_source/parent_record.json` — raw JSON:API resource of the parent page (plus included entities)
- `00_source/file_link.json` — the file reference as discovered on the page, and the download result
- `00_source/TERI-Alumni-Quarterly-Inaugural-Issue.pdf` — the freshly downloaded PDF bytes
- `01_change_record.json` — the ChangeRecord as yielded by detect_drupal_changes, and the handler outcome
- `02_extraction.json` — ExtractionResult: every page's text, route (text/ocr/empty), tables, PDF metadata
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
- downloaded 26806398 bytes from `https://teriin.org/sites/default/files/files/TERI-Alumni-Quarterly-Inaugural-Issue.pdf` (requested `https://teriin.org/sites/default/files/files/TERI-Alumni-Quarterly-Inaugural-Issue.pdf`, https upgrade: False) · sha256 `3837c8a6fc2995112906da415b527f39408859d24543b432050eac0cd958095f`
- change detection: status **new** · fingerprint `inbody:8a64f693f3de31c6620aa873d1d4c742561566cb` · changed_mark 1788323098 · prior: False

### 2. Extraction
- 16 page(s) · routes {'text': 16} · tables 0 · PDF metadata keys ['extraction_mode', 'page_signals', 'route']
- characters: 37233 · empty pages: 0

### 3. Canonical document
- sections 16 · body chars 37263 · content_hash `91a5fb091b332ca311030cb92149ee0edd4b8d72f558da87303bf0c3a270409c` · doc_version 1
- authors [] · tags [] · categories []
- entity_refs 0 · file_links []
- extra {'bundle': 'page'}

### 4. Dates
- effective_start_date **2025-09-30T04:28:20+00:00** (precision day, source parent_page) · effective_end_date None (precision None)
- parent page resolution: rule `bundle_created` source `created` fields ['created'] raw []
- file resolver: overridden=False · decision action `keep_page_date` rule `multi_pdf_no_evidence` · edition_label None · evidence used ['drupal', 'pdf_meta', 'pdf_text']
- decision evidence: No per-document evidence of any kind; the page date stands. One of 3 PDFs on this page, so it was read for a date of its own; it states none that could be verified, and the page's date stands as this file's fallback.
- date_decision rows in MySQL: 1 — action `keep_page_date` rule `multi_pdf_no_evidence` candidate 2025-09-30 04:28:20

### 5. Chunks
- 5 parent(s), 23 child(ren) · child tokens min/avg/max 165/403/503 · section types ['None']

### 6. Qdrant
- collection `e2e_audit_documents` · points for this document: 28 · indexer reported 28
- delete_document calls: [{'document_id': 'inbody:8a64f693f3de31c6620aa873d1d4c742561566cb', 'keep_ids': 28}]

### 7. MySQL
- `e2e_audit_documents`: 1 row(s)
- `e2e_audit_documents_author`: 0 row(s)
- `e2e_audit_documents_tag`: 0 row(s)
- `e2e_audit_documents_theme`: 0 row(s)
- `e2e_audit_documents_retry`: 0 row(s)
- `e2e_audit_documents_dead_link`: 0 row(s)
- `e2e_audit_documents_date_decision`: 1 row(s)
- `e2e_audit_documents_entity_mention`: 56 row(s)
- `e2e_audit_documents_assertion`: 0 row(s)
- `e2e_audit_documents_assertion_rejection`: 0 row(s)
- `e2e_audit_documents_predicate_candidate`: 0 row(s)
- `e2e_audit_documents_knowledge_run`: 1 row(s)
- `e2e_audit_documents_attachment`: 0 row(s)
- `e2e_audit_documents_attachment (as file)`: 1 row(s)
- `e2e_audit_documents_entity_resolution_decision`: 56 row(s)
- `e2e_audit_documents_entity_extraction`: 23 row(s)
- `e2e_audit_documents_assertion_link`: 0 row(s)
- `e2e_audit_ingest_log`: 1 row(s)
- `e2e_audit_documents_entity (referenced)`: 0 row(s)

### 8. Knowledge stage
- status **ok** in 0.16s · knowledge_version `kv1:9f993bff646d51c0`
- counts {'chunks_seen': 23, 'chunks_cached': 0, 'mentions': 56, 'entities_auto': 0, 'entities_provisional': 0, 'entities_ambiguous': 9, 'entities_unresolved': 47, 'claims_built': 0, 'claims_staged': 0, 'claims_rejected': 0, 'claims_retracted': 0, 'pending_predicates': 0, 'conflicts_disputed': 0, 'conflicts_superseded': 0}
- projection {'status': 'skipped', 'version': None, 'edges': 0}
  - stage `prelude`: ran · counts {'entities': 2451, 'chunks': 23} · notes [] · errors []
  - stage `supersede`: skipped · counts {} · notes [] · errors []
  - stage `mentions`: ran · counts {'cached': 0, 'mentions': 56} · notes [] · errors []
  - stage `resolution`: ran · counts {'auto': 0, 'provisional': 0, 'ambiguous': 9, 'unresolved': 47, 'cache_recorded': 23} · notes [] · errors []
  - stage `claims`: ran · counts {'llm_calls': 0, 'llm_failures': 0, 'run_calls_used': 2, 'run_calls_remaining': 149998, 'llm': 0, 'unknown_predicates': 0, 'built': 0} · notes [] · errors []
  - stage `validate`: ran · counts {} · notes [] · errors []
  - stage `persist`: ran · counts {'accepted': 0, 'pending_predicates': 0} · notes [] · errors []
  - stage `conflicts`: skipped · counts {} · notes [] · errors []
  - stage `project`: skipped · counts {} · notes [] · errors []

### 9. Neo4j
- Document node: None · chunk stubs: 0 · Claim nodes: 0 · current-state edges from this document's claims: 0

## Checks

**91 pass · 0 fail · 10 n/a · 8 info**

| status | category | check | detail | expected | actual |
| --- | --- | --- | --- | --- | --- |
| PASS | outcome | handler_outcome_is_indexed | Every fresh document is expected to end 'indexed'. | indexed | indexed |
| PASS | outcome | no_exception | - | - | - |
| PASS | source | pdf_downloaded_non_empty | - | >0 bytes | 26806398 |
| INFO | source | pdf_pages | 16 pages extracted, 16 with text | - | {'pages': 16, 'with_text': 16, 'routes': {'text': 16}, 'tables': 0} |
| PASS | canonical | sections_equal_non_empty_pages | from_pdf makes one section per page with text. | 16 | 16 |
| PASS | canonical | section_text_equals_page_text | Section text is the page text verbatim. | - | - |
| PASS | canonical | title_precedence_description_node_filename | title = file.description or node.title or filename | TERI Alumni Association | TERI Alumni Association |
| PASS | canonical | linked_article_uuid_is_parent_node | - | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 |
| PASS | canonical | source_url_is_parent_page | - | https://teriin.org/alumni | https://teriin.org/alumni |
| PASS | canonical | file_url_is_fetched_url | - | https://teriin.org/sites/default/files/files/TERI-Alumni-Quarterly-In… | https://teriin.org/sites/default/files/files/TERI-Alumni-Quarterly-In… |
| PASS | canonical | bundle_inherited_from_parent | - | page | page |
| PASS | parent_child | facets_inherited_from_parent | categories/tags/authors equal the parent page's | {'categories': [], 'tags': [], 'authors': []} | {'categories': [], 'tags': [], 'authors': []} |
| PASS | parent_child | entity_refs_inherited_from_parent | - | - | - |
| PASS | parent_child | parent_lists_this_file | - | inbody:8a64f693f3de31c6620aa873d1d4c742561566cb | ['inbody:8a64f693f3de31c6620aa873d1d4c742561566cb', 'inbody:019c7e591… |
| PASS | canonical | content_hash_is_sha256_of_body_text | - | - | 91a5fb091b332ca311030cb92149ee0edd4b8d72f558da87303bf0c3a270409c |
| PASS | canonical | body_text_non_empty | - | >0 chars | 37263 |
| PASS | dates | effective_start_date_present | Undated documents are excluded from date filters (pipeline warns). | a date | 2025-09-30T04:28:20+00:00 |
| PASS | dates | resolver_ran | - | - | - |
| INFO | dates | pdf_date_path | inherited from parent page | - | {'parent_rule': 'bundle_created', 'parent_source': 'created', 'decisi… |
| PASS | dates | pdf_start_date_equals_parent_resolved | - | 2025-09-30T04:28:20+00:00 | 2025-09-30T04:28:20+00:00 |
| PASS | dates | pdf_precision_equals_parent | - | day | day |
| PASS | dates | pdf_end_date_equals_parent | - | - | - |
| PASS | dates | date_source_is_parent_page | - | parent_page | parent_page |
| PASS | dates | parent_page_canonical_date_matches_parent_resolution | The page's own document and the value handed to its files agree. | 2025-09-30T04:28:20+00:00 | 2025-09-30T04:28:20+00:00 |
| PASS | dates | date_decision_row_presence | A row is written when the resolver produced a decision. | True | 1 |
| PASS | dates | date_decision_row_names_parent_node | - | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 |
| PASS | dates | date_decision_current_date_is_parent_resolved | - | 2025-09-30T04:28:20+00:00 | 2025-09-30 04:28:20 |
| PASS | chunks | chunks_produced | - | >0 children | {'parents': 5, 'children': 23} |
| PASS | chunks | chunk_ids_unique | - | 28 | 28 |
| PASS | chunks | every_parent_reference_resolves | - | - | - |
| PASS | chunks | parents_have_at_least_two_children | A parent is emitted only when it groups more than one child. | ['17c31e80-3498-556f-8f98-07ee9175668a', '1adf97c1-9d59-57ca-bca1-ad7… | {'bbb5de42-74b7-5238-a79a-61467f6b5424': 5, 'c796ee51-7d19-568a-a9d1-… |
| PASS | chunks | children_carry_document_id | - | - | - |
| PASS | chunks | children_carry_doc_version | - | 1 | [1] |
| PASS | chunks | children_within_token_limit | child_max_tokens=560 | 560 | {'max_child_tokens': 503, 'over': []} |
| PASS | chunks | parents_within_token_limit | parent_max_tokens=2600 | 2600 | {'max_parent_tokens': 2088, 'over': []} |
| PASS | chunks | content_hash_is_sha256_of_text | - | - | - |
| PASS | chunks | child_index_is_contiguous | - | [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19… | [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19… |
| PASS | chunks | children_carry_page_numbers | - | - | - |
| PASS | chunks | source_lines_covered_by_chunks | 100.000% of 823 source lines appear verbatim in some chunk | >=98% | {'coverage': 1.0, 'missing_sample': []} |
| PASS | chunks | children_have_embed_text_with_breadcrumb | embed_text = 'title › heading' + text | - | - |
| PASS | qdrant | point_count_equals_chunks | - | {'chunks': 28, 'indexer_reported': 28} | 28 |
| PASS | qdrant | every_chunk_id_is_a_point | - | 28 | {'missing': [], 'extra': []} |
| PASS | qdrant | payload_equals_chunk_payload_plus_stamps | Stored payload == Chunk.to_payload() plus created_at/updated_at/embed_model. | - | - |
| PASS | qdrant | children_have_real_vectors | - | - | - |
| PASS | qdrant | parents_have_zero_vectors | - | - | - |
| PASS | qdrant | vector_dimension_matches_setting | - | 3072 | [3072] |
| PASS | qdrant | children_stamped_with_embed_model | - | - | ['text-embedding-3-large:3072'] |
| PASS | qdrant | points_carry_document_id | - | - | - |
| PASS | qdrant | points_carry_effective_start_date | - | 2025-09-30T04:28:20+00:00 | ['2025-09-30T04:28:20+00:00'] |
| PASS | qdrant | points_carry_year_precision_only_when_year | - | day | ['None'] |
| PASS | qdrant | points_are_current_and_versioned | - | 1 | ['1'] |
| PASS | qdrant | points_stamped_with_pipeline_version | - | c1.i1.p2.e1 | ['c1.i1.p2.e1'] |
| PASS | qdrant | swap_deleted_with_keep_ids | delete_document(id, keep_ids=<new chunk ids>) ran after the upsert. | 28 | [{'document_id': 'inbody:8a64f693f3de31c6620aa873d1d4c742561566cb', '… |
| PASS | mysql | state_row_exists | - | - | - |
| PASS | mysql | row_source_type | - | pdf_attachment | pdf_attachment |
| PASS | mysql | row_source_key | - | https://teriin.org/sites/default/files/files/TERI-Alumni-Quarterly-In… | https://teriin.org/sites/default/files/files/TERI-Alumni-Quarterly-In… |
| PASS | mysql | row_fingerprint | - | inbody:8a64f693f3de31c6620aa873d1d4c742561566cb | inbody:8a64f693f3de31c6620aa873d1d4c742561566cb |
| PASS | mysql | row_content_hash | - | 91a5fb091b332ca311030cb92149ee0edd4b8d72f558da87303bf0c3a270409c | 91a5fb091b332ca311030cb92149ee0edd4b8d72f558da87303bf0c3a270409c |
| PASS | mysql | row_doc_version | - | 1 | 1 |
| PASS | mysql | row_pipeline_version | - | c1.i1.p2.e1 | c1.i1.p2.e1 |
| PASS | mysql | row_bundle | - | page | page |
| PASS | mysql | row_entity_type | - | - | - |
| PASS | mysql | row_changed_mark | - | 1788323098 | 1788323098 |
| PASS | mysql | row_title | - | TERI Alumni Association | TERI Alumni Association |
| PASS | mysql | row_url | - | https://teriin.org/alumni | https://teriin.org/alumni |
| PASS | mysql | row_effective_start_date | - | 2025-09-30T04:28:20+00:00 | 2025-09-30 04:28:20 |
| PASS | mysql | row_date_source | - | parent_page | parent_page |
| PASS | mysql | row_start_precision | - | day | day |
| PASS | mysql | row_effective_end_date | - | - | - |
| PASS | mysql | row_end_precision | - | - | - |
| PASS | mysql | row_indexed_at_set | - | not null | 2026-09-10 10:26:01 |
| PASS | mysql | row_raw_meta_equals_canonical | - | - | - |
| PASS | mysql | author_facet_rows | - | - | - |
| PASS | mysql | tag_facet_rows | - | - | - |
| PASS | mysql | theme_rows_match_classification | documents_theme = theme_taxonomy.classify(categories) | - | - |
| PASS | mysql | attachment_link_rows | - | - | - |
| PASS | mysql | ingest_log_indexed_row | - | 1 | ['indexed'] |
| PASS | mysql | ingest_log_chunk_count | - | 28 | 28 |
| PASS | mysql | ingest_log_hash_and_version | - | - | - |
| PASS | mysql | no_retry_marker | - | - | - |
| PASS | parent_child | linked_from_parent_in_attachment_table | - | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 | ['04107bb4-fefe-4ffa-be5d-f88ac9310dd6'] |
| INFO | parent_child | parents_claiming_this_file | 1 page(s) link this file | - | ['04107bb4-fefe-4ffa-be5d-f88ac9310dd6'] |
| INFO | knowledge | stage_report | status=ok {'chunks_seen': 23, 'chunks_cached': 0, 'mentions': 56, 'entities_auto': 0, 'en… | - | {'document_id': 'inbody:8a64f693f3de31c6620aa873d1d4c742561566cb', 'd… |
| PASS | knowledge | run_row_written | - | 1 | [(1, 'ok')] |
| PASS | knowledge | run_row_matches_report | - | {'status': 'ok', 'mentions': 56, 'claims_staged': 0} | {'status': 'ok', 'mentions': 56, 'claims_staged': 0} |
| PASS | knowledge | mention_rows_equal_report | - | 56 | 56 |
| PASS | knowledge | decision_rows_equal_report | - | 56 | 56 |
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
| N/A | knowledge | authored_points_at_this_document | The object identifies the document; the entity link is the claim's own provenance. | inbody:8a64f693f3de31c6620aa873d1d4c742561566cb | - |
| N/A | knowledge | partner_of_points_at_an_organization | - | - | - |
| PASS | knowledge | authored_never_exceeds_its_source | An author claim per stated author at most; unresolved names are skipped, never invented. | <= 0 | 0 |
| PASS | knowledge | partner_of_never_exceeds_its_source | - | <= 0 | 0 |
| N/A | knowledge | authored_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | partner_of_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | no_duplicate_claim_identity | - | 0 | 0 |
| N/A | knowledge | claim_ids_are_unique | - | 0 | 0 |
| PASS | knowledge | extraction_cache_recorded | One cache row per child chunk content hash. | 23 | 23 |
| PASS | graph | nothing_projected_when_skipped | projection skipped: no touched claims | - | {'claims_in_graph': 0} |

