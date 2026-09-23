# 24_pdf_events_6658c489-9aad-483b-9004-27b62225425d

**pdf_attachment** · bundle `events` · outcome **indexed**

- document_id: `6658c489-9aad-483b-9004-27b62225425d`
- title: Concept Note WCEF
- source_key: https://teriin.org/sites/default/files/2026-09/Concept%20Note_WCEF_CPP_Ecolabel_virtual.pdf
- parent page: `c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1` — WCEF2026 Accelerator Session: Circular Public Procurement for Sustainable Consumption and Production (https://teriin.org/event/wcef2026-accelerator-session-circular-public-procurement-and-ecolabels)
- file: Concept Note_WCEF_CPP_Ecolabel_virtual.pdf · origin `attachment` · description Concept Note WCEF
- handled in 1.346s (build 0.914s, extract 0.765s, chunk 0.004s, embed+upsert 0.37s, knowledge 0.018s)

## Files in this folder

- `00_source/parent_record.json` — raw JSON:API resource of the parent page (plus included entities)
- `00_source/file_link.json` — the file reference as discovered on the page, and the download result
- `00_source/Concept_Note_WCEF_CPP_Ecolabel_virtual.pdf` — the freshly downloaded PDF bytes
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
- downloaded 304839 bytes from `https://teriin.org/sites/default/files/2026-09/Concept%20Note_WCEF_CPP_Ecolabel_virtual.pdf` (requested `https://teriin.org/sites/default/files/2026-09/Concept%20Note_WCEF_CPP_Ecolabel_virtual.pdf`, https upgrade: False) · sha256 `3785260b970a8802b8f6bb593c9b5d2c81a6e6be1239b81191e91e1ea2e92839`
- change detection: status **new** · fingerprint `2026-09-08T06:49:49+00:00` · changed_mark 1788850189 · prior: False

### 2. Extraction
- 2 page(s) · routes {'text': 2} · tables 1 · PDF metadata keys ['extraction_mode', 'page_signals', 'route']
- characters: 3547 · empty pages: 0

### 3. Canonical document
- sections 2 · body chars 3549 · content_hash `2906b1d7628d9b151da8062e4db4fb8aa10cc500de6514187bb711fe69a62695` · doc_version 1
- authors [] · tags ['Environmental governance', 'Government policy/regulations', 'Government schemes', 'Sustainable development', 'Sustainable Development Goals', 'Sustainable public procurement'] · categories ['Resources & Sustainable Development']
- entity_refs 12 · file_links []
- extra {'bundle': 'events'}

### 4. Dates
- effective_start_date **2026-09-18T00:00:00+00:00** (precision day, source parent_page) · effective_end_date 2026-09-18T00:00:00+00:00 (precision day)
- parent page resolution: rule `bundle_date_field` source `cms_field` fields ['field_event_start_date', 'field_event_end_date'] raw ['2026-09-18T08:30:00+00:00', '2026-09-18T10:00:00+00:00']
- file resolver: overridden=False · decision action `keep_page_date` rule `parent_bundle_date_field` · edition_label None · evidence used ['drupal']
- decision evidence: The parent events page states its date in field_event_start_date ('2026-09-18T08:30:00+00:00'); it holds one PDF, so that file carries its page's date.
- date_decision rows in MySQL: 1 — action `keep_page_date` rule `parent_bundle_date_field` candidate 2026-09-18 00:00:00

### 5. Chunks
- 0 parent(s), 3 child(ren) · child tokens min/avg/max 132/226/282 · section types ['None']

### 6. Qdrant
- collection `e2e_audit_documents` · points for this document: 3 · indexer reported 3
- delete_document calls: [{'document_id': '6658c489-9aad-483b-9004-27b62225425d', 'keep_ids': 3}]

### 7. MySQL
- `e2e_audit_documents`: 1 row(s)
- `e2e_audit_documents_author`: 0 row(s)
- `e2e_audit_documents_tag`: 6 row(s)
- `e2e_audit_documents_theme`: 1 row(s)
- `e2e_audit_documents_retry`: 0 row(s)
- `e2e_audit_documents_dead_link`: 0 row(s)
- `e2e_audit_documents_date_decision`: 1 row(s)
- `e2e_audit_documents_entity_mention`: 0 row(s)
- `e2e_audit_documents_assertion`: 0 row(s)
- `e2e_audit_documents_assertion_rejection`: 0 row(s)
- `e2e_audit_documents_predicate_candidate`: 0 row(s)
- `e2e_audit_documents_knowledge_run`: 1 row(s)
- `e2e_audit_documents_attachment`: 0 row(s)
- `e2e_audit_documents_attachment (as file)`: 1 row(s)
- `e2e_audit_documents_entity_resolution_decision`: 0 row(s)
- `e2e_audit_documents_entity_extraction`: 3 row(s)
- `e2e_audit_documents_assertion_link`: 0 row(s)
- `e2e_audit_ingest_log`: 1 row(s)
- `e2e_audit_documents_entity (referenced)`: 0 row(s)

### 8. Knowledge stage
- status **ok** in 0.01s · knowledge_version `kv1:9f993bff646d51c0`
- counts {'chunks_seen': 3, 'chunks_cached': 0, 'mentions': 0, 'entities_auto': 0, 'entities_provisional': 0, 'entities_ambiguous': 0, 'entities_unresolved': 0, 'claims_built': 0, 'claims_staged': 0, 'claims_rejected': 0, 'claims_retracted': 0, 'pending_predicates': 0, 'conflicts_disputed': 0, 'conflicts_superseded': 0}
- projection {'status': 'skipped', 'version': None, 'edges': 0}
  - stage `prelude`: ran · counts {'entities': 2451, 'chunks': 3} · notes [] · errors []
  - stage `supersede`: skipped · counts {} · notes [] · errors []
  - stage `mentions`: ran · counts {'cached': 0, 'mentions': 0} · notes [] · errors []
  - stage `resolution`: ran · counts {'auto': 0, 'provisional': 0, 'ambiguous': 0, 'unresolved': 0, 'cache_recorded': 3} · notes [] · errors []
  - stage `claims`: ran · counts {'llm_calls': 0, 'llm_failures': 0, 'run_calls_used': 44, 'run_calls_remaining': 149956, 'llm': 0, 'unknown_predicates': 0, 'built': 0} · notes [] · errors []
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
| PASS | source | pdf_downloaded_non_empty | - | >0 bytes | 304839 |
| INFO | source | pdf_pages | 2 pages extracted, 2 with text | - | {'pages': 2, 'with_text': 2, 'routes': {'text': 2}, 'tables': 1} |
| PASS | canonical | sections_equal_non_empty_pages | from_pdf makes one section per page with text. | 2 | 2 |
| PASS | canonical | section_text_equals_page_text | Section text is the page text verbatim. | - | - |
| PASS | canonical | title_precedence_description_node_filename | title = file.description or node.title or filename | Concept Note WCEF | Concept Note WCEF |
| PASS | canonical | linked_article_uuid_is_parent_node | - | c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1 | c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1 |
| PASS | canonical | source_url_is_parent_page | - | https://teriin.org/event/wcef2026-accelerator-session-circular-public… | https://teriin.org/event/wcef2026-accelerator-session-circular-public… |
| PASS | canonical | file_url_is_fetched_url | - | https://teriin.org/sites/default/files/2026-09/Concept%20Note_WCEF_CP… | https://teriin.org/sites/default/files/2026-09/Concept%20Note_WCEF_CP… |
| PASS | canonical | bundle_inherited_from_parent | - | events | events |
| PASS | parent_child | facets_inherited_from_parent | categories/tags/authors equal the parent page's | {'categories': ['Resources & Sustainable Development'], 'tags': ['Env… | {'categories': ['Resources & Sustainable Development'], 'tags': ['Env… |
| PASS | parent_child | entity_refs_inherited_from_parent | - | - | - |
| PASS | parent_child | parent_lists_this_file | - | 6658c489-9aad-483b-9004-27b62225425d | ['6658c489-9aad-483b-9004-27b62225425d'] |
| PASS | canonical | content_hash_is_sha256_of_body_text | - | - | 2906b1d7628d9b151da8062e4db4fb8aa10cc500de6514187bb711fe69a62695 |
| PASS | canonical | body_text_non_empty | - | >0 chars | 3549 |
| PASS | dates | effective_start_date_present | Undated documents are excluded from date filters (pipeline warns). | a date | 2026-09-18T00:00:00+00:00 |
| PASS | dates | resolver_ran | - | - | - |
| INFO | dates | pdf_date_path | inherited from parent page | - | {'parent_rule': 'bundle_date_field', 'parent_source': 'cms_field', 'd… |
| PASS | dates | pdf_start_date_equals_parent_resolved | - | 2026-09-18T00:00:00+00:00 | 2026-09-18T00:00:00+00:00 |
| PASS | dates | pdf_precision_equals_parent | - | day | day |
| PASS | dates | pdf_end_date_equals_parent | - | 2026-09-18T00:00:00+00:00 | 2026-09-18T00:00:00+00:00 |
| PASS | dates | date_source_is_parent_page | - | parent_page | parent_page |
| PASS | dates | parent_page_canonical_date_matches_parent_resolution | The page's own document and the value handed to its files agree. | 2026-09-18T00:00:00+00:00 | 2026-09-18T00:00:00+00:00 |
| PASS | dates | date_decision_row_presence | A row is written when the resolver produced a decision. | True | 1 |
| PASS | dates | date_decision_row_names_parent_node | - | c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1 | c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1 |
| PASS | dates | date_decision_current_date_is_parent_resolved | - | 2026-09-18T00:00:00+00:00 | 2026-09-18 00:00:00 |
| PASS | chunks | chunks_produced | - | >0 children | {'parents': 0, 'children': 3} |
| PASS | chunks | chunk_ids_unique | - | 3 | 3 |
| PASS | chunks | every_parent_reference_resolves | - | - | - |
| PASS | chunks | parents_have_at_least_two_children | A parent is emitted only when it groups more than one child. | - | - |
| PASS | chunks | children_carry_document_id | - | - | - |
| PASS | chunks | children_carry_doc_version | - | 1 | [1] |
| PASS | chunks | children_within_token_limit | child_max_tokens=512 | 512 | {'max_child_tokens': 282, 'over': []} |
| PASS | chunks | parents_within_token_limit | parent_max_tokens=100000 | 100000 | {'max_parent_tokens': 0, 'over': []} |
| PASS | chunks | content_hash_is_sha256_of_text | - | - | - |
| PASS | chunks | child_index_is_contiguous | - | [0, 1, 2] | [0, 1, 2] |
| PASS | chunks | children_carry_page_numbers | - | - | - |
| PASS | chunks | source_lines_covered_by_chunks | 100.000% of 73 source lines appear verbatim in some chunk | >=98% | {'coverage': 1.0, 'missing_sample': []} |
| PASS | chunks | children_have_embed_text_with_breadcrumb | embed_text = 'title › heading' + text | - | - |
| PASS | qdrant | point_count_equals_chunks | - | {'chunks': 3, 'indexer_reported': 3} | 3 |
| PASS | qdrant | every_chunk_id_is_a_point | - | 3 | {'missing': [], 'extra': []} |
| PASS | qdrant | payload_equals_chunk_payload_plus_stamps | Stored payload == Chunk.to_payload() plus created_at/updated_at/embed_model. | - | - |
| PASS | qdrant | children_have_real_vectors | - | - | - |
| PASS | qdrant | parents_have_zero_vectors | - | - | - |
| PASS | qdrant | vector_dimension_matches_setting | - | 3072 | [3072] |
| PASS | qdrant | children_stamped_with_embed_model | - | - | ['text-embedding-3-large:3072'] |
| PASS | qdrant | points_carry_document_id | - | - | - |
| PASS | qdrant | points_carry_effective_start_date | - | 2026-09-18T00:00:00+00:00 | ['2026-09-18T00:00:00+00:00'] |
| PASS | qdrant | points_carry_year_precision_only_when_year | - | day | ['None'] |
| PASS | qdrant | points_are_current_and_versioned | - | 1 | ['1'] |
| PASS | qdrant | points_stamped_with_pipeline_version | - | c1.i1.p2.e1 | ['c1.i1.p2.e1'] |
| PASS | qdrant | swap_deleted_with_keep_ids | delete_document(id, keep_ids=<new chunk ids>) ran after the upsert. | 3 | [{'document_id': '6658c489-9aad-483b-9004-27b62225425d', 'keep_ids': … |
| PASS | mysql | state_row_exists | - | - | - |
| PASS | mysql | row_source_type | - | pdf_attachment | pdf_attachment |
| PASS | mysql | row_source_key | - | https://teriin.org/sites/default/files/2026-09/Concept%20Note_WCEF_CP… | https://teriin.org/sites/default/files/2026-09/Concept%20Note_WCEF_CP… |
| PASS | mysql | row_fingerprint | - | 2026-09-08T06:49:49+00:00 | 2026-09-08T06:49:49+00:00 |
| PASS | mysql | row_content_hash | - | 2906b1d7628d9b151da8062e4db4fb8aa10cc500de6514187bb711fe69a62695 | 2906b1d7628d9b151da8062e4db4fb8aa10cc500de6514187bb711fe69a62695 |
| PASS | mysql | row_doc_version | - | 1 | 1 |
| PASS | mysql | row_pipeline_version | - | c1.i1.p2.e1 | c1.i1.p2.e1 |
| PASS | mysql | row_bundle | - | events | events |
| PASS | mysql | row_entity_type | - | - | - |
| PASS | mysql | row_changed_mark | - | 1788850189 | 1788850189 |
| PASS | mysql | row_title | - | Concept Note WCEF | Concept Note WCEF |
| PASS | mysql | row_url | - | https://teriin.org/event/wcef2026-accelerator-session-circular-public… | https://teriin.org/event/wcef2026-accelerator-session-circular-public… |
| PASS | mysql | row_effective_start_date | - | 2026-09-18T00:00:00+00:00 | 2026-09-18 00:00:00 |
| PASS | mysql | row_date_source | - | parent_page | parent_page |
| PASS | mysql | row_start_precision | - | day | day |
| PASS | mysql | row_effective_end_date | - | 2026-09-18T00:00:00+00:00 | 2026-09-18 00:00:00 |
| PASS | mysql | row_end_precision | - | day | day |
| PASS | mysql | row_indexed_at_set | - | not null | 2026-09-10 10:30:46 |
| PASS | mysql | row_raw_meta_equals_canonical | - | - | - |
| PASS | mysql | author_facet_rows | - | - | - |
| PASS | mysql | tag_facet_rows | - | ['Environmental governance', 'Government policy/regulations', 'Govern… | ['Environmental governance', 'Government policy/regulations', 'Govern… |
| PASS | mysql | theme_rows_match_classification | documents_theme = theme_taxonomy.classify(categories) | [('Resources & Sustainable Development', 'primary', None, 'main', 'Re… | [('Resources & Sustainable Development', 'primary', None, 'main', 'Re… |
| PASS | mysql | attachment_link_rows | - | - | - |
| PASS | mysql | ingest_log_indexed_row | - | 1 | ['indexed'] |
| PASS | mysql | ingest_log_chunk_count | - | 3 | 3 |
| PASS | mysql | ingest_log_hash_and_version | - | - | - |
| PASS | mysql | no_retry_marker | - | - | - |
| PASS | parent_child | linked_from_parent_in_attachment_table | - | c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1 | ['c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1'] |
| INFO | parent_child | parents_claiming_this_file | 1 page(s) link this file | - | ['c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1'] |
| INFO | knowledge | stage_report | status=ok {'chunks_seen': 3, 'chunks_cached': 0, 'mentions': 0, 'entities_auto': 0, 'enti… | - | {'document_id': '6658c489-9aad-483b-9004-27b62225425d', 'doc_version'… |
| PASS | knowledge | run_row_written | - | 1 | [(1, 'ok')] |
| PASS | knowledge | run_row_matches_report | - | {'status': 'ok', 'mentions': 0, 'claims_staged': 0} | {'status': 'ok', 'mentions': 0, 'claims_staged': 0} |
| PASS | knowledge | mention_rows_equal_report | - | 0 | 0 |
| PASS | knowledge | decision_rows_equal_report | - | 0 | 0 |
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
| N/A | knowledge | authored_points_at_this_document | The object identifies the document; the entity link is the claim's own provenance. | 6658c489-9aad-483b-9004-27b62225425d | - |
| N/A | knowledge | partner_of_points_at_an_organization | - | - | - |
| PASS | knowledge | authored_never_exceeds_its_source | An author claim per stated author at most; unresolved names are skipped, never invented. | <= 0 | 0 |
| PASS | knowledge | partner_of_never_exceeds_its_source | - | <= 0 | 0 |
| N/A | knowledge | authored_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | partner_of_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | no_duplicate_claim_identity | - | 0 | 0 |
| N/A | knowledge | claim_ids_are_unique | - | 0 | 0 |
| PASS | knowledge | extraction_cache_recorded | One cache row per child chunk content hash. | 3 | 3 |
| PASS | graph | nothing_projected_when_skipped | projection skipped: no touched claims | - | {'claims_in_graph': 0} |

