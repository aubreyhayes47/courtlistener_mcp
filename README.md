# Deploying a CourtListener MCP Server on Render

This guide walks through creating and deploying a Python MCP server that speaks to the CourtListener v4 REST APIs and exposes four reliable tools:

- `courtlistener.search` — search across case law / PACER / judges / oral arguments (Citegeist search API)
- `courtlistener.get_cluster` — retrieve a case (cluster) by cluster ID (the ID in CourtListener opinion URLs)
- `courtlistener.get_opinion` — retrieve a specific opinion document by opinion ID (full text)
- `courtlistener.resolve_from_url` — paste a CourtListener URL and get back the underlying cluster + opinions

You will deploy the MCP server to Render using HTTP transport so multiple clients can connect.

## Prerequisites

**Requirements**

- Python 3.10+
- MCP Python SDK (FastMCP)
- `httpx` for HTTP requests

Install dependencies locally:

```bash
pip install "mcp[cli]" httpx
```

### CourtListener API Token

Create or copy a token from your CourtListener account (Profile → API). All requests must include:

```
Authorization: Token <YOUR_TOKEN>
```

Render best practice: store the token as an environment variable named `COURTLISTENER_API_TOKEN`.

## CourtListener API Model (What You’re Integrating)

CourtListener exposes two styles of API that matter here:

1. **Search API (search engine-backed)**  
   - Endpoint: `/api/rest/v4/search/`  
   - Used for discovery and ranking  
   - Supports keyword search (default) and semantic search (case law only)  
   - Uses cursor pagination
2. **Case Law REST APIs (database-backed)**  
   - Clusters: `/api/rest/v4/clusters/` (case/grouping used in CourtListener URLs)  
   - Opinions: `/api/rest/v4/opinions/` (individual decision text; lead/concurrence/dissent)  
   - Dockets/Courts exist too, but the four-tool baseline focuses on Search + Clusters + Opinions.

**Critical distinction:** CourtListener website URLs include a cluster ID (not opinion ID). Opinion IDs do not reliably match cluster IDs.

## Project Structure

Recommended minimal structure (already present in this repo):

```
courtlistener-mcp/
  courtlistener_server.py
  requirements.txt
  README.md
```

`requirements.txt`:

```
mcp[cli]
httpx
```

## Implementation: The MCP Server (All 4 Tools)

The server is implemented in `courtlistener_server.py`. Key reliability goals:

- One shared `httpx.AsyncClient`
- One request helper that adds auth, timeouts, and errors consistently
- Uses CourtListener v4 endpoints everywhere
- Uses field selection on opinion fetches to keep payloads small

The file defines four tools:

### 1) `courtlistener.search`

Search via `/api/rest/v4/search/` across multiple corpora. Returns a normalized, agent-friendly list (default limit = 10) and a `next_cursor`.

**Input contract**

- `query` is required
- `type` selects what you’re searching
- `courts` is an optional list of `court_id` codes
- `semantic` only applies to `type="o"`
- `highlight` toggles snippet highlighting
- `cursor` supports pagination

**Notes**

- For `type="o"` results, the reliable identifier is typically `cluster_id`.
- The tool returns `raw` per item for robustness; remove if you want a stricter schema.

### 2) `courtlistener.get_cluster`

Fetch a cluster by ID. Optionally fetch all sub-opinion documents.

### 3) `courtlistener.get_opinion`

Fetch an opinion document by opinion ID, returning text in the preferred format. Uses `fields=` to omit unneeded fields.

### 4) `courtlistener.resolve_from_url`

Accepts a CourtListener URL like `https://www.courtlistener.com/opinion/2812209/obergefell-v-hodges/` and returns the canonical cluster (and optionally the opinion docs).

## Running the Server (HTTP Transport for Render)

Add the entry point (already in `courtlistener_server.py`):

```python
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    mcp.run(transport="http", host="0.0.0.0", port=port)
```

For HTTP transport, FastMCP exposes the JSON-RPC endpoint at `/mcp`.

## Deployment to Render

1. **Push to Git.** Push this project to GitHub (or another git host).  
2. **Create a Render Web Service.**  
   - Environment: Python  
   - Build command: Render auto-installs from `requirements.txt` (or set `pip install -r requirements.txt`)  
   - Start command:  
     ```bash
     python courtlistener_server.py
     ```
3. **Environment Variables.** Set on Render:  
   - `COURTLISTENER_API_TOKEN` = your token  
   - Render automatically sets `PORT`
4. **Verify.** After deployment, your MCP endpoint will be:  
   ```
   https://<your-service>.onrender.com/mcp
   ```

## Using the MCP Server from an Agent

Once registered with an MCP-capable client, agents can call:

**Search for cases (10 results)**

```json
{
  "query": "breach of warranty",
  "type": "o",
  "courts": ["ca5"],
  "limit": 10
}
```

**Follow a result to retrieve full text**

```json
{
  "cluster_id": 2812209,
  "include_opinions": true,
  "opinion_text_format": "html_with_citations"
}
```

> Note: `include_opinions` defaults to `false` to avoid long fan-out requests when clusters contain many sub-opinions.

**Retrieve a specific opinion document**

```json
{
  "opinion_id": 9969234,
  "text_format": "html_with_citations"
}
```

**Paste a URL**

```json
{
  "url": "https://www.courtlistener.com/opinion/2812209/obergefell-v-hodges/",
  "include_opinions": true
}
```

## Reliability Checklist (Why This Implementation Works)

- Correct API versions: everything uses `/api/rest/v4/...` (no v3 endpoints).
- Correct identifiers: search returns clusters; website URLs contain cluster IDs; opinions fetched by opinion IDs.
- Consistent HTTP behavior: client is created lazily with consistent timeouts and closed via the provided helper.
- Consistent output: all tools return normalized JSON objects (not fragile formatted strings).
- Field selection: opinion fetch uses `fields=` to reduce payload size.
- Pagination support: search returns `next_cursor` for agents to continue.

## Notes on Logging

If you add logging, use Python’s `logging` and write to stderr. For HTTP mode on Render, stdout is acceptable, but structured logging is preferred.
