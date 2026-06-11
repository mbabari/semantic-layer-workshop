# Census Bureau ACS — Neocarta benchmark & retrieval optimization

Hands-on extension of the semantic-layer workshop. Use it to compare **WITH vs WITHOUT**
Neocarta on a large, ambiguous schema, then experiment with **compact retrieval** and
other payload optimizations.

**Public data (no copy):** queries run against
[`bigquery-public-data.census_bureau_acs`](https://console.cloud.google.com/marketplace/product/bigquery-public-data/census-bureau-acs)
(278 near-identical tables, ~252 columns each). Only **metadata** is loaded into Neo4j.

Findings write-up: [`census_demo_findings.md`](census_demo_findings.md)

---

## Prerequisites

1. Complete [Module 0 setup](../module-0/README.md) (GCP, Neo4j, OpenAI key).
2. Copy env template and fill in **your** values (never commit `.env`):

   ```bash
   cp .env.example .env
   ```

3. Authenticate to GCP:

   ```bash
   gcloud auth application-default login
   ```

Required `.env` keys for the Census demo:

| Variable | Example | Purpose |
|----------|---------|---------|
| `GCP_PROJECT_ID` | `my-gcp-project` | BigQuery billing project |
| `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` | Aura or Desktop | Neo4j connection |
| `CENSUS_NEO4J_DATABASE` | `census` | Separate DB for ACS metadata |
| `OPENAI_API_KEY` | `sk-...` | Embeddings + agent LLM |
| `ANTHROPIC_API_KEY` | optional | Anthropic models in agent menu |

---

## 1. Build the semantic layer (once)

From the repo root:

```bash
uv sync
uv run module-2/build_census_semantic_layer.py
```

This uses Neocarta's `BigQuerySchemaConnector` to extract
`bigquery-public-data.census_bureau_acs` from `INFORMATION_SCHEMA`, load
`Database → Schema → Table → Column` into Neo4j (`census`), add geography-rich
table descriptions, and embed Schema + Table nodes.

Stages are re-runnable:

```bash
uv run module-2/build_census_semantic_layer.py --only descriptions
uv run module-2/build_census_semantic_layer.py --only embeddings
```

---

## 2. Run the agents

### Interactive (recommended for demos)

```bash
# WITH Neocarta — raw MCP table payload
uv run module-3/census_agent.py --mode with

# WITH Neocarta — compact schema wrapper (~45–81% fewer tokens)
uv run module-3/census_agent_optimized.py --mode with

# WITHOUT — brute-force schema discovery (baseline)
uv run module-3/census_agent.py --mode without
```

Type `1`–`4` for verified demo questions, or paste your own. Use `exit` to quit.

### Full benchmark sweep

```bash
uv run module-3/census_agent_optimized.py --runall
uv run module-3/census_agent_optimized.py --runall --model gpt-4o-mini
```

Prints a WITH vs WITHOUT comparison table (tokens, cost, success rate).

Aggregate benchmark (original script):

```bash
MODELS=gpt-4o-mini,gpt-4o uv run module-3/benchmark_census.py
```

---

## 3. What to optimize (for retrieval experiments)

| Lever | Where | Notes |
|-------|--------|------|
| **Table disambiguation** | `build_census_semantic_layer.py` → `GEO_DESCRIPTIONS`, table descriptions | Fixes geography/year/span among 278 tables |
| **MCP tool allowlist** | `NEOCARTA_ALLOWED` in `census_agent*.py` | Blocks full-catalog dump tools |
| **Payload compaction** | `census_agent_optimized.py` → `compact_table_context()` | Strips null JSON; main token win |
| **max_tables cap** | `CENSUS_COMPACT_MAX_TABLES` env | Default `1` |
| **Column descriptions** | `CENSUS_COMPACT_DESCRIPTIONS=1` | More context, more tokens |
| **Column embeddings** | `build_census_semantic_layer.py` → `EMBEDDING_NODE_LABELS` | 68K columns — slow; enables column-hybrid retrieval |
| **Business terms** | Module 2b pattern (CSV glossary) | Maps “rent burden” → column names |

**Product feedback:** ideal long-term fix is a `verbosity=compact` option on Neocarta MCP
`TableContext` responses rather than agent-side wrappers.

---

## 4. Verified demo questions

| # | Question (summary) | Expected table |
|---|-------------------|----------------|
| 1 | Rent burden by county, 2017 5-year ACS | `county_2017_5yr` |
| 2 | Congressional districts by bachelor's degree, 2016 1-year | `congressionaldistrict_2016_1yr` |
| 3 | States by median age, 2015 5-year | `state_2015_5yr` |
| 4 | Top 10 counties by median household income, 2018 5-year | `county_2018_5yr` |

Avoid live questions about overlapping “area” geographies (CBSA vs PUMA vs place) —
table-level vector search struggles to separate them.

---

## 5. Files in this extension

| File | Role |
|------|------|
| `module-2/build_census_semantic_layer.py` | Build Neo4j `census` graph from public BQ metadata |
| `module-3/census_agent.py` | Interactive WITH/WITHOUT agent + `--runall` sweep |
| `module-3/census_agent_optimized.py` | Compact retrieval wrapper (Option A) |
| `module-3/benchmark_census.py` | Non-interactive WITH vs WITHOUT benchmark |
| `module-3/census_demo_findings.md` | Measured results & talking points |

Workshop originals (`3_agent.py`, Module 2 notebooks) are unchanged.

---

## Security reminder

- **Never commit** `.env`, API keys, or internal Slack/meeting notes.
- `.env` is gitignored; use `.env.example` as the template only.
- This demo reads **public** Census data; you only pay BigQuery for bytes scanned.
