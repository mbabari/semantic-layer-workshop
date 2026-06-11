# Census ACS demo — Neocarta semantic layer findings

Customer-facing write-up for the Census Bureau ACS use case. Source data and
benchmark: `module-3/benchmark_census.py`; live demo: `module-3/census_agent.py`.

## The setup

- **Data**: [`bigquery-public-data.census_bureau_acs`](https://console.cloud.google.com/marketplace/product/bigquery-public-data/census-bureau-acs) — **278 near-identical tables**
  (`<geography>_<year>_<span>`, e.g. `county_2018_5yr`, `cbsa_2017_1yr`),
  **~252 columns each**, ~68K columns total. See [How to connect](#how-to-connect-to-census-bureau-acs) below.
- **Full schema ≈ 475K tokens — it does not fit in a 128K-context model.** You
  *cannot* paste this schema into a prompt. That is the core problem.
- **Semantic layer**: Neo4j `census` database (built by
  `module-2/build_census_semantic_layer.py`) — 278 tables + 68K columns +
  disambiguating descriptions + table/schema embeddings.
- **Comparison** (only variable = is Neocarta in the loop?):
  - **WITH Neocarta**: `get_context_by_table_hybrid_search` (semantic retrieval) + `execute_sql`.
  - **WITHOUT Neocarta**: `list_census_tables` + `get_table_columns` + `execute_sql`
    (the agent must brute-force the schema; pasting it is impossible).
- **Models**: `gpt-4o-mini` (cheap/weak) and `gpt-4o` (strong/expensive).

## Headline result: cheap models become reliable

Average per question (4 questions), `gpt-4o-mini`:

| | tokens | cost / question | correct table | tool calls |
|---|---|---|---|---|
| **WITH Neocarta** | 37,363 | $0.0057 | **4 / 4** | 2 (clean) |
| **WITHOUT** | 40,229 | $0.0062 | **2 / 4** | up to 20 |

Without the semantic layer, **2 of the 4 questions looped for 20 tool calls and
gave up with no answer at all** (the agent kept re-scanning metadata trying to
find the right table among 278). With Neocarta, every question was a correct,
two-step answer (retrieve → query).

<img width="811" height="857" alt="image" src="https://github.com/user-attachments/assets/5c587c08-1ee6-4904-ac5a-d7674568a54c" />


### The talking point

> "With a cheap model and no semantic layer, half the questions **failed** — and
> you still paid ~66K tokens for those dead-end runs that returned nothing. With
> Neocarta, the same cheap model answers every question correctly in two steps.
> You stop paying for failure, and you can use a cheaper model with confidence."

The value is **reliability and accuracy for cheaper models** — and eliminating
**wasted spend on runs that produce no answer** — not a flat "X% fewer tokens."

## Why a naive agent struggles (the "confusion" to show on screen)

- 278 tables that differ only by geography × year × survey span.
- Cryptic column names (`income_per_capita`, `percent_income_spent_on_rent`,
  `male_85_and_over`, hundreds of age-by-sex buckets).
- No way to fit the schema in context → the agent lists tables and guesses,
  re-sending growing metadata each turn until it succeeds, fails, or times out.

## How to connect to Census Bureau ACS

The demo **does not copy** Census data into your project. It queries Google’s public
dataset in place and builds a **metadata-only** semantic layer in Neo4j.

### BigQuery (query execution)

| What | Value |
|------|--------|
| **Public dataset** | `bigquery-public-data.census_bureau_acs` |
| **Marketplace / docs** | [Census Bureau ACS on BigQuery Public Datasets](https://console.cloud.google.com/marketplace/product/bigquery-public-data/census-bureau-acs) · [BigQuery public datasets overview](https://cloud.google.com/bigquery/public-data) |
| **Billing project** | Your GCP project (`GCP_PROJECT_ID` in `.env`) — you pay for bytes scanned, not storage |
| **Auth** | Application Default Credentials: `gcloud auth application-default login` |
| **Fully qualified table** | `` `bigquery-public-data.census_bureau_acs.county_2018_5yr` `` |

Optional env overrides (defaults shown):

```bash
CENSUS_SOURCE_PROJECT=bigquery-public-data
CENSUS_SOURCE_DATASET=census_bureau_acs
GCP_PROJECT_ID=your-billing-project
```

Quick sanity check in the BigQuery console or `bq`:

```sql
SELECT geo_id, median_income
FROM `bigquery-public-data.census_bureau_acs.county_2018_5yr`
LIMIT 5
```

### Neo4j semantic layer (metadata / retrieval)

| What | Value |
|------|--------|
| **Build script** | `module-2/build_census_semantic_layer.py` |
| **Neo4j database** | `census` (set `CENSUS_NEO4J_DATABASE=census` or use default in script) |
| **Source** | BigQuery `INFORMATION_SCHEMA` for the public dataset (metadata only) |
| **Requires** | `NEO4J_*`, `OPENAI_API_KEY` (embeddings), `GCP_PROJECT_ID` (BQ client for schema extract) |

```bash
uv run module-2/build_census_semantic_layer.py
```

### Run the agent

```bash
uv run module-3/census_agent.py --mode with          # Neocarta + SQL
uv run module-3/census_agent_optimized.py --mode with   # compact schema (lower tokens)
```

Neocarta MCP reads the **`census`** Neo4j database; SQL runs against **`bigquery-public-data.census_bureau_acs`** via the local `execute_sql` tool.

## Reproduce it

```bash
# Aggregate numbers (both models):
MODELS=gpt-4o-mini,gpt-4o MONTHLY_QUERIES=10000 \
  uv run module-3/benchmark_census.py

# Live side-by-side (run the same question in each):
uv run module-3/census_agent.py --mode without   # watch it loop / give up
uv run module-3/census_agent.py --mode with       # 2-call correct answer
```

Use the built-in demo questions (`1`–`4`) — verified to retrieve the correct
table WITH Neocarta. Avoid live questions about overlapping "area" geographies
(metropolitan area / CBSA vs PUMA vs place vs block group): those concepts are
too semantically similar for table-level vector search to separate reliably.

## Verified demo questions

1. Rent burden (share of income spent on rent) by county, 2017 5-year ACS → `county_2017_5yr`
2. Congressional voting districts by residents with a bachelor's degree, 2016 1-year ACS → `congressionaldistrict_2016_1yr`
3. U.S. states by median age, 2015 5-year ACS → `state_2015_5yr`
4. Counties by highest median household income, 2018 5-year ACS → `county_2018_5yr`
