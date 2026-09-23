# 23_web_events_c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1

**website** · bundle `events` · outcome **indexed**

- document_id: `c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1`
- title: WCEF2026 Accelerator Session: Circular Public Procurement for Sustainable Consumption and Production
- source_key: https://teriin.org/event/wcef2026-accelerator-session-circular-public-procurement-and-ecolabels
- handled in 0.709s (build 0.0s, extract Nones, chunk 0.005s, embed+upsert 0.658s, knowledge 0.014s)

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
- JSON:API `node/events` uuid `c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1` · nid 13332 · created 2026-08-14T07:25:38+00:00 · changed 2026-09-08T06:49:49+00:00
- change detection: status **new** · fingerprint `2026-09-08T06:49:49+00:00` · changed_mark 1788850189 · prior: False

### 2. Extraction
- HTML→text: body 3330 chars from 1 rich-text field(s); 1 PDF link(s) discovered (['attachment'])

### 3. Canonical document
- sections 1 · body chars 3330 · content_hash `7e696dba9705aeac74c947df232fd242d5b579c42562a401a45144e88b9bce14` · doc_version 1
- authors [] · tags ['Environmental governance', 'Government policy/regulations', 'Government schemes', 'Sustainable development', 'Sustainable Development Goals', 'Sustainable public procurement'] · categories ['Resources & Sustainable Development']
- entity_refs 12 · file_links [('6658c489-9aad-483b-9004-27b62225425d', 'attachment')]
- extra {'bundle': 'events', 'nid': 13332, 'changed': '2026-09-08T06:49:49+00:00'}

### 4. Dates
- effective_start_date **2026-09-18T00:00:00+00:00** (precision day, source cms_field) · effective_end_date 2026-09-18T00:00:00+00:00 (precision day)
- rule `bundle_date_field` · source `cms_field` · fields ['field_event_start_date', 'field_event_end_date'] · raw values ['2026-09-18T08:30:00+00:00', '2026-09-18T10:00:00+00:00'] · role range_start · range_issue None
- date_decision rows in MySQL: 1 — action `propose_override` rule `bundle_date_field` candidate 2026-09-18 00:00:00

### 5. Chunks
- 0 parent(s), 2 child(ren) · child tokens min/avg/max 250/277/305 · section types ['None']

### 6. Qdrant
- collection `e2e_audit_documents` · points for this document: 2 · indexer reported 2
- delete_document calls: [{'document_id': 'c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1', 'keep_ids': 2}]

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
- `e2e_audit_documents_attachment`: 1 row(s)
- `e2e_audit_documents_attachment (as file)`: 0 row(s)
- `e2e_audit_documents_entity_resolution_decision`: 0 row(s)
- `e2e_audit_documents_entity_extraction`: 2 row(s)
- `e2e_audit_documents_assertion_link`: 0 row(s)
- `e2e_audit_ingest_log`: 1 row(s)
- `e2e_audit_documents_entity (referenced)`: 0 row(s)

