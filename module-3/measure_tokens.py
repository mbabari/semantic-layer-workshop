"""Measure token usage WITH vs WITHOUT the neocarta semantic layer.

Runs the same question(s) through two agents and compares token usage, tool
calls, and estimated cost:

  * WITH    semantic layer -> tools = neocarta context tools + BigQuery execute_sql
  * WITHOUT semantic layer -> tools = all BigQuery tools (execute_sql + schema
                              discovery), no neocarta. The agent must discover
                              schema by brute force instead of semantic retrieval.

Usage:
    uv run module-3/measure_tokens.py
    uv run module-3/measure_tokens.py "Your own question here"
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from langchain_core.messages import AIMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

from langchain.agents import create_agent
from langchain.tools import BaseTool
from langchain_litellm import ChatLiteLLM
from langgraph.checkpoint.memory import InMemorySaver

load_dotenv()

# Same instructions as the real agent so the comparison is apples-to-apples.
_PROJECT = os.getenv("GCP_PROJECT_ID", "")
_DATASET = os.getenv("BIGQUERY_DATASET_ID", "acme_corp")

SYSTEM_PROMPT = f"""You are a Text2SQL agent that answers questions about our BigQuery dataset.

The GCP project is `{_PROJECT}` and the dataset is `{_DATASET}`. Qualify every table
as `{_PROJECT}.{_DATASET}.<table>`.

Use whatever tools you have to discover the correct tables, columns, and join keys
before writing SQL.

Rules:
* Make sure you have the schema you need before writing a query.
* Return query results to the user in a readable format."""

DEFAULT_MODEL = "gpt-4o"

# --- MCP servers (both stdio, matching the verified Claude Desktop setup) ----
NEOCARTA = {
    "transport": "stdio",
    "command": "uvx",
    "args": ["--from", "neocarta[mcp]@0.4.0", "neocarta-mcp"],
    "env": {
        k: v
        for k, v in {
            "NEO4J_URI": os.getenv("NEO4J_URI"),
            "NEO4J_USERNAME": os.getenv("NEO4J_USERNAME"),
            "NEO4J_PASSWORD": os.getenv("NEO4J_PASSWORD"),
            "NEO4J_DATABASE": os.getenv("NEO4J_DATABASE"),
            "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
            "EMBEDDING_MODEL": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
            "EMBEDDING_DIMENSIONS": os.getenv("EMBEDDING_DIMENSIONS", "768"),
        }.items()
        if v is not None
    },
}

BIGQUERY = {
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@toolbox-sdk/server", "--prebuilt", "bigquery", "--stdio"],
    "env": {"BIGQUERY_PROJECT": os.getenv("GCP_PROJECT_ID")},
}

client = MultiServerMCPClient({"neocarta": NEOCARTA, "bigquery": BIGQUERY})

QUESTIONS = [
    "Which product line contributes the most to total ARR?",
    "Which subscriptions are renewing in the next 90 days, ranked by ARR at risk?",
    "Is there a correlation between a customer's health score and their support experience?",
]


def tally(messages: list) -> tuple[int, int, list[str]]:
    """Sum input/output tokens and collect tool-call names from a run's messages."""
    tin = tout = 0
    calls: list[str] = []
    for m in messages:
        um = getattr(m, "usage_metadata", None)
        if um:
            tin += um.get("input_tokens", 0)
            tout += um.get("output_tokens", 0)
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                calls.append(tc["name"])
    return tin, tout, calls


def estimate_cost(model: str, tin: int, tout: int) -> float | None:
    """Best-effort USD cost via LiteLLM's pricing table."""
    try:
        from litellm import cost_per_token

        pin, pout = cost_per_token(model=model, prompt_tokens=tin, completion_tokens=tout)
        return pin + pout
    except Exception:
        return None


def make_agent(model: str, tools: list[BaseTool], checkpointer: InMemorySaver):
    for t in tools:  # tool errors become observations so the agent can self-correct
        t.handle_tool_error = True
    return create_agent(
        model=ChatLiteLLM(model=model, num_retries=8),  # back off on rate limits
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )


async def run_once(model: str, tools: list[BaseTool], question: str) -> tuple[int, int, list[str]]:
    """Run one question through a FRESH agent (cold: no prior context). Returns (in, out, calls)."""
    agent = make_agent(model, tools, InMemorySaver())
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]},
            config={"configurable": {"thread_id": "cold"}, "recursion_limit": 30},
        )
    except Exception as e:  # noqa: BLE001 — one bad model/question shouldn't abort the suite
        print(f"    [error: {str(e)[:80]}]")
        return -1, 0, ["ERROR"]
    return tally(result["messages"])


