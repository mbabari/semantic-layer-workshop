"""Benchmark token usage / cost: WITH vs WITHOUT the Neocarta semantic layer.

Use case: the StackOverflow public dataset (16 tables, 10 near-identical
`posts_*` tables, 228 columns, no descriptions). This is where a naive agent
that gets the whole schema dumped into its prompt burns tokens and confuses the
`posts_*` tables -- and where the semantic layer's targeted retrieval wins.

Two agents answer the same questions:
  * WITH    -> Neocarta MCP retrieval tools (against the `spider2` Neo4j db)
               + a SQL executor. The schema is NOT in the prompt.
  * WITHOUT -> the full raw schema (every table + column) pasted into the system
               prompt + the same SQL executor. No retrieval.

We measure input/output tokens, estimated USD cost (LiteLLM pricing), tool-call
count, and project monthly savings at a chosen query volume. Runs across one or
more models so you can show the weak-model accuracy story and the strong-model
dollar story.

Usage (from repo root):
    ./.venv/bin/python module-3/benchmark_stackoverflow.py
    MODELS=gpt-4o-mini,gpt-4o MONTHLY_QUERIES=10000 ./.venv/bin/python module-3/benchmark_stackoverflow.py
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import bigquery
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from neo4j import GraphDatabase

from langchain.agents import create_agent
from langchain.tools import BaseTool
from langchain_litellm import ChatLiteLLM
from langgraph.checkpoint.memory import InMemorySaver

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)

SOURCE_PROJECT = os.getenv("STACKOVERFLOW_SOURCE_PROJECT", "bigquery-public-data")
SOURCE_DATASET = os.getenv("STACKOVERFLOW_SOURCE_DATASET", "stackoverflow")
NEO4J_DATABASE = os.getenv("SPIDER2_NEO4J_DATABASE", "spider2")
BILLING_PROJECT = os.getenv("GCP_PROJECT_ID")

# Safety cap so a runaway SELECT * on a 40 GB table errors instead of scanning.
MAX_BYTES_BILLED = int(os.getenv("MAX_BYTES_BILLED", str(15 * 10**9)))  # 15 GB
RECURSION_LIMIT = int(os.getenv("RECURSION_LIMIT", "30"))
MONTHLY_QUERIES = int(os.getenv("MONTHLY_QUERIES", "10000"))

MODELS = [
    m.strip()
    for m in os.getenv("MODELS", "gpt-4o-mini,gpt-4o").split(",")
    if m.strip()
]

QUESTIONS = [
    "What are the top 10 tags by number of questions?",
    "Which 5 users have the highest reputation, and how many badges does each have?",
    "What is the average score of questions grouped by the year they were created?",
    "Who are the top 5 users by number of accepted answers to questions?",
]
# Smoke-test knob: run only the first N questions.
_MAX_Q = int(os.getenv("MAX_QUESTIONS", "0"))
if _MAX_Q:
    QUESTIONS = QUESTIONS[:_MAX_Q]

TABLE_FQN = f"{SOURCE_PROJECT}.{SOURCE_DATASET}"

_bq = bigquery.Client(project=BILLING_PROJECT)


@tool
def execute_sql(sql: str) -> str:
    """Execute a BigQuery Standard SQL query and return up to 50 result rows.

    Tables must be fully qualified, e.g. `bigquery-public-data.stackoverflow.users`.
    Avoid SELECT * — select only needed columns to keep scans small.
    """
    try:
        job = _bq.query(
            sql,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED),
        )
        rows = [dict(r) for r in job.result(max_results=50)]
        gb = (job.total_bytes_billed or 0) / 1e9
        return f"OK ({gb:.2f} GB billed). Rows: {rows}"
    except Exception as e:  # surfaced to the agent so it can self-correct
        return f"ERROR: {e}"


def build_schema_dump() -> str:
    """Render the full raw StackOverflow schema (the 'paste the schema' baseline)."""
    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI"),
        auth=(os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD")),
    )
    lines: list[str] = []
    with driver.session(database=NEO4J_DATABASE) as session:
        rows = session.run(
            """
            MATCH (s:Schema {name: $schema})-[:HAS_TABLE]->(t:Table)-[:HAS_COLUMN]->(c:Column)
            RETURN t.name AS table, collect(c.name + ' ' + coalesce(c.type, '')) AS cols
            ORDER BY table
            """,
            schema=SOURCE_DATASET,
        )
        for r in rows:
            cols = ", ".join(sorted(r["cols"]))
            lines.append(f"- `{TABLE_FQN}.{r['table']}` ({cols})")
    driver.close()
    return "\n".join(lines)


def system_prompt_with() -> str:
    return f"""You are a Text2SQL agent for the StackOverflow dataset in BigQuery.
Tables live in `{TABLE_FQN}` and must be fully qualified as `{TABLE_FQN}.<table>`.

