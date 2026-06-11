"""Benchmark token usage / cost: WITH vs WITHOUT the Neocarta semantic layer,
on the Census Bureau ACS dataset (278 near-identical tables, ~68K columns).

The full ACS schema is ~475K tokens -- it does NOT fit in a 128K-context model,
so "just paste the schema" is impossible. The realistic naive baseline is
schema *discovery*: the agent lists tables and fetches columns by brute force,
re-sending that metadata every turn and often picking the wrong geography/year.

Two agents answer the same questions:
  * WITH    -> Neocarta MCP retrieval (against the `census` Neo4j db) + execute_sql.
               One targeted retrieval returns the exact table + columns.
  * WITHOUT -> list_census_tables + get_table_columns + execute_sql. No retrieval;
               the agent must discover the schema itself.

Measures input/output tokens, estimated USD cost (LiteLLM pricing), tool-call
count, the tables the agent actually queried, and projects monthly savings.

Usage (from repo root):
    ./.venv/bin/python module-3/benchmark_census.py
    MODELS=gpt-4o-mini,gpt-4o MONTHLY_QUERIES=10000 ./.venv/bin/python module-3/benchmark_census.py
"""

import asyncio
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import bigquery
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient

from langchain.agents import create_agent
from langchain.tools import BaseTool
from langchain_litellm import ChatLiteLLM
from langgraph.checkpoint.memory import InMemorySaver

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)

SOURCE_PROJECT = os.getenv("CENSUS_SOURCE_PROJECT", "bigquery-public-data")
SOURCE_DATASET = os.getenv("CENSUS_SOURCE_DATASET", "census_bureau_acs")
NEO4J_DATABASE = os.getenv("CENSUS_NEO4J_DATABASE", "census")
BILLING_PROJECT = os.getenv("GCP_PROJECT_ID")
TABLE_FQN = f"{SOURCE_PROJECT}.{SOURCE_DATASET}"

MAX_BYTES_BILLED = int(os.getenv("MAX_BYTES_BILLED", str(15 * 10**9)))
RECURSION_LIMIT = int(os.getenv("RECURSION_LIMIT", "40"))
MONTHLY_QUERIES = int(os.getenv("MONTHLY_QUERIES", "10000"))
MODELS = [m.strip() for m in os.getenv("MODELS", "gpt-4o-mini,gpt-4o").split(",") if m.strip()]

# Targeted retrieval only -- never expose get_full_metadata_schema (full-catalog
# dump) or the list_* tools, the same way the workshop allowlists BigQuery to execute_sql.
NEOCARTA_ALLOWED = {
    "get_context_by_table_hybrid_search",
    "get_context_by_column_hybrid_search",
    "get_context_by_schema_and_table_vector_search",
}

# (question, expected_table) -- expected table is for scoring disambiguation only.
# Verified business-vocabulary questions over distinctive geographies (county,
# congressional district, state). Overlapping "area" geographies (cbsa/puma/place)
# are intentionally avoided -- table-level vector search can't separate them.
QUESTIONS = [
    ("What is the rent burden (share of household income spent on rent) by county in "
     "the 2017 5-year ACS?", "county_2017_5yr"),
    ("Rank congressional voting districts by the number of residents with a "
     "bachelor's degree, using the 2016 1-year ACS.", "congressionaldistrict_2016_1yr"),
    ("Rank U.S. states by median age (oldest first) using the 2015 5-year ACS.",
     "state_2015_5yr"),
    ("What are the 10 counties with the highest median household income in the "
     "2018 5-year ACS?", "county_2018_5yr"),
]
_MAX_Q = int(os.getenv("MAX_QUESTIONS", "0"))
if _MAX_Q:
    QUESTIONS = QUESTIONS[:_MAX_Q]

_bq = bigquery.Client(project=BILLING_PROJECT)
_TABLES_CACHE: list[str] = []


@tool
def execute_sql(sql: str) -> str:
    """Execute a BigQuery Standard SQL query and return up to 50 rows.

    Tables must be fully qualified, e.g. `bigquery-public-data.census_bureau_acs.county_2018_5yr`.
    Select only needed columns.
    """
    try:
        job = _bq.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED))
        rows = [dict(r) for r in job.result(max_results=50)]
        return f"OK. Rows: {rows}"
    except Exception as e:
        return f"ERROR: {e}"


