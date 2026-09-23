# 05_pdf_page_inbody_019c7e59163edfbb3535b05744ff6949b

**pdf_attachment** · bundle `page` · outcome **indexed**

- document_id: `inbody:019c7e59163edfbb3535b05744ff6949b04cef47`
- title: TERI Alumni Association
- source_key: https://www.teriin.org/sites/default/files/files/Book-on-Dr-RK-Pachauri.pdf
- parent page: `04107bb4-fefe-4ffa-be5d-f88ac9310dd6` — TERI Alumni Association (https://teriin.org/alumni)
- file: Book-on-Dr-RK-Pachauri.pdf · origin `inbody` · description -
- handled in 178.77s (build 27.697s, extract 26.982s, chunk 2.433s, embed+upsert 68.316s, knowledge 80.287s)

## Files in this folder

- `00_source/parent_record.json` — raw JSON:API resource of the parent page (plus included entities)
- `00_source/file_link.json` — the file reference as discovered on the page, and the download result
- `00_source/Book-on-Dr-RK-Pachauri.pdf` — the freshly downloaded PDF bytes
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
- downloaded 2825799 bytes from `https://www.teriin.org/sites/default/files/files/Book-on-Dr-RK-Pachauri.pdf` (requested `https://www.teriin.org/sites/default/files/files/Book-on-Dr-RK-Pachauri.pdf`, https upgrade: False) · sha256 `800f36ac4ebc76c377475a3444203ad59d24588b814dadc3ae1c2fd82163596a`
- change detection: status **new** · fingerprint `inbody:019c7e59163edfbb3535b05744ff6949b04cef47` · changed_mark 1788323098 · prior: False

### 2. Extraction
- 248 page(s) · routes {'ocr': 5, 'text': 240, 'empty': 3} · tables 0 · PDF metadata keys ['extraction_mode', 'page_signals', 'route']
- characters: 617552 · empty pages: 3

### 3. Canonical document
- sections 245 · body chars 618040 · content_hash `f714881177174c84a61bdbadca629d05ea175b51ec734fec84b7bf3836045a4c` · doc_version 1
- authors [] · tags [] · categories []
- entity_refs 0 · file_links []
- extra {'bundle': 'page'}

### 4. Dates
- effective_start_date **2020-01-01T00:00:00+00:00** (precision year, source document_copyright) · effective_end_date None (precision None)
- parent page resolution: rule `bundle_created` source `created` fields ['created'] raw []
- file resolver: overridden=True · decision action `propose_override` rule `copyright_statement_corroborated` · edition_label None · evidence used ['drupal', 'pdf_meta', 'pdf_text']
- decision evidence: The document's front matter states 'Ⓒ \ue064\ue055\ue062\ue059 Alumni Association 2020' and its DocInfo creation date is 2020-09-21T17:51:50+00:00; both name 2020, which differs from the page's 2025-09-30. Year precision: 1 January is a marker, not a day.
- date_decision rows in MySQL: 1 — action `propose_override` rule `copyright_statement_corroborated` candidate 2020-01-01 00:00:00

### 5. Chunks
- 98 parent(s), 431 child(ren) · child tokens min/avg/max 157/425/559 · section types ['None', 'references']

### 6. Qdrant
- collection `e2e_audit_documents` · points for this document: 529 · indexer reported 529
- delete_document calls: [{'document_id': 'inbody:019c7e59163edfbb3535b05744ff6949b04cef47', 'keep_ids': 529}]

### 7. MySQL
- `e2e_audit_documents`: 1 row(s)
- `e2e_audit_documents_author`: 0 row(s)
- `e2e_audit_documents_tag`: 0 row(s)
- `e2e_audit_documents_theme`: 0 row(s)
- `e2e_audit_documents_retry`: 0 row(s)
- `e2e_audit_documents_dead_link`: 0 row(s)
- `e2e_audit_documents_date_decision`: 1 row(s)
- `e2e_audit_documents_entity_mention`: 1673 row(s)
- `e2e_audit_documents_assertion`: 0 row(s)
- `e2e_audit_documents_assertion_rejection`: 15 row(s)
- `e2e_audit_documents_predicate_candidate`: 0 row(s)
- `e2e_audit_documents_knowledge_run`: 1 row(s)
- `e2e_audit_documents_attachment`: 0 row(s)
- `e2e_audit_documents_attachment (as file)`: 1 row(s)
- `e2e_audit_documents_entity_resolution_decision`: 1673 row(s)
- `e2e_audit_documents_entity_extraction`: 431 row(s)
- `e2e_audit_documents_assertion_link`: 0 row(s)
- `e2e_audit_ingest_log`: 1 row(s)
- `e2e_audit_documents_entity (referenced)`: 7 row(s)

### 8. Knowledge stage
- status **ok** in 80.28s · knowledge_version `kv1:9f993bff646d51c0`
- counts {'chunks_seen': 431, 'chunks_cached': 0, 'mentions': 1673, 'entities_auto': 29, 'entities_provisional': 0, 'entities_ambiguous': 50, 'entities_unresolved': 1594, 'claims_built': 15, 'claims_staged': 0, 'claims_rejected': 15, 'claims_retracted': 0, 'pending_predicates': 0, 'conflicts_disputed': 0, 'conflicts_superseded': 0}
- projection {'status': 'skipped', 'version': None, 'edges': 0}
  - stage `prelude`: ran · counts {'entities': 2451, 'chunks': 431} · notes [] · errors []
  - stage `supersede`: skipped · counts {} · notes [] · errors []
  - stage `mentions`: ran · counts {'cached': 0, 'mentions': 1673} · notes [] · errors []
  - stage `resolution`: ran · counts {'auto': 29, 'provisional': 0, 'ambiguous': 50, 'unresolved': 1594, 'cache_recorded': 431} · notes [] · errors []
  - stage `claims`: ran · counts {'llm_calls': 26, 'llm_failures': 0, 'run_calls_used': 28, 'run_calls_remaining': 149972, 'llm': 15, 'unknown_predicates': 0, 'built': 15} · notes [] · errors []
  - stage `validate`: ran · counts {'accepted': 0, 'rejected': 15, 'rejected_missing_object_entity': 15} · notes [] · errors []
  - stage `persist`: ran · counts {'accepted': 0, 'pending_predicates': 0, 'rejections_recorded': 15} · notes [] · errors []
  - stage `conflicts`: skipped · counts {} · notes [] · errors []
  - stage `project`: skipped · counts {} · notes [] · errors []

### 9. Neo4j
- Document node: None · chunk stubs: 0 · Claim nodes: 0 · current-state edges from this document's claims: 0

## Checks

**91 pass · 0 fail · 10 n/a · 9 info**

| status | category | check | detail | expected | actual |
| --- | --- | --- | --- | --- | --- |
| PASS | outcome | handler_outcome_is_indexed | Every fresh document is expected to end 'indexed'. | indexed | indexed |
| PASS | outcome | no_exception | - | - | - |
| PASS | source | pdf_downloaded_non_empty | - | >0 bytes | 2825799 |
| INFO | source | pdf_pages | 248 pages extracted, 245 with text | - | {'pages': 248, 'with_text': 245, 'routes': {'ocr': 5, 'text': 240, 'e… |
| PASS | canonical | sections_equal_non_empty_pages | from_pdf makes one section per page with text. | 245 | 245 |
| PASS | canonical | section_text_equals_page_text | Section text is the page text verbatim. | - | - |
| PASS | canonical | title_precedence_description_node_filename | title = file.description or node.title or filename | TERI Alumni Association | TERI Alumni Association |
| PASS | canonical | linked_article_uuid_is_parent_node | - | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 |
| PASS | canonical | source_url_is_parent_page | - | https://teriin.org/alumni | https://teriin.org/alumni |
| PASS | canonical | file_url_is_fetched_url | - | https://www.teriin.org/sites/default/files/files/Book-on-Dr-RK-Pachau… | https://www.teriin.org/sites/default/files/files/Book-on-Dr-RK-Pachau… |
| PASS | canonical | bundle_inherited_from_parent | - | page | page |
| PASS | parent_child | facets_inherited_from_parent | categories/tags/authors equal the parent page's | {'categories': [], 'tags': [], 'authors': []} | {'categories': [], 'tags': [], 'authors': []} |
| PASS | parent_child | entity_refs_inherited_from_parent | - | - | - |
| PASS | parent_child | parent_lists_this_file | - | inbody:019c7e59163edfbb3535b05744ff6949b04cef47 | ['inbody:8a64f693f3de31c6620aa873d1d4c742561566cb', 'inbody:019c7e591… |
| PASS | canonical | content_hash_is_sha256_of_body_text | - | - | f714881177174c84a61bdbadca629d05ea175b51ec734fec84b7bf3836045a4c |
| PASS | canonical | body_text_non_empty | - | >0 chars | 618040 |
| PASS | dates | effective_start_date_present | Undated documents are excluded from date filters (pipeline warns). | a date | 2020-01-01T00:00:00+00:00 |
| PASS | dates | resolver_ran | - | - | - |
| INFO | dates | pdf_date_path | document_copyright override | - | {'parent_rule': 'bundle_created', 'parent_source': 'created', 'decisi… |
| PASS | dates | override_start_equals_resolver | - | 2020-01-01T00:00:00+00:00 | 2020-01-01T00:00:00+00:00 |
| PASS | dates | date_source_matches_resolver_canonical_source | The persisted source is the one the resolver derived. | document_copyright | document_copyright |
| PASS | dates | date_source_is_document_derived | An overridden PDF date came from the document itself. | document_text\\|document_copyright | document_copyright |
| PASS | dates | parent_page_canonical_date_matches_parent_resolution | The page's own document and the value handed to its files agree. | 2025-09-30T04:28:20+00:00 | 2025-09-30T04:28:20+00:00 |
| PASS | dates | date_decision_row_presence | A row is written when the resolver produced a decision. | True | 1 |
| PASS | dates | date_decision_row_names_parent_node | - | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 |
| PASS | dates | date_decision_current_date_is_parent_resolved | - | 2025-09-30T04:28:20+00:00 | 2025-09-30 04:28:20 |
| PASS | chunks | chunks_produced | - | >0 children | {'parents': 98, 'children': 431} |
| PASS | chunks | chunk_ids_unique | - | 529 | 529 |
| PASS | chunks | every_parent_reference_resolves | - | - | - |
| PASS | chunks | parents_have_at_least_two_children | A parent is emitted only when it groups more than one child. | ['0026771f-9f71-5eaf-84d5-478af12b453e', '01682bc8-df9d-5538-96c3-453… | {'259828b7-7227-540f-a329-16ff40919e77': 2, '77bd2beb-6851-55d4-81e0-… |
| PASS | chunks | children_carry_document_id | - | - | - |
| PASS | chunks | children_carry_doc_version | - | 1 | [1] |
| PASS | chunks | children_within_token_limit | child_max_tokens=560 | 560 | {'max_child_tokens': 559, 'over': []} |
| PASS | chunks | parents_within_token_limit | parent_max_tokens=2600 | 2600 | {'max_parent_tokens': 2101, 'over': []} |
| PASS | chunks | content_hash_is_sha256_of_text | - | - | - |
| PASS | chunks | child_index_is_contiguous | - | [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19… | [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19… |
| PASS | chunks | children_carry_page_numbers | - | - | - |
| PASS | chunks | source_lines_covered_by_chunks | 100.000% of 9703 source lines appear verbatim in some chunk | >=98% | {'coverage': 1.0, 'missing_sample': []} |
| PASS | chunks | children_have_embed_text_with_breadcrumb | embed_text = 'title › heading' + text | - | - |
| PASS | qdrant | point_count_equals_chunks | - | {'chunks': 529, 'indexer_reported': 529} | 529 |
| PASS | qdrant | every_chunk_id_is_a_point | - | 529 | {'missing': [], 'extra': []} |
| PASS | qdrant | payload_equals_chunk_payload_plus_stamps | Stored payload == Chunk.to_payload() plus created_at/updated_at/embed_model. | - | - |
| PASS | qdrant | children_have_real_vectors | - | - | - |
| PASS | qdrant | parents_have_zero_vectors | - | - | - |
| PASS | qdrant | vector_dimension_matches_setting | - | 3072 | [3072] |
| PASS | qdrant | children_stamped_with_embed_model | - | - | ['text-embedding-3-large:3072'] |
| PASS | qdrant | points_carry_document_id | - | - | - |
| PASS | qdrant | points_carry_effective_start_date | - | 2020-01-01T00:00:00+00:00 | ['2020-01-01T00:00:00+00:00'] |
| PASS | qdrant | points_carry_year_precision_only_when_year | - | year | ['year'] |
| PASS | qdrant | points_are_current_and_versioned | - | 1 | ['1'] |
| PASS | qdrant | points_stamped_with_pipeline_version | - | c1.i1.p2.e1 | ['c1.i1.p2.e1'] |
| PASS | qdrant | swap_deleted_with_keep_ids | delete_document(id, keep_ids=<new chunk ids>) ran after the upsert. | 529 | [{'document_id': 'inbody:019c7e59163edfbb3535b05744ff6949b04cef47', '… |
| PASS | mysql | state_row_exists | - | - | - |
| PASS | mysql | row_source_type | - | pdf_attachment | pdf_attachment |
| PASS | mysql | row_source_key | - | https://www.teriin.org/sites/default/files/files/Book-on-Dr-RK-Pachau… | https://www.teriin.org/sites/default/files/files/Book-on-Dr-RK-Pachau… |
| PASS | mysql | row_fingerprint | - | inbody:019c7e59163edfbb3535b05744ff6949b04cef47 | inbody:019c7e59163edfbb3535b05744ff6949b04cef47 |
| PASS | mysql | row_content_hash | - | f714881177174c84a61bdbadca629d05ea175b51ec734fec84b7bf3836045a4c | f714881177174c84a61bdbadca629d05ea175b51ec734fec84b7bf3836045a4c |
| PASS | mysql | row_doc_version | - | 1 | 1 |
| PASS | mysql | row_pipeline_version | - | c1.i1.p2.e1 | c1.i1.p2.e1 |
| PASS | mysql | row_bundle | - | page | page |
| PASS | mysql | row_entity_type | - | - | - |
| PASS | mysql | row_changed_mark | - | 1788323098 | 1788323098 |
| PASS | mysql | row_title | - | TERI Alumni Association | TERI Alumni Association |
| PASS | mysql | row_url | - | https://teriin.org/alumni | https://teriin.org/alumni |
| PASS | mysql | row_effective_start_date | - | 2020-01-01T00:00:00+00:00 | 2020-01-01 00:00:00 |
| PASS | mysql | row_date_source | - | document_copyright | document_copyright |
| PASS | mysql | row_start_precision | - | year | year |
| PASS | mysql | row_effective_end_date | - | - | - |
| PASS | mysql | row_end_precision | - | - | - |
| PASS | mysql | row_indexed_at_set | - | not null | 2026-09-10 10:27:40 |
| PASS | mysql | row_raw_meta_equals_canonical | - | - | - |
| PASS | mysql | author_facet_rows | - | - | - |
| PASS | mysql | tag_facet_rows | - | - | - |
| PASS | mysql | theme_rows_match_classification | documents_theme = theme_taxonomy.classify(categories) | - | - |
| PASS | mysql | attachment_link_rows | - | - | - |
| PASS | mysql | ingest_log_indexed_row | - | 1 | ['indexed'] |
| PASS | mysql | ingest_log_chunk_count | - | 529 | 529 |
| PASS | mysql | ingest_log_hash_and_version | - | - | - |
| PASS | mysql | no_retry_marker | - | - | - |
| PASS | parent_child | linked_from_parent_in_attachment_table | - | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 | ['04107bb4-fefe-4ffa-be5d-f88ac9310dd6'] |
| INFO | parent_child | parents_claiming_this_file | 1 page(s) link this file | - | ['04107bb4-fefe-4ffa-be5d-f88ac9310dd6'] |
| INFO | knowledge | stage_report | status=ok {'chunks_seen': 431, 'chunks_cached': 0, 'mentions': 1673, 'entities_auto': 29,… | - | {'document_id': 'inbody:019c7e59163edfbb3535b05744ff6949b04cef47', 'd… |
| PASS | knowledge | run_row_written | - | 1 | [(1, 'ok')] |
| PASS | knowledge | run_row_matches_report | - | {'status': 'ok', 'mentions': 1673, 'claims_staged': 0} | {'status': 'ok', 'mentions': 1673, 'claims_staged': 0} |
| PASS | knowledge | mention_rows_equal_report | - | 1673 | 1673 |
| PASS | knowledge | decision_rows_equal_report | - | 1673 | 1673 |
| PASS | knowledge | mentions_point_at_own_chunks | - | - | - |
| PASS | knowledge | staged_claim_rows_equal_report | - | 0 | 0 |
| PASS | knowledge | claims_cite_own_chunks_or_document | - | - | - |
| PASS | knowledge | rejection_rows_equal_report | - | 15 | 15 |
| INFO | knowledge | claims_by_predicate | {} | - | - |
| INFO | knowledge | claims_by_method | {} | - | - |
| INFO | knowledge | cms_author_values | 0 author value(s) in metadata | - | - |
| INFO | knowledge | cms_partner_values | 0 partner value(s) in metadata | - | - |
| N/A | knowledge | authored_is_cms_only | AUTHORED must never come from the LLM. | cms_field | - |
| N/A | knowledge | partner_of_is_cms_only | PARTNER_OF must never come from the LLM. | cms_field | - |
| N/A | knowledge | authored_records_its_source_field | - | ['field_authors', 'field_rpaper_author', 'field_article_authors', 'fi… | - |
| N/A | knowledge | partner_of_records_its_source_field | - | ['field_completed_partners', 'field_ongoing_partners'] | - |
| N/A | knowledge | authored_points_at_this_document | The object identifies the document; the entity link is the claim's own provenance. | inbody:019c7e59163edfbb3535b05744ff6949b04cef47 | - |
| N/A | knowledge | partner_of_points_at_an_organization | - | - | - |
| PASS | knowledge | authored_never_exceeds_its_source | An author claim per stated author at most; unresolved names are skipped, never invented. | <= 0 | 0 |
| PASS | knowledge | partner_of_never_exceeds_its_source | - | <= 0 | 0 |
| N/A | knowledge | authored_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | partner_of_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | no_duplicate_claim_identity | - | 0 | 0 |
| N/A | knowledge | claim_ids_are_unique | - | 0 | 0 |
| INFO | knowledge | rejections_by_reason | {'missing_object_entity': 15} | - | {'missing_object_entity': 15} |
| PASS | knowledge | rejected_claims_are_not_staged | Recorded for visibility; a subject may legitimately carry one accepted and one rejected c… | - | - |
| PASS | knowledge | extraction_cache_recorded | One cache row per child chunk content hash. | 431 | 431 |
| PASS | graph | nothing_projected_when_skipped | projection skipped: no touched claims | - | {'claims_in_graph': 0} |