async def warm_demo(model: str, tools_with: list[BaseTool]) -> None:
    """Ask two questions in the SAME thread to show whether metadata is re-fetched or
    reused from the context window."""
    print("\n=== COLD vs WARM (does it re-fetch schema each question?) — WITH neocarta ===")
    agent = make_agent(model, tools_with, InMemorySaver())
    cfg = {"configurable": {"thread_id": "warm"}, "recursion_limit": 30}
    prev = 0
    for i, q in enumerate(QUESTIONS[:2], 1):
        result = await agent.ainvoke({"messages": [{"role": "user", "content": q}]}, config=cfg)
        msgs = result["messages"]
        tin, tout, calls = tally(msgs[prev:])  # only the messages added by THIS turn
        prev = len(msgs)
        fetched = any("get_context" in c or "list_" in c or "get_table" in c for c in calls)
        print(f"  Q{i} (turn {'cold' if i == 1 else 'warm'}): in={tin:>6} out={tout:>4} "
              f"calls={calls}  metadata-tool-called={fetched}")
    print("  -> If Q2 shows no metadata tool call, the schema was reused from context")
    print("     (but note Q2 input tokens stay high: the whole transcript is re-sent each turn).")


async def main() -> None:
    models = (
        os.getenv("MODELS", "").split(",")
        if os.getenv("MODELS")
        else [os.getenv("AGENT_MODEL", DEFAULT_MODEL)]
    )
    models = [m.strip() for m in models if m.strip()]
    questions = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else QUESTIONS

    neocarta_tools = await client.get_tools(server_name="neocarta")
    bigquery_tools = await client.get_tools(server_name="bigquery")
    execute_sql = [t for t in bigquery_tools if t.name == "execute_sql"]
    # Expose ONLY targeted semantic-retrieval tools — not the full-schema-dump / list-all
    # tools (get_full_metadata_schema, list_tables_by_schema), which defeat the point at
    # scale (a single dump of a 135-table graph is 100k+ tokens).
    neocarta_ctx = [t for t in neocarta_tools if "get_context" in t.name]
    tools_with = neocarta_ctx + execute_sql               # semantic layer (targeted only)
    tools_without = list(bigquery_tools)                  # bq discovery + execute_sql

    print(f"\nModels: {models}")
    print(f"WITH    neocarta : {len(tools_with)} tools | WITHOUT : {len(tools_without)} tools")
    print(f"Questions: {len(questions)}\n")

    summary = []  # (model, avg_with, avg_without, pct, cost_with, cost_without)
    for model in models:
        print(f"\n################  MODEL: {model}  ################")
        print(f"{'Question':<48} {'in':>7} {'out':>6} {'total':>7} {'$':>9}  tools")
        print("-" * 110)
        agg = {"with": [0, 0, 0.0, 0], "without": [0, 0, 0.0, 0]}  # in, out, cost, valid_n
        for q in questions:
            for label, tools, key in (("WITH   ", tools_with, "with"),
                                       ("WITHOUT", tools_without, "without")):
                tin, tout, calls = await run_once(model, tools, q)
                if tin < 0:  # errored run — don't pollute the average
                    print(f"{(label + ' ' + q)[:47]:<48} {'ERR':>7}")
                    continue
                cost = estimate_cost(model, tin, tout)
                agg[key][0] += tin
                agg[key][1] += tout
                agg[key][2] += cost or 0
                agg[key][3] += 1
                cost_s = f"${cost:.4f}" if cost is not None else "n/a"
                print(f"{(label + ' ' + q)[:47]:<48} {tin:>7} {tout:>6} {tin + tout:>7} "
                      f"{cost_s:>9}  {len(calls)}: {', '.join(calls)}")
            print("-" * 110)

        nw, no_ = max(agg["with"][3], 1), max(agg["without"][3], 1)
        aw = (agg["with"][0] + agg["with"][1]) / nw
        ao = (agg["without"][0] + agg["without"][1]) / no_
        pct = 100 * (ao - aw) / ao if ao else 0
        cw, co = agg["with"][2] / nw, agg["without"][2] / no_
        print(f"  AVERAGE per question  WITH: {aw:,.0f} tok (~${cw:.4f})  |  "
              f"WITHOUT: {ao:,.0f} tok (~${co:.4f})  |  saving: {pct:.0f}%")
        summary.append((model, aw, ao, pct, cw, co))

    print("\n\n================  CROSS-MODEL AVERAGES (per question)  ================")
    print(f"{'Model':<22} {'WITH tok':>9} {'WITHOUT tok':>12} {'saving':>8} {'$ with':>9} {'$ without':>10}")
    for m, aw, ao, pct, cw, co in summary:
        print(f"{m:<22} {aw:>9,.0f} {ao:>12,.0f} {pct:>7.0f}% {cw:>9.4f} {co:>10.4f}")

    # Demonstrate context reuse on the first model only.
    if summary:
        await warm_demo(models[0], tools_with)


if __name__ == "__main__":
    asyncio.run(main())