@tool
def list_census_tables() -> str:
    """List all table names in the Census ACS dataset."""
    global _TABLES_CACHE
    if not _TABLES_CACHE:
        rows = _bq.query(
            f"SELECT table_name FROM `{TABLE_FQN}`.INFORMATION_SCHEMA.TABLES ORDER BY table_name"
        ).result()
        _TABLES_CACHE = [r["table_name"] for r in rows]
    return "Tables: " + ", ".join(_TABLES_CACHE)


@tool
def get_table_columns(table_name: str) -> str:
    """Return the column names and data types for one Census ACS table."""
    rows = _bq.query(
        f"SELECT column_name, data_type FROM `{TABLE_FQN}`.INFORMATION_SCHEMA.COLUMNS "
        f"WHERE table_name = @t ORDER BY ordinal_position",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("t", "STRING", table_name)]
        ),
    ).result()
    cols = [f"{r['column_name']} {r['data_type']}" for r in rows]
    if not cols:
        return f"No such table: {table_name}"
    return f"{table_name} ({len(cols)} columns): " + ", ".join(cols)


def system_prompt_with() -> str:
    return f"""You are a Text2SQL agent for the U.S. Census ACS dataset in BigQuery.
Tables live in `{TABLE_FQN}` and must be fully qualified as `{TABLE_FQN}.<table>`.

This dataset has hundreds of near-identical tables that differ only by geography
level, year, and survey span (e.g. county_2018_5yr vs cbsa_2017_1yr), each with
~252 columns.

MANDATORY before writing SQL:
* Call get_context_by_table_hybrid_search ONCE with the key terms from the
  question (geography + year + survey span + the measure) and set max_tables=1 so
  you retrieve only the single best-matching table and its columns.
* Do not call it repeatedly. One targeted retrieval is enough.
Then call execute_sql.

Rules:
* Never SELECT * (tables have ~252 columns). Select only what you need.
* Pick the exact geography/year/span the question asks for.
* Give a short final answer."""


def system_prompt_without() -> str:
    return f"""You are a Text2SQL agent for the U.S. Census ACS dataset in BigQuery.
Tables must be fully qualified as `{TABLE_FQN}.<table>`.

This dataset has hundreds of near-identical tables that differ only by geography
level, year, and survey span. The full schema is far too large to show at once.
Use list_census_tables to see table names and get_table_columns to inspect a
table's columns. Discover the correct table and columns, then call execute_sql.

Rules:
* Never SELECT * (tables have ~252 columns). Select only what you need.
* Pick the exact geography/year/span the question asks for.
* Give a short final answer."""


def tally(messages: list):
    tin = tout = 0
    calls: list[str] = []
    sql_tables: set[str] = set()
    for m in messages:
        um = getattr(m, "usage_metadata", None)
        if um:
            tin += um.get("input_tokens", 0)
            tout += um.get("output_tokens", 0)
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                calls.append(tc["name"])
                if tc["name"] == "execute_sql":
                    sql = tc["args"].get("sql", "")
                    sql_tables.update(re.findall(rf"{re.escape(SOURCE_DATASET)}\.([a-z0-9_]+)", sql))
    return tin, tout, calls, sql_tables


def estimate_cost(model: str, tin: int, tout: int) -> float:
    try:
        from litellm import cost_per_token

        pin, pout = cost_per_token(model=model, prompt_tokens=tin, completion_tokens=tout)
        return pin + pout
    except Exception:
        return 0.0


def make_agent(model: str, prompt: str, tools: list[BaseTool]):
    for t in tools:
        t.handle_tool_error = True
    return create_agent(
        model=ChatLiteLLM(model=model, num_retries=8, temperature=0),
        tools=tools,
        system_prompt=prompt,
        checkpointer=InMemorySaver(),
    )


async def run_once(model: str, prompt: str, tools: list[BaseTool], question: str):
    """Run one question. Returns (tin, tout, calls, sql_tables, finished).

    If the agent loops until the recursion limit (a real failure mode for
    brute-force discovery over 278 tables), we still recover the messages from
    the checkpointer so the tokens burned before giving up are counted.
    """
    from langgraph.errors import GraphRecursionError

    agent = make_agent(model, prompt, tools)
    config = {"configurable": {"thread_id": "cold"}, "recursion_limit": RECURSION_LIMIT}
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}, config=config
        )
        return (*tally(result["messages"]), True)
    except GraphRecursionError:
        state = await agent.aget_state(config)
        return (*tally(state.values.get("messages", [])), False)


