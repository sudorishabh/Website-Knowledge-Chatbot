# 17_web_ongoing_projects_2f5ede1c-8091-4ec8-9dd8-866797f0af41

**website** · bundle `ongoing_projects` · outcome **indexed**

- document_id: `2f5ede1c-8091-4ec8-9dd8-866797f0af41`
- title: National Roadmap: Solarisation of Closed & Reclaimed Coal Mines in India
- source_key: https://teriin.org/project/national-roadmap-solarisation-closed-reclaimed-coal-mines-india
- handled in 2.605s (build 0.0s, extract Nones, chunk 0.002s, embed+upsert 0.379s, knowledge 2.195s)

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
- JSON:API `node/ongoing_projects` uuid `2f5ede1c-8091-4ec8-9dd8-866797f0af41` · nid 13382 · created 2026-09-08T10:31:02+00:00 · changed 2026-09-09T09:17:53+00:00
- change detection: status **new** · fingerprint `2026-09-09T09:17:53+00:00` · changed_mark 1788945473 · prior: False

### 2. Extraction
- HTML→text: body 2437 chars from 1 rich-text field(s); 0 PDF link(s) discovered ([])

### 3. Canonical document
- sections 1 · body chars 2437 · content_hash `dbcfee6fb4c3c47a67b80db5a400c3a4ff5ea39c5e213afb814e08aeba4cbe98` · doc_version 1
- authors [] · tags ['Renewable energy'] · categories ['Electricity and Renewables']
- entity_refs 3 · file_links []
- extra {'bundle': 'ongoing_projects', 'nid': 13382, 'changed': '2026-09-09T09:17:53+00:00'}

### 4. Dates
- effective_start_date **2026-04-10T00:00:00+00:00** (precision day, source cms_field) · effective_end_date None (precision None)
- rule `bundle_date_field` · source `cms_field` · fields ['field_ongoing_start_date'] · raw values ['2026-04-10T04:30:00+00:00'] · role range_start · range_issue None
- date_decision rows in MySQL: 1 — action `propose_override` rule `bundle_date_field` candidate 2026-04-10 00:00:00

### 5. Chunks
- 1 parent(s), 2 child(ren) · child tokens min/avg/max 153/228/303 · section types ['None']

### 6. Qdrant
- collection `e2e_audit_documents` · points for this document: 3 · indexer reported 3
- delete_document calls: [{'document_id': '2f5ede1c-8091-4ec8-9dd8-866797f0af41', 'keep_ids': 3}]

### 7. MySQL
- `e2e_audit_documents`: 1 row(s)
- `e2e_audit_documents_author`: 0 row(s)
- `e2e_audit_documents_tag`: 1 row(s)
- `e2e_audit_documents_theme`: 1 row(s)
- `e2e_audit_documents_retry`: 0 row(s)
- `e2e_audit_documents_dead_link`: 0 row(s)
- `e2e_audit_documents_date_decision`: 1 row(s)
- `e2e_audit_documents_entity_mention`: 8 row(s)
- `e2e_audit_documents_assertion`: 0 row(s)
- `e2e_audit_documents_assertion_rejection`: 0 row(s)
- `e2e_audit_documents_predicate_candidate`: 0 row(s)
- `e2e_audit_documents_knowledge_run`: 1 row(s)
- `e2e_audit_documents_attachment`: 0 row(s)
- `e2e_audit_documents_attachment (as file)`: 0 row(s)
- `e2e_audit_documents_entity_resolution_decision`: 8 row(s)
- `e2e_audit_documents_entity_extraction`: 2 row(s)
- `e2e_audit_documents_assertion_link`: 0 row(s)
- `e2e_audit_ingest_log`: 1 row(s)
- `e2e_audit_documents_entity (referenced)`: 1 row(s)

