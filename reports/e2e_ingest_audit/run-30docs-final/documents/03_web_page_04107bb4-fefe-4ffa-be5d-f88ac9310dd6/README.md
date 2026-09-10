# 03_web_page_04107bb4-fefe-4ffa-be5d-f88ac9310dd6

**website** · bundle `page` · outcome **indexed**

- document_id: `04107bb4-fefe-4ffa-be5d-f88ac9310dd6`
- title: TERI Alumni Association
- source_key: https://teriin.org/alumni
- handled in 12.837s (build 0.0s, extract Nones, chunk 0.062s, embed+upsert 0.762s, knowledge 11.985s)

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
- JSON:API `node/page` uuid `04107bb4-fefe-4ffa-be5d-f88ac9310dd6` · nid 12393 · created 2025-09-30T04:28:20+00:00 · changed 2026-09-02T04:24:58+00:00
- change detection: status **new** · fingerprint `2026-09-02T04:24:58+00:00` · changed_mark 1788323098 · prior: False

### 2. Extraction
- HTML→text: body 24829 chars from 1 rich-text field(s); 3 PDF link(s) discovered (['inbody', 'inbody', 'inbody'])

### 3. Canonical document
- sections 1 · body chars 24829 · content_hash `2162448bb18fa41598c03bbd401c77911e50059d8329c4ead7b47c17e742cb82` · doc_version 1
- authors [] · tags [] · categories []
- entity_refs 0 · file_links [('inbody:8a64f693f3de31c6620aa873d1d4c742561566cb', 'inbody'), ('inbody:019c7e59163edfbb3535b05744ff6949b04cef47', 'inbody'), ('inbody:0a7c343146247d17ae9ebdffa512fb4a3b39a5b2', 'inbody')]
- extra {'bundle': 'page', 'nid': 12393, 'changed': '2026-09-02T04:24:58+00:00'}

### 4. Dates
- effective_start_date **2025-09-30T04:28:20+00:00** (precision day, source created) · effective_end_date None (precision None)
- rule `bundle_created` · source `created` · fields ['created'] · raw values [] · role created_stamp · range_issue None
- date_decision rows in MySQL: 0

### 5. Chunks
- 6 parent(s), 23 child(ren) · child tokens min/avg/max 182/297/410 · section types ['None', 'references']

### 6. Qdrant
- collection `e2e_audit_documents` · points for this document: 29 · indexer reported 29
- delete_document calls: [{'document_id': '04107bb4-fefe-4ffa-be5d-f88ac9310dd6', 'keep_ids': 29}]

### 7. MySQL
- `e2e_audit_documents`: 1 row(s)
- `e2e_audit_documents_author`: 0 row(s)
- `e2e_audit_documents_tag`: 0 row(s)
- `e2e_audit_documents_theme`: 0 row(s)
- `e2e_audit_documents_retry`: 0 row(s)
- `e2e_audit_documents_dead_link`: 0 row(s)
- `e2e_audit_documents_date_decision`: 0 row(s)
- `e2e_audit_documents_entity_mention`: 104 row(s)
- `e2e_audit_documents_assertion`: 0 row(s)
- `e2e_audit_documents_assertion_rejection`: 1 row(s)
- `e2e_audit_documents_predicate_candidate`: 0 row(s)
- `e2e_audit_documents_knowledge_run`: 1 row(s)
- `e2e_audit_documents_attachment`: 3 row(s)
- `e2e_audit_documents_attachment (as file)`: 0 row(s)
- `e2e_audit_documents_entity_resolution_decision`: 104 row(s)
- `e2e_audit_documents_entity_extraction`: 23 row(s)
- `e2e_audit_documents_assertion_link`: 0 row(s)
- `e2e_audit_ingest_log`: 1 row(s)
- `e2e_audit_documents_entity (referenced)`: 1 row(s)