MANDATORY: before writing SQL, call the metadata/context tools (e.g.
get_context_by_table_hybrid_search) with the key terms from the question to find
the correct tables, columns, and join keys. Only then call execute_sql.

Rules:
* Never SELECT * — select only the columns you need (the tables are large).
* Resolve human-readable names (e.g. users.display_name), not raw ids.
* Give a short final answer with the result."""


def system_prompt_without(schema_dump: str) -> str:
    return f"""You are a Text2SQL agent for the StackOverflow dataset in BigQuery.
Tables must be fully qualified as `{TABLE_FQN}.<table>`.

Here is the full database schema:
{schema_dump}

Rules:
* Never SELECT * — select only the columns you need (the tables are large).
* Resolve human-readable names (e.g. users.display_name), not raw ids.
* Give a short final answer with the result."""


def tally(messages: list) -> tuple[int, int, list[str]]:
    """Sum input/output tokens and collect tool-call names."""
    tin = tout = 0
    calls: list[str] = []
    for m in messages:
        um = getattr(m, "usage_metadata", None)
        if um:
            tin += um.get("input_tokens", 0)
            tout += um.get("output_tokens", 0)
        if isinstance(m, AIMessage):
            calls.extend(tc["name"] for tc in (m.tool_calls or []))
    return tin, tout, calls


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
    agent = make_agent(model, prompt, tools)
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"configurable": {"thread_id": "cold"}, "recursion_limit": RECURSION_LIMIT},
    )
    return tally(result["messages"])


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
    tools_with = list(neocarta_tools) + [execute_sql]
    tools_without = [execute_sql]

    schema_dump = build_schema_dump()
    prompt_with = system_prompt_with()
    prompt_without = system_prompt_without(schema_dump)

    print(f"Models: {MODELS}")
    print(f"WITH tools: {len(tools_with)} | WITHOUT tools: {len(tools_without)}")
    print(f"Schema-dump size (WITHOUT, chars): {len(schema_dump)}")
    print(f"Questions: {len(QUESTIONS)} | monthly-query projection: {MONTHLY_QUERIES:,}\n")

    summary = []
    for model in MODELS:
        print(f"\n################  MODEL: {model}  ################")
        print(f"{'mode  question':<58} {'in':>7} {'out':>5} {'total':>7} {'$/q':>9}  tools")
        print("-" * 120)
        agg = {"with": [0, 0, 0.0], "without": [0, 0, 0.0]}
        for q in QUESTIONS:
            for label, prompt, tools, key in (
                ("WITH   ", prompt_with, tools_with, "with"),
                ("WITHOUT", prompt_without, tools_without, "without"),
            ):
                tin, tout, calls = await run_once(model, prompt, tools, q)
                cost = estimate_cost(model, tin, tout)
                agg[key][0] += tin
                agg[key][1] += tout
                agg[key][2] += cost
                print(
                    f"{(label + ' ' + q)[:57]:<58} {tin:>7} {tout:>5} {tin + tout:>7} "
                    f"{'$' + format(cost, '.4f'):>9}  {len(calls)}"
                )
            print("-" * 120)

        n = len(QUESTIONS)
        tok_w = (agg["with"][0] + agg["with"][1]) / n
        tok_wo = (agg["without"][0] + agg["without"][1]) / n
        cost_w = agg["with"][2] / n
        cost_wo = agg["without"][2] / n
        tok_save = 100 * (tok_wo - tok_w) / tok_wo if tok_wo else 0
        cost_save = 100 * (cost_wo - cost_w) / cost_wo if cost_wo else 0
        monthly_saved = (cost_wo - cost_w) * MONTHLY_QUERIES
        summary.append((model, tok_w, tok_wo, tok_save, cost_w, cost_wo, cost_save, monthly_saved))
        print(
            f"  AVG/q  WITH: {tok_w:,.0f} tok (${cost_w:.4f})  |  "
            f"WITHOUT: {tok_wo:,.0f} tok (${cost_wo:.4f})  |  "
            f"tokens -{tok_save:.0f}%  cost -{cost_save:.0f}%"
        )

    print("\n\n================  SUMMARY (avg per question)  ================")
    print(
        f"{'Model':<16}{'tok WITH':>10}{'tok W/O':>10}{'tok save':>9}"
        f"{'$ WITH':>9}{'$ W/O':>9}{'$ save':>8}{'  $/mo saved @ ' + format(MONTHLY_QUERIES, ',')}"
    )
    for m, tw, two, ts, cw, cwo, cs, ms in summary:
        print(
            f"{m:<16}{tw:>10,.0f}{two:>10,.0f}{ts:>8.0f}%"
            f"{cw:>9.4f}{cwo:>9.4f}{cs:>7.0f}%{ms:>15,.2f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
