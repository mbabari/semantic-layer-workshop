# Module 3: Run an Agent with the Semantic Layer

## What You'll Do

Run a self-contained command-line agent that connects to the semantic layer built in Module 2. Ask natural language questions about the ACME Corp dataset and watch the agent use Neo4j to find the right tables before generating and executing SQL — no external chat application required.

The agent ([`3_agent.py`](3_agent.py)) is a LangGraph Text2SQL agent wired to two MCP servers:

| MCP server | Transport | Role |
|---|---|---|
| **neocarta** | `stdio` (via `uvx`) | Semantic layer retrieval — searches the Neo4j graph for relevant schema |
| **BigQuery** | remote HTTP | Query execution — the agent is allowlisted to the `execute_sql` tool only |

```
Your question
      ↓
neocarta MCP → relevant tables, columns, FK paths (from Neo4j)
      ↓
LLM (SQL generation)
      ↓
BigQuery MCP → execute_sql
      ↓
Answer
```

## Prerequisites

- Semantic layer graph built and accessible (Module 2 complete)
- Environment setup complete: [`../module-0/README.md`](../module-0/README.md)
- The following set in your `.env` (see [`.env.example`](../.env.example)):
  - `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`
  - `OPENAI_API_KEY`
  - `GCP_PROJECT_ID`, `BIGQUERY_DATASET_ID`
- Google Cloud Application Default Credentials, used by the BigQuery MCP server:
  ```bash
  gcloud auth application-default login
  ```
- The BigQuery MCP API enabled on your project:
  ```bash
  gcloud beta services mcp enable bigquery.googleapis.com --project=YOUR_PROJECT_ID
  ```

> **OpenAI only.** This version of the workshop supports **OpenAI models and
> embeddings exclusively** — for both the agent LLM (`AGENT_MODEL`) and the
> semantic-search embeddings (`EMBEDDING_MODEL`). A single `OPENAI_API_KEY`
> covers both. Other providers (Gemini, Cohere, Azure, Bedrock) are not
> supported here.

> **Embeddings must match Module 2.** The agent forwards `EMBEDDING_MODEL`
> (default `text-embedding-3-small`) and `EMBEDDING_DIMENSIONS` (default `768`)
> to the neocarta server. These must match the values used to build the graph
> in Module 2, or semantic search will fail.

## Run It

From the repository root:

```bash
uv run module-3/3_agent.py
```

You'll get an interactive prompt. Ask a question, watch the tool calls, and type `exit` (or `Ctrl-D`) to quit.

```
===================================== Chat =====================================
Ask a question about the dataset, or type 'exit' to quit.

> Which product line contributes the most to total ARR?
Calling tools: get_context_by_table_hybrid_search
Calling tools: execute_sql
Agent: Acme Graph DB Enterprise contributes the most ...
```

### Optional configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_MODEL` | `gpt-4o-mini` | OpenAI chat model for the agent (e.g. `gpt-4o`, `gpt-4o-mini`) |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model for semantic search |
| `EMBEDDING_DIMENSIONS` | `768` | Embedding vector dimensions |

> Only OpenAI models are supported in this version — see the OpenAI-only note above.