### 8. Knowledge stage
- status **ok** in 11.98s · knowledge_version `kv1:9f993bff646d51c0`
- counts {'chunks_seen': 23, 'chunks_cached': 0, 'mentions': 104, 'entities_auto': 2, 'entities_provisional': 0, 'entities_ambiguous': 20, 'entities_unresolved': 82, 'claims_built': 1, 'claims_staged': 0, 'claims_rejected': 1, 'claims_retracted': 0, 'pending_predicates': 0, 'conflicts_disputed': 0, 'conflicts_superseded': 0}
- projection {'status': 'skipped', 'version': None, 'edges': 0}
  - stage `prelude`: ran · counts {'entities': 2451, 'chunks': 23} · notes [] · errors []
  - stage `supersede`: skipped · counts {} · notes [] · errors []
  - stage `mentions`: ran · counts {'cached': 0, 'mentions': 104} · notes [] · errors []
  - stage `resolution`: ran · counts {'auto': 2, 'provisional': 0, 'ambiguous': 20, 'unresolved': 82, 'cache_recorded': 23} · notes [] · errors []
  - stage `claims`: ran · counts {'llm_calls': 2, 'llm_failures': 0, 'run_calls_used': 2, 'run_calls_remaining': 149998, 'llm': 1, 'unknown_predicates': 0, 'built': 1} · notes [] · errors []
  - stage `validate`: ran · counts {'accepted': 0, 'rejected': 1, 'rejected_missing_object_entity': 1} · notes [] · errors []
  - stage `persist`: ran · counts {'accepted': 0, 'pending_predicates': 0, 'rejections_recorded': 1} · notes [] · errors []
  - stage `conflicts`: skipped · counts {} · notes [] · errors []
  - stage `project`: skipped · counts {} · notes [] · errors []

### 9. Neo4j
- Document node: None · chunk stubs: 0 · Claim nodes: 0 · current-state edges from this document's claims: 0

## Checks

**82 pass · 0 fail · 10 n/a · 7 info**