async def main() -> None:
    client = MultiServerMCPClient(
        {
            "neocarta": {
                "transport": "stdio",
                "command": "uvx",
                "args": ["--from", "neocarta[mcp]@0.4.0", "neocarta-mcp"],
                "env": {
                    k: v
                    for k, v in {
                        "NEO4J_URI": os.getenv("NEO4J_URI"),
                        "NEO4J_USERNAME": os.getenv("NEO4J_USERNAME"),
                        "NEO4J_PASSWORD": os.getenv("NEO4J_PASSWORD"),
                        "NEO4J_DATABASE": NEO4J_DATABASE,
                        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
                        "EMBEDDING_MODEL": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
                        "EMBEDDING_DIMENSIONS": os.getenv("EMBEDDING_DIMENSIONS", "768"),
                    }.items()
                    if v is not None
                },
            }
        }
    )
    neocarta_tools = await client.get_tools(server_name="neocarta")
    neocarta_tools = [t for t in neocarta_tools if t.name in NEOCARTA_ALLOWED]
    tools_with = list(neocarta_tools) + [execute_sql]
    tools_without = [list_census_tables, get_table_columns, execute_sql]

    print(f"Models: {MODELS}")
    print(f"Dataset: {TABLE_FQN}  (full schema ~475K tokens -> cannot be pasted)")
    print(f"WITH tools: {len(tools_with)} | WITHOUT tools: {len(tools_without)}")
    print(f"Questions: {len(QUESTIONS)} | monthly-query projection: {MONTHLY_QUERIES:,}\n")

    summary = []
    for model in MODELS:
        print(f"\n################  MODEL: {model}  ################")
        print(f"{'mode':<8}{'in':>8}{'out':>6}{'total':>8}{'$/q':>9} {'hit':>4}  tables_queried / #tools")
        print("-" * 110)
        agg = {"with": [0, 0, 0.0, 0], "without": [0, 0, 0.0, 0]}
        for q, expected in QUESTIONS:
            print(f"Q: {q[:96]}")
            for label, prompt, tools, key in (
                ("WITH", system_prompt_with(), tools_with, "with"),
                ("WITHOUT", system_prompt_without(), tools_without, "without"),
            ):
                tin, tout, calls, tables, finished = await run_once(model, prompt, tools, q)
                cost = estimate_cost(model, tin, tout)
                hit = finished and expected in tables
                agg[key][0] += tin
                agg[key][1] += tout
                agg[key][2] += cost
                agg[key][3] += int(hit)
                status = "YES" if hit else ("no" if finished else "DNF")
                print(
                    f"  {label:<6}{tin:>8}{tout:>6}{tin + tout:>8}{'$' + format(cost, '.4f'):>9} "
                    f"{status:>4}  {sorted(tables)} / {len(calls)} tools"
                )
            print("-" * 110)

        n = len(QUESTIONS)
        tok_w = (agg["with"][0] + agg["with"][1]) / n
        tok_wo = (agg["without"][0] + agg["without"][1]) / n
        cost_w, cost_wo = agg["with"][2] / n, agg["without"][2] / n
        tok_save = 100 * (tok_wo - tok_w) / tok_wo if tok_wo else 0
        cost_save = 100 * (cost_wo - cost_w) / cost_wo if cost_wo else 0
        monthly_saved = (cost_wo - cost_w) * MONTHLY_QUERIES
        summary.append((model, tok_w, tok_wo, tok_save, cost_w, cost_wo, cost_save,
                        monthly_saved, agg["with"][3], agg["without"][3], n))
        print(
            f"  AVG/q  WITH {tok_w:,.0f} tok ${cost_w:.4f} (correct table {agg['with'][3]}/{n})  |  "
            f"WITHOUT {tok_wo:,.0f} tok ${cost_wo:.4f} (correct table {agg['without'][3]}/{n})  |  "
            f"tokens -{tok_save:.0f}%  cost -{cost_save:.0f}%"
        )

    print("\n\n================  SUMMARY (avg per question)  ================")
    print(f"{'Model':<14}{'tokW':>9}{'tokW/O':>9}{'tok-':>6}{'$W':>9}{'$W/O':>9}{'$-':>5}"
          f"{'hitW':>6}{'hitW/O':>7}{'  $/mo saved @' + format(MONTHLY_QUERIES, ',')}")
    for m, tw, two, ts, cw, cwo, cs, ms, hw, hwo, n in summary:
        print(f"{m:<14}{tw:>9,.0f}{two:>9,.0f}{ts:>5.0f}%{cw:>9.4f}{cwo:>9.4f}{cs:>4.0f}%"
              f"{hw:>4}/{n}{hwo:>5}/{n}{ms:>16,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
