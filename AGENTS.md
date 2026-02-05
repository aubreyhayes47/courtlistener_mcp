# AGENTS.md

## Project overview
This repository hosts a Python MCP (Model Context Protocol) server that wraps CourtListener v4 REST APIs. The server is implemented in `courtlistener_server.py` and exposes tools for search, cluster retrieval, opinion retrieval, and URL resolution. The service is intended to run as a standalone HTTP MCP server (Render-friendly).

The server also includes a `courts_db`-powered court resolution tool to map human court strings to CourtListener `court_id` codes, which can then be used to filter search results.

In addition to accepting explicit `court_id` values, the search tool also supports `court_query` (a human court string) that is resolved via `courts_db` and applied as a CourtListener search filter.

## Key architecture notes
- **Single async client:** The server uses a lazily-created shared `httpx.AsyncClient` with retries, timeouts, and a consistent auth header.
- **CourtListener v4 only:** All endpoints are `/api/rest/v4/...`.
- **Tooling:** The MCP surface is defined with `FastMCP` and `@mcp.tool()` functions.
- **Entry point:** `courtlistener_server.py` runs the MCP server when executed directly.

## How to run (local)
```bash
pip install -r requirements.txt
export COURTLISTENER_API_TOKEN=your_token
python courtlistener_server.py
```
The MCP JSON-RPC endpoint is `/mcp` on the configured port.

## Development guidelines (prompt engineering + implementation)
When modifying this repo, follow these LLM-oriented best practices:

### 1. Be explicit about tool contracts
- **Describe inputs/outputs** for any new MCP tools. Make schemas explicit in docstrings and ensure parameter validation is strict.
- **Normalize responses** with stable keys so downstream agents can rely on them.

### 2. Keep context small and reliable
- Prefer **field selection** and explicit filtering over returning huge payloads.
- Preserve the `raw` response when useful, but always return a normalized summary.

### 3. Defensive API usage
- Always use **v4 endpoints**; avoid v3 or undocumented URLs.
- Keep the **shared AsyncClient** pattern; avoid per-call clients.
- Maintain consistent **timeouts, retry rules, and error messages**.

### 4. Error handling and logging
- Raise actionable exceptions with clear, short messages.
- Use structured logging when adding new logs (`logger.warning(..., extra={...})`).

### 5. Testing & validation
- If adding new behavior, include or update tests where feasible.
- Favor small, deterministic unit tests over integration tests (external API access may be limited).

## Conventions
- Python 3.10+ style.
- Keep functions cohesive and well-documented.
- Do not introduce try/catch around imports.

## Deployment considerations
- `COURTLISTENER_API_TOKEN` **must** be provided.
- `PORT` controls the HTTP listener.

## Suggested prompts for MCP clients
- "Search for case law in the 9th Circuit about fair use."
- "Resolve this CourtListener URL and return the opinions."
- "Fetch the full text of opinion ID 12345 as plain text."

---
If you introduce new tools or change existing ones, update both this file and the README so that downstream users understand the contract.