| status | category | check | detail | expected | actual |
| --- | --- | --- | --- | --- | --- |
| PASS | outcome | handler_outcome_is_indexed | Every fresh document is expected to end 'indexed'. | indexed | indexed |
| PASS | outcome | no_exception | - | - | - |
| PASS | canonical | title_equals_node_title | - | TERI Alumni Association | TERI Alumni Association |
| PASS | canonical | body_is_single_section | Website body becomes one section. | 24829 | 24829 |
| PASS | canonical | raw_meta_equals_record_metadata | - | - | - |
| PASS | canonical | article_uuid_is_node_uuid | - | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 |
| PASS | canonical | file_links_equal_record_files | - | ['inbody:8a64f693f3de31c6620aa873d1d4c742561566cb', 'inbody:019c7e591… | ['inbody:8a64f693f3de31c6620aa873d1d4c742561566cb', 'inbody:019c7e591… |
| PASS | canonical | extra_carries_bundle_nid_changed | - | - | {'bundle': 'page', 'nid': 12393, 'changed': '2026-09-02T04:24:58+00:0… |
| PASS | canonical | facets_match_drupal_facets_rule | - | {'categories': [], 'tags': [], 'authors': []} | {'categories': [], 'tags': [], 'authors': []} |
| PASS | canonical | content_hash_is_sha256_of_body_text | - | - | 2162448bb18fa41598c03bbd401c77911e50059d8329c4ead7b47c17e742cb82 |
| PASS | canonical | body_text_non_empty | - | >0 chars | 24829 |
| PASS | dates | effective_start_date_present | Undated documents are excluded from date filters (pipeline warns). | a date | 2025-09-30T04:28:20+00:00 |
| PASS | dates | effective_date_evidence_present | - | - | - |
| INFO | dates | website_date_rule | rule=bundle_created source=created fields=['created'] | - | {'rule': 'bundle_created', 'source': 'created', 'fields': ['created']… |
| PASS | dates | canonical_date_equals_evidence_value | - | 2025-09-30T04:28:20+00:00 | 2025-09-30T04:28:20+00:00 |
| PASS | dates | canonical_end_equals_evidence_end | - | - | - |
| PASS | dates | date_source_equals_evidence_source | - | created | created |
| PASS | dates | date_decision_row_presence | Written only when the bundle maps to a real CMS date field. | False | 0 |
| PASS | chunks | chunks_produced | - | >0 children | {'parents': 6, 'children': 23} |
| PASS | chunks | chunk_ids_unique | - | 29 | 29 |
| PASS | chunks | every_parent_reference_resolves | - | - | - |
| PASS | chunks | parents_have_at_least_two_children | A parent is emitted only when it groups more than one child. | ['0576288d-6c06-54e5-b2c4-0e4bc7e4f853', '27c86bd0-e595-5434-bb53-2f7… | {'27c86bd0-e595-5434-bb53-2f70d1b95bce': 4, '5d821c72-ff95-54a9-baa3-… |
| PASS | chunks | children_carry_document_id | - | - | - |
| PASS | chunks | children_carry_doc_version | - | 1 | [1] |
| PASS | chunks | children_within_token_limit | child_max_tokens=480 | 480 | {'max_child_tokens': 410, 'over': []} |
| PASS | chunks | parents_within_token_limit | parent_max_tokens=2200 | 2200 | {'max_parent_tokens': 1097, 'over': []} |
| PASS | chunks | content_hash_is_sha256_of_text | - | - | - |
| PASS | chunks | child_index_is_contiguous | - | [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19… | [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19… |
| PASS | chunks | source_lines_covered_by_chunks | 100.000% of 181 source lines appear verbatim in some chunk | >=98% | {'coverage': 1.0, 'missing_sample': []} |
| PASS | chunks | children_have_embed_text_with_breadcrumb | embed_text = 'title › heading' + text | - | - |
| PASS | qdrant | point_count_equals_chunks | - | {'chunks': 29, 'indexer_reported': 29} | 29 |
| PASS | qdrant | every_chunk_id_is_a_point | - | 29 | {'missing': [], 'extra': []} |
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
| PASS | qdrant | swap_deleted_with_keep_ids | delete_document(id, keep_ids=<new chunk ids>) ran after the upsert. | 29 | [{'document_id': '04107bb4-fefe-4ffa-be5d-f88ac9310dd6', 'keep_ids': … |
| PASS | mysql | state_row_exists | - | - | - |
| PASS | mysql | row_source_type | - | website | website |
| PASS | mysql | row_source_key | - | https://teriin.org/alumni | https://teriin.org/alumni |
| PASS | mysql | row_fingerprint | - | 2026-09-02T04:24:58+00:00 | 2026-09-02T04:24:58+00:00 |
| PASS | mysql | row_content_hash | - | 2162448bb18fa41598c03bbd401c77911e50059d8329c4ead7b47c17e742cb82 | 2162448bb18fa41598c03bbd401c77911e50059d8329c4ead7b47c17e742cb82 |
| PASS | mysql | row_doc_version | - | 1 | 1 |
| PASS | mysql | row_pipeline_version | - | c1.i1.p2.e1 | c1.i1.p2.e1 |
| PASS | mysql | row_bundle | - | page | page |
| PASS | mysql | row_entity_type | - | node | node |
| PASS | mysql | row_changed_mark | - | 1788323098 | 1788323098 |
| PASS | mysql | row_title | - | TERI Alumni Association | TERI Alumni Association |
| PASS | mysql | row_url | - | https://teriin.org/alumni | https://teriin.org/alumni |
| PASS | mysql | row_effective_start_date | - | 2025-09-30T04:28:20+00:00 | 2025-09-30 04:28:20 |
| PASS | mysql | row_date_source | - | created | created |
| PASS | mysql | row_start_precision | - | day | day |
| PASS | mysql | row_effective_end_date | - | - | - |
| PASS | mysql | row_end_precision | - | - | - |
| PASS | mysql | row_indexed_at_set | - | not null | 2026-09-10 10:25:41 |
| PASS | mysql | row_raw_meta_equals_canonical | - | - | - |
| PASS | mysql | author_facet_rows | - | - | - |
| PASS | mysql | tag_facet_rows | - | - | - |
| PASS | mysql | theme_rows_match_classification | documents_theme = theme_taxonomy.classify(categories) | - | - |
| PASS | mysql | attachment_link_rows | - | [('inbody:019c7e59163edfbb3535b05744ff6949b04cef47', 'inbody'), ('inb… | [('inbody:019c7e59163edfbb3535b05744ff6949b04cef47', 'inbody'), ('inb… |
| PASS | mysql | ingest_log_indexed_row | - | 1 | ['indexed'] |
| PASS | mysql | ingest_log_chunk_count | - | 29 | 29 |
| PASS | mysql | ingest_log_hash_and_version | - | - | - |
| PASS | mysql | no_retry_marker | - | - | - |
| INFO | knowledge | stage_report | status=ok {'chunks_seen': 23, 'chunks_cached': 0, 'mentions': 104, 'entities_auto': 2, 'e… | - | {'document_id': '04107bb4-fefe-4ffa-be5d-f88ac9310dd6', 'doc_version'… |
| PASS | knowledge | run_row_written | - | 1 | [(1, 'ok')] |
| PASS | knowledge | run_row_matches_report | - | {'status': 'ok', 'mentions': 104, 'claims_staged': 0} | {'status': 'ok', 'mentions': 104, 'claims_staged': 0} |
| PASS | knowledge | mention_rows_equal_report | - | 104 | 104 |
| PASS | knowledge | decision_rows_equal_report | - | 104 | 104 |
| PASS | knowledge | mentions_point_at_own_chunks | - | - | - |
| PASS | knowledge | staged_claim_rows_equal_report | - | 0 | 0 |
| PASS | knowledge | claims_cite_own_chunks_or_document | - | - | - |
| PASS | knowledge | rejection_rows_equal_report | - | 1 | 1 |
| INFO | knowledge | claims_by_predicate | {} | - | - |
| INFO | knowledge | claims_by_method | {} | - | - |
| INFO | knowledge | cms_author_values | 0 author value(s) in metadata | - | - |
| INFO | knowledge | cms_partner_values | 0 partner value(s) in metadata | - | - |
| N/A | knowledge | authored_is_cms_only | AUTHORED must never come from the LLM. | cms_field | - |
| N/A | knowledge | partner_of_is_cms_only | PARTNER_OF must never come from the LLM. | cms_field | - |
| N/A | knowledge | authored_records_its_source_field | - | ['field_authors', 'field_rpaper_author', 'field_article_authors', 'fi… | - |
| N/A | knowledge | partner_of_records_its_source_field | - | ['field_completed_partners', 'field_ongoing_partners'] | - |
| N/A | knowledge | authored_points_at_this_document | The object identifies the document; the entity link is the claim's own provenance. | 04107bb4-fefe-4ffa-be5d-f88ac9310dd6 | - |
| N/A | knowledge | partner_of_points_at_an_organization | - | - | - |
| PASS | knowledge | authored_never_exceeds_its_source | An author claim per stated author at most; unresolved names are skipped, never invented. | <= 0 | 0 |
| PASS | knowledge | partner_of_never_exceeds_its_source | - | <= 0 | 0 |
| N/A | knowledge | authored_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | partner_of_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | no_duplicate_claim_identity | - | 0 | 0 |
| N/A | knowledge | claim_ids_are_unique | - | 0 | 0 |
| INFO | knowledge | rejections_by_reason | {'missing_object_entity': 1} | - | {'missing_object_entity': 1} |
| PASS | knowledge | rejected_claims_are_not_staged | Recorded for visibility; a subject may legitimately carry one accepted and one rejected c… | - | - |
| PASS | knowledge | extraction_cache_recorded | One cache row per child chunk content hash. | 23 | 23 |
| PASS | graph | nothing_projected_when_skipped | projection skipped: no touched claims | - | {'claims_in_graph': 0} |