### 8. Knowledge stage
- status **ok** in 0.01s · knowledge_version `kv1:9f993bff646d51c0`
- counts {'chunks_seen': 2, 'chunks_cached': 0, 'mentions': 0, 'entities_auto': 0, 'entities_provisional': 0, 'entities_ambiguous': 0, 'entities_unresolved': 0, 'claims_built': 0, 'claims_staged': 0, 'claims_rejected': 0, 'claims_retracted': 0, 'pending_predicates': 0, 'conflicts_disputed': 0, 'conflicts_superseded': 0}
- projection {'status': 'skipped', 'version': None, 'edges': 0}
  - stage `prelude`: ran · counts {'entities': 2451, 'chunks': 2} · notes [] · errors []
  - stage `supersede`: skipped · counts {} · notes [] · errors []
  - stage `mentions`: ran · counts {'cached': 0, 'mentions': 0} · notes [] · errors []
  - stage `resolution`: ran · counts {'auto': 0, 'provisional': 0, 'ambiguous': 0, 'unresolved': 0, 'cache_recorded': 2} · notes [] · errors []
  - stage `claims`: ran · counts {'llm_calls': 0, 'llm_failures': 0, 'run_calls_used': 44, 'run_calls_remaining': 149956, 'llm': 0, 'unknown_predicates': 0, 'built': 0} · notes [] · errors []
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
| PASS | canonical | title_equals_node_title | - | WCEF2026 Accelerator Session: Circular Public Procurement for Sustain… | WCEF2026 Accelerator Session: Circular Public Procurement for Sustain… |
| PASS | canonical | body_is_single_section | Website body becomes one section. | 3330 | 3330 |
| PASS | canonical | raw_meta_equals_record_metadata | - | - | - |
| PASS | canonical | article_uuid_is_node_uuid | - | c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1 | c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1 |
| PASS | canonical | file_links_equal_record_files | - | ['6658c489-9aad-483b-9004-27b62225425d'] | ['6658c489-9aad-483b-9004-27b62225425d'] |
| PASS | canonical | extra_carries_bundle_nid_changed | - | - | {'bundle': 'events', 'nid': 13332, 'changed': '2026-09-08T06:49:49+00… |
| PASS | canonical | facets_match_drupal_facets_rule | - | {'categories': ['Resources & Sustainable Development'], 'tags': ['Env… | {'categories': ['Resources & Sustainable Development'], 'tags': ['Env… |
| PASS | canonical | content_hash_is_sha256_of_body_text | - | - | 7e696dba9705aeac74c947df232fd242d5b579c42562a401a45144e88b9bce14 |
| PASS | canonical | body_text_non_empty | - | >0 chars | 3330 |
| PASS | dates | effective_start_date_present | Undated documents are excluded from date filters (pipeline warns). | a date | 2026-09-18T00:00:00+00:00 |
| PASS | dates | effective_date_evidence_present | - | - | - |
| INFO | dates | website_date_rule | rule=bundle_date_field source=cms_field fields=['field_event_start_date', 'field_event_en… | - | {'rule': 'bundle_date_field', 'source': 'cms_field', 'fields': ['fiel… |
| PASS | dates | canonical_date_equals_evidence_value | - | 2026-09-18T00:00:00+00:00 | 2026-09-18T00:00:00+00:00 |
| PASS | dates | canonical_end_equals_evidence_end | - | 2026-09-18T00:00:00+00:00 | 2026-09-18T00:00:00+00:00 |
| PASS | dates | date_source_equals_evidence_source | - | cms_field | cms_field |
| PASS | dates | date_decision_row_presence | Written only when the bundle maps to a real CMS date field. | True | 1 |
| PASS | chunks | chunks_produced | - | >0 children | {'parents': 0, 'children': 2} |
| PASS | chunks | chunk_ids_unique | - | 2 | 2 |
| PASS | chunks | every_parent_reference_resolves | - | - | - |
| PASS | chunks | parents_have_at_least_two_children | A parent is emitted only when it groups more than one child. | - | - |
| PASS | chunks | children_carry_document_id | - | - | - |
| PASS | chunks | children_carry_doc_version | - | 1 | [1] |
| PASS | chunks | children_within_token_limit | child_max_tokens=480 | 480 | {'max_child_tokens': 305, 'over': []} |
| PASS | chunks | parents_within_token_limit | parent_max_tokens=2200 | 2200 | {'max_parent_tokens': 0, 'over': []} |
| PASS | chunks | content_hash_is_sha256_of_text | - | - | - |
| PASS | chunks | child_index_is_contiguous | - | [0, 1] | [0, 1] |
| PASS | chunks | source_lines_covered_by_chunks | 100.000% of 23 source lines appear verbatim in some chunk | >=98% | {'coverage': 1.0, 'missing_sample': []} |
| PASS | chunks | children_have_embed_text_with_breadcrumb | embed_text = 'title › heading' + text | - | - |
| PASS | qdrant | point_count_equals_chunks | - | {'chunks': 2, 'indexer_reported': 2} | 2 |
| PASS | qdrant | every_chunk_id_is_a_point | - | 2 | {'missing': [], 'extra': []} |
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
| PASS | qdrant | swap_deleted_with_keep_ids | delete_document(id, keep_ids=<new chunk ids>) ran after the upsert. | 2 | [{'document_id': 'c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1', 'keep_ids': … |
| PASS | mysql | state_row_exists | - | - | - |
| PASS | mysql | row_source_type | - | website | website |
| PASS | mysql | row_source_key | - | https://teriin.org/event/wcef2026-accelerator-session-circular-public… | https://teriin.org/event/wcef2026-accelerator-session-circular-public… |
| PASS | mysql | row_fingerprint | - | 2026-09-08T06:49:49+00:00 | 2026-09-08T06:49:49+00:00 |
| PASS | mysql | row_content_hash | - | 7e696dba9705aeac74c947df232fd242d5b579c42562a401a45144e88b9bce14 | 7e696dba9705aeac74c947df232fd242d5b579c42562a401a45144e88b9bce14 |
| PASS | mysql | row_doc_version | - | 1 | 1 |
| PASS | mysql | row_pipeline_version | - | c1.i1.p2.e1 | c1.i1.p2.e1 |
| PASS | mysql | row_bundle | - | events | events |
| PASS | mysql | row_entity_type | - | node | node |
| PASS | mysql | row_changed_mark | - | 1788850189 | 1788850189 |
| PASS | mysql | row_title | - | WCEF2026 Accelerator Session: Circular Public Procurement for Sustain… | WCEF2026 Accelerator Session: Circular Public Procurement for Sustain… |
| PASS | mysql | row_url | - | https://teriin.org/event/wcef2026-accelerator-session-circular-public… | https://teriin.org/event/wcef2026-accelerator-session-circular-public… |
| PASS | mysql | row_effective_start_date | - | 2026-09-18T00:00:00+00:00 | 2026-09-18 00:00:00 |
| PASS | mysql | row_date_source | - | cms_field | cms_field |
| PASS | mysql | row_start_precision | - | day | day |
| PASS | mysql | row_effective_end_date | - | 2026-09-18T00:00:00+00:00 | 2026-09-18 00:00:00 |
| PASS | mysql | row_end_precision | - | day | day |
| PASS | mysql | row_indexed_at_set | - | not null | 2026-09-10 10:30:45 |
| PASS | mysql | row_raw_meta_equals_canonical | - | - | - |
| PASS | mysql | author_facet_rows | - | - | - |
| PASS | mysql | tag_facet_rows | - | ['Environmental governance', 'Government policy/regulations', 'Govern… | ['Environmental governance', 'Government policy/regulations', 'Govern… |
| PASS | mysql | theme_rows_match_classification | documents_theme = theme_taxonomy.classify(categories) | [('Resources & Sustainable Development', 'primary', None, 'main', 'Re… | [('Resources & Sustainable Development', 'primary', None, 'main', 'Re… |
| PASS | mysql | attachment_link_rows | - | [('6658c489-9aad-483b-9004-27b62225425d', 'attachment')] | [('6658c489-9aad-483b-9004-27b62225425d', 'attachment')] |
| PASS | mysql | ingest_log_indexed_row | - | 1 | ['indexed'] |
| PASS | mysql | ingest_log_chunk_count | - | 2 | 2 |
| PASS | mysql | ingest_log_hash_and_version | - | - | - |
| PASS | mysql | no_retry_marker | - | - | - |
| INFO | knowledge | stage_report | status=ok {'chunks_seen': 2, 'chunks_cached': 0, 'mentions': 0, 'entities_auto': 0, 'enti… | - | {'document_id': 'c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1', 'doc_version'… |
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
| N/A | knowledge | authored_points_at_this_document | The object identifies the document; the entity link is the claim's own provenance. | c78a7fde-a2f5-4b5c-9926-8f4f5ee240b1 | - |
| N/A | knowledge | partner_of_points_at_an_organization | - | - | - |
| PASS | knowledge | authored_never_exceeds_its_source | An author claim per stated author at most; unresolved names are skipped, never invented. | <= 0 | 0 |
| PASS | knowledge | partner_of_never_exceeds_its_source | - | <= 0 | 0 |
| N/A | knowledge | authored_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | partner_of_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | no_duplicate_claim_identity | - | 0 | 0 |
| N/A | knowledge | claim_ids_are_unique | - | 0 | 0 |
| PASS | knowledge | extraction_cache_recorded | One cache row per child chunk content hash. | 2 | 2 |
| PASS | graph | nothing_projected_when_skipped | projection skipped: no touched claims | - | {'claims_in_graph': 0} |