> Having trouble reaching the remote BigQuery MCP server? You can swap in the
> local **MCP Toolbox for Databases** as a drop-in replacement — see
> [BigQuery MCP Server Not Responding](../reference/troubleshooting.md#3-bigquery-mcp-server-not-responding).

## Exploring the ACME Dataset

Once running, try these questions. Watch the tool traces to see how the semantic layer is informing query generation.

More information on these example questions may be found in the [Example Questions reference file](../reference/example-questions.md).

### 1 (Easy). Which product line contributes the most to total ARR, and what is the breakdown?

**Expected result:**

| Product | Active subscriptions | ARR (USD) | % of total |
|---|---|---|---|
| Acme Graph DB Enterprise | 3 | $3,180,000 | 82.9% |
| Acme Cloud | 4 | $314,000 | 8.2% |
| Acme GraphRAG | 1 | $240,000 | 6.3% |
| Acme Graph Data Science | 1 | $100,000 | 2.6% |

**Insight:** Graph DB Enterprise is the dominant revenue driver, accounting for over 80% of ARR despite having fewer subscriptions than Cloud. This concentration in a single product line is a key business risk to monitor.

### 2 (Easy). Which subscriptions are renewing in the next 90 days, ranked by ARR at risk?

**Expected result** *(as of Q4 2025 data):*

| Subscription | Customer | Plan | ARR (USD) | Renewal date |
|---|---|---|---|---|
| SUB002 | CUST001 | GDS Enterprise | $100,000 | 2026-01-01 |
| SUB008 | CUST006 | Cloud Pro | $80,000 | 2026-03-01 |
| SUB005 | CUST004 | Cloud Pro | $60,000 | 2026-01-01 |
| SUB010 | CUST008 | Cloud Growth | $54,000 | 2026-01-01 |

**Insight:** $294,000 in ARR is up for renewal in Q1 2026. Three of the four renewals fall on January 1st, making early Q1 a critical period for CSM outreach.

---

### 3 (Easy). What is the total committed annual vendor spend by category?

**Expected result:**

| Category | Active contracts | Annual spend (USD) |
|---|---|---|
| SaaS | 6 | $4,340,000 |
| Consulting | 2 | $510,000 |
| Hardware | 1 | $180,000 |

**Insight:** SaaS tooling dominates Acme's vendor spend at $4.34M annually across 6 contracts — nearly 88% of total committed external spend. Worth reviewing for consolidation opportunities.

---

### 4 (Medium). What is the win rate and average deal size by customer segment?

**Expected result:**

| Segment | Total opps | Won | Lost | Win rate | Avg won deal (USD) |
|---|---|---|---|---|---|
| Enterprise | 4 | 3 | 0 | 100% | $1,700,000 |
| Mid-Market | 5 | 1 | 1 | 50% | $120,000 |
| SMB | 1 | 0 | 0 | — | — |

**Insight:** Enterprise deals close at a 100% win rate with an average deal size 14× larger than Mid-Market. However, SMB has no closed deals yet, suggesting the segment may still be in early exploration. The mid-market 50% win rate and a large open pipeline make it the key segment to focus on for improving conversion.

---

### 5 (Medium). How are performance ratings distributed across departments in the most recent review cycle?

**Expected result** *(2024-H2 cycle):*

| Department | Rating | Employees | Avg score |
|---|---|---|---|
| Customer Success | Meets | 1 | 3.70 |
| Engineering | Below | 1 | 2.40 |
| Engineering | Exceeds | 3 | 4.60 |
| Engineering | Meets | 1 | 3.80 |
| Product | Exceeds | 1 | 4.50 |
| Product | Meets | 1 | 3.90 |
| Sales | Exceeds | 1 | 4.40 |
| Sales | Meets | 1 | 3.60 |

**Insight:** Engineering has the widest performance spread — three "Exceeds" employees averaging 4.6, alongside one "Below" employee at 2.4. Sales and Product skew positive. Customer Success has only one reviewed employee, suggesting a data coverage gap to investigate.

---

### 6 (Hard). Is there a correlation between a customer's health score and their support experience?

**Expected result:**

| Customer | Segment | Health score | Account status | Tickets | Avg CSAT | Avg resolution (hrs) |
|---|---|---|---|---|---|---|
| Umbrella Biotech | Mid-Market | 30 | Churned | 1 | 3.0 | 49.0 |
| Prometheus AI | SMB | 55 | Prospect | 1 | — | — |
| Hooli Labs | Mid-Market | 64 | Active | 1 | — | — |
| Soylent Retail | Enterprise | 68 | Active | 1 | 5.0 | 31.0 |
| Initech Systems | Mid-Market | 72 | Active | 1 | 4.0 | 21.0 |
| Tyrell Data | Mid-Market | 78 | Active | 1 | 4.0 | 52.0 |
| Acme Widgets Co | Mid-Market | 81 | Active | 1 | 5.0 | 27.0 |
| Globex Financial | Enterprise | 87 | Active | 1 | 5.0 | 7.0 |
| Stark Industries | Enterprise | 91 | Active | 1 | — | — |
| Massive Dynamic | Enterprise | 95 | Active | 1 | 5.0 | 2.0 |

**Insight:** A clear positive correlation emerges between health score and support quality. The churned customer (Umbrella Biotech, score 30) had the lowest CSAT (3.0) and the longest resolution time (49 hrs) of any rated ticket. Conversely, the two highest-scoring customers — Massive Dynamic (95) and Globex Financial (87) — had perfect CSAT scores and the fastest resolution times (2 hrs and 7 hrs respectively). This suggests that support responsiveness is both a leading indicator of and contributor to customer health. Customers with CSAT scores below 4.0 or resolution times above 30 hours warrant proactive CSM outreach.

## Alternative: Claude Desktop

Prefer a graphical chat client? You can connect the same semantic layer to [Claude Desktop](https://claude.ai/download) (or any MCP-compatible application) instead of running the CLI agent. Add both MCP servers to your configuration file:

```json
{
  "mcpServers": {
    "neocarta": {
      "command": "uvx",
      "args": [
        "--from",
        "neocarta[mcp]@0.4.0",
        "neocarta-mcp"
      ],
      "env": {
        "NEO4J_URI": "neo4j+s://xxxxxxxx.databases.neo4j.io",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "your-password",
        "NEO4J_DATABASE": "neo4j",
        "OPENAI_API_KEY": "sk-...",
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "EMBEDDING_DIMENSIONS": "768"
      }
    },
    "bigquery": {
      "command": "npx",
      "args": ["-y", "@toolbox-sdk/server", "--prebuilt", "bigquery", "--stdio"],
      "env": {
        "BIGQUERY_PROJECT": "your-gcp-project-id"
      }
    }
  }
}
```

Replace all placeholder values with your credentials, then restart the application.

To effectively use the Neo4j semantic layer, disable the schema-discovery tools the BigQuery server exposes so the agent relies on neocarta for schema retrieval and only uses BigQuery to execute SQL:

| Tool | Enabled |
|------|---------|
| `list_dataset_ids` | ❌ |
| `search_catalog` | ❌ |
| `get_dataset_info` | ❌ |
| `ask_data_insights` | ❌ |
| `list_table_ids` | ❌ |
| `analyze_contribution` | ❌ |
| `get_table_info` | ❌ |
| `forecast` | ❌ |
| `execute_sql` | ✅ |

## What's Next

Try loading your own schema in Module 4.

→ [Module 4: Bring Your Own Data](../module-4/README.md)