### 8. Knowledge stage
- status **ok** in 2.19s · knowledge_version `kv1:9f993bff646d51c0`
- counts {'chunks_seen': 2, 'chunks_cached': 0, 'mentions': 8, 'entities_auto': 2, 'entities_provisional': 0, 'entities_ambiguous': 0, 'entities_unresolved': 6, 'claims_built': 0, 'claims_staged': 0, 'claims_rejected': 0, 'claims_retracted': 0, 'pending_predicates': 0, 'conflicts_disputed': 0, 'conflicts_superseded': 0}
- projection {'status': 'skipped', 'version': None, 'edges': 0}
  - stage `prelude`: ran · counts {'entities': 2451, 'chunks': 2} · notes [] · errors []
  - stage `supersede`: skipped · counts {} · notes [] · errors []
  - stage `mentions`: ran · counts {'cached': 0, 'mentions': 8} · notes [] · errors []
  - stage `resolution`: ran · counts {'auto': 2, 'provisional': 0, 'ambiguous': 0, 'unresolved': 6, 'cache_recorded': 2} · notes [] · errors []
  - stage `claims`: ran · counts {'llm_calls': 1, 'llm_failures': 0, 'run_calls_used': 43, 'run_calls_remaining': 149957, 'llm': 0, 'unknown_predicates': 0, 'built': 0} · notes ['this document has no canonical PROJECT entity yet; seeding is a global pass (scripts.build_knowledge), and its claims will be staged once it runs'] · errors []
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
| PASS | canonical | title_equals_node_title | - | National Roadmap: Solarisation of Closed & Reclaimed Coal Mines in In… | National Roadmap: Solarisation of Closed & Reclaimed Coal Mines in In… |
| PASS | canonical | body_is_single_section | Website body becomes one section. | 2437 | 2437 |
| PASS | canonical | raw_meta_equals_record_metadata | - | - | - |
| PASS | canonical | article_uuid_is_node_uuid | - | 2f5ede1c-8091-4ec8-9dd8-866797f0af41 | 2f5ede1c-8091-4ec8-9dd8-866797f0af41 |
| PASS | canonical | file_links_equal_record_files | - | - | - |
| PASS | canonical | extra_carries_bundle_nid_changed | - | - | {'bundle': 'ongoing_projects', 'nid': 13382, 'changed': '2026-09-09T0… |
| PASS | canonical | facets_match_drupal_facets_rule | - | {'categories': ['Electricity and Renewables'], 'tags': ['Renewable en… | {'categories': ['Electricity and Renewables'], 'tags': ['Renewable en… |
| PASS | canonical | content_hash_is_sha256_of_body_text | - | - | dbcfee6fb4c3c47a67b80db5a400c3a4ff5ea39c5e213afb814e08aeba4cbe98 |
| PASS | canonical | body_text_non_empty | - | >0 chars | 2437 |
| PASS | dates | effective_start_date_present | Undated documents are excluded from date filters (pipeline warns). | a date | 2026-04-10T00:00:00+00:00 |
| PASS | dates | effective_date_evidence_present | - | - | - |
| INFO | dates | website_date_rule | rule=bundle_date_field source=cms_field fields=['field_ongoing_start_date'] | - | {'rule': 'bundle_date_field', 'source': 'cms_field', 'fields': ['fiel… |
| PASS | dates | canonical_date_equals_evidence_value | - | 2026-04-10T00:00:00+00:00 | 2026-04-10T00:00:00+00:00 |
| PASS | dates | canonical_end_equals_evidence_end | - | - | - |
| PASS | dates | date_source_equals_evidence_source | - | cms_field | cms_field |
| PASS | dates | date_decision_row_presence | Written only when the bundle maps to a real CMS date field. | True | 1 |
| PASS | chunks | chunks_produced | - | >0 children | {'parents': 1, 'children': 2} |
| PASS | chunks | chunk_ids_unique | - | 3 | 3 |
| PASS | chunks | every_parent_reference_resolves | - | - | - |
| PASS | chunks | parents_have_at_least_two_children | A parent is emitted only when it groups more than one child. | ['0440bf77-d1de-5f70-b938-0d5933ee4ec4'] | {'0440bf77-d1de-5f70-b938-0d5933ee4ec4': 2} |
| PASS | chunks | children_carry_document_id | - | - | - |
| PASS | chunks | children_carry_doc_version | - | 1 | [1] |
| PASS | chunks | children_within_token_limit | child_max_tokens=480 | 480 | {'max_child_tokens': 303, 'over': []} |
| PASS | chunks | parents_within_token_limit | parent_max_tokens=2200 | 2200 | {'max_parent_tokens': 423, 'over': []} |
| PASS | chunks | content_hash_is_sha256_of_text | - | - | - |
| PASS | chunks | child_index_is_contiguous | - | [0, 1] | [0, 1] |
| PASS | chunks | source_lines_covered_by_chunks | 100.000% of 4 source lines appear verbatim in some chunk | >=98% | {'coverage': 1.0, 'missing_sample': []} |
| PASS | chunks | children_have_embed_text_with_breadcrumb | embed_text = 'title › heading' + text | - | - |
| PASS | qdrant | point_count_equals_chunks | - | {'chunks': 3, 'indexer_reported': 3} | 3 |
| PASS | qdrant | every_chunk_id_is_a_point | - | 3 | {'missing': [], 'extra': []} |
| PASS | qdrant | payload_equals_chunk_payload_plus_stamps | Stored payload == Chunk.to_payload() plus created_at/updated_at/embed_model. | - | - |
| PASS | qdrant | children_have_real_vectors | - | - | - |
| PASS | qdrant | parents_have_zero_vectors | - | - | - |
| PASS | qdrant | vector_dimension_matches_setting | - | 3072 | [3072] |
| PASS | qdrant | children_stamped_with_embed_model | - | - | ['text-embedding-3-large:3072'] |
| PASS | qdrant | points_carry_document_id | - | - | - |
| PASS | qdrant | points_carry_effective_start_date | - | 2026-04-10T00:00:00+00:00 | ['2026-04-10T00:00:00+00:00'] |
| PASS | qdrant | points_carry_year_precision_only_when_year | - | day | ['None'] |
| PASS | qdrant | points_are_current_and_versioned | - | 1 | ['1'] |
| PASS | qdrant | points_stamped_with_pipeline_version | - | c1.i1.p2.e1 | ['c1.i1.p2.e1'] |
| PASS | qdrant | swap_deleted_with_keep_ids | delete_document(id, keep_ids=<new chunk ids>) ran after the upsert. | 3 | [{'document_id': '2f5ede1c-8091-4ec8-9dd8-866797f0af41', 'keep_ids': … |
| PASS | mysql | state_row_exists | - | - | - |
| PASS | mysql | row_source_type | - | website | website |
| PASS | mysql | row_source_key | - | https://teriin.org/project/national-roadmap-solarisation-closed-recla… | https://teriin.org/project/national-roadmap-solarisation-closed-recla… |
| PASS | mysql | row_fingerprint | - | 2026-09-09T09:17:53+00:00 | 2026-09-09T09:17:53+00:00 |
| PASS | mysql | row_content_hash | - | dbcfee6fb4c3c47a67b80db5a400c3a4ff5ea39c5e213afb814e08aeba4cbe98 | dbcfee6fb4c3c47a67b80db5a400c3a4ff5ea39c5e213afb814e08aeba4cbe98 |
| PASS | mysql | row_doc_version | - | 1 | 1 |
| PASS | mysql | row_pipeline_version | - | c1.i1.p2.e1 | c1.i1.p2.e1 |
| PASS | mysql | row_bundle | - | ongoing_projects | ongoing_projects |
| PASS | mysql | row_entity_type | - | node | node |
| PASS | mysql | row_changed_mark | - | 1788945473 | 1788945473 |
| PASS | mysql | row_title | - | National Roadmap: Solarisation of Closed & Reclaimed Coal Mines in In… | National Roadmap: Solarisation of Closed & Reclaimed Coal Mines in In… |
| PASS | mysql | row_url | - | https://teriin.org/project/national-roadmap-solarisation-closed-recla… | https://teriin.org/project/national-roadmap-solarisation-closed-recla… |
| PASS | mysql | row_effective_start_date | - | 2026-04-10T00:00:00+00:00 | 2026-04-10 00:00:00 |
| PASS | mysql | row_date_source | - | cms_field | cms_field |
| PASS | mysql | row_start_precision | - | day | day |
| PASS | mysql | row_effective_end_date | - | - | - |
| PASS | mysql | row_end_precision | - | - | - |
| PASS | mysql | row_indexed_at_set | - | not null | 2026-09-10 10:30:36 |
| PASS | mysql | row_raw_meta_equals_canonical | - | - | - |
| PASS | mysql | author_facet_rows | - | - | - |
| PASS | mysql | tag_facet_rows | - | ['Renewable energy'] | ['Renewable energy'] |
| PASS | mysql | theme_rows_match_classification | documents_theme = theme_taxonomy.classify(categories) | [('Electricity and Renewables', 'sub', 'Energy', 'main', 'Energy > El… | [('Electricity and Renewables', 'sub', 'Energy', 'main', 'Energy > El… |
| PASS | mysql | attachment_link_rows | - | - | - |
| PASS | mysql | ingest_log_indexed_row | - | 1 | ['indexed'] |
| PASS | mysql | ingest_log_chunk_count | - | 3 | 3 |
| PASS | mysql | ingest_log_hash_and_version | - | - | - |
| PASS | mysql | no_retry_marker | - | - | - |
| INFO | knowledge | stage_report | status=ok {'chunks_seen': 2, 'chunks_cached': 0, 'mentions': 8, 'entities_auto': 2, 'enti… | - | {'document_id': '2f5ede1c-8091-4ec8-9dd8-866797f0af41', 'doc_version'… |
| PASS | knowledge | run_row_written | - | 1 | [(1, 'ok')] |
| PASS | knowledge | run_row_matches_report | - | {'status': 'ok', 'mentions': 8, 'claims_staged': 0} | {'status': 'ok', 'mentions': 8, 'claims_staged': 0} |
| PASS | knowledge | mention_rows_equal_report | - | 8 | 8 |
| PASS | knowledge | decision_rows_equal_report | - | 8 | 8 |
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
| N/A | knowledge | authored_points_at_this_document | The object identifies the document; the entity link is the claim's own provenance. | 2f5ede1c-8091-4ec8-9dd8-866797f0af41 | - |
| N/A | knowledge | partner_of_points_at_an_organization | - | - | - |
| PASS | knowledge | authored_never_exceeds_its_source | An author claim per stated author at most; unresolved names are skipped, never invented. | <= 0 | 0 |
| PASS | knowledge | partner_of_never_exceeds_its_source | - | <= 0 | 0 |
| N/A | knowledge | authored_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | partner_of_has_no_duplicate_edge | One edge per relationship, however many fields state it. | 0 | 0 |
| N/A | knowledge | no_duplicate_claim_identity | - | 0 | 0 |
| N/A | knowledge | claim_ids_are_unique | - | 0 | 0 |
| PASS | knowledge | extraction_cache_recorded | One cache row per child chunk content hash. | 2 | 2 |
| PASS | graph | nothing_projected_when_skipped | projection skipped: no touched claims | - | {'claims_in_graph': 0} |

