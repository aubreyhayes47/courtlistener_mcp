from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("courtlistener")

API_BASE = "https://www.courtlistener.com/api/rest/v4"
SEARCH_ENDPOINT = f"{API_BASE}/search/"
CLUSTERS_ENDPOINT = f"{API_BASE}/clusters"
OPINIONS_ENDPOINT = f"{API_BASE}/opinions"

API_TOKEN = os.environ.get("COURTLISTENER_API_TOKEN")
if not API_TOKEN:
    raise RuntimeError("Missing COURTLISTENER_API_TOKEN environment variable")

DEFAULT_TIMEOUT = httpx.Timeout(20.0)
HEADERS = {
    "Authorization": f"Token {API_TOKEN}",
    "Accept": "application/json",
}

# Shared async client. Create once per process.
client = httpx.AsyncClient(headers=HEADERS, timeout=DEFAULT_TIMEOUT)


def _extract_next_cursor(next_url: Optional[str]) -> Optional[str]:
    """Extract cursor token from a next-page URL."""
    if not next_url:
        return None
    m = re.search(r"[?&]cursor=([^&]+)", next_url)
    return m.group(1) if m else None


def _approximate_count_flag(result_type: str, count: int) -> bool:
    """Approximate counts apply for certain types over 2000 results."""
    return result_type in {"d", "r"} and count > 2000


async def _get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """GET JSON with consistent error messages."""
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"CourtListener HTTP error {e.response.status_code}: {e.response.text[:500]}")
    except httpx.RequestError as e:
        raise RuntimeError(f"CourtListener request error: {str(e)}")


async def _post_json(url: str, json_body: Dict[str, Any], params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """POST JSON with consistent error messages."""
    try:
        resp = await client.post(url, params=params, json=json_body)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"CourtListener HTTP error {e.response.status_code}: {e.response.text[:500]}")
    except httpx.RequestError as e:
        raise RuntimeError(f"CourtListener request error: {str(e)}")


def _courts_param(courts: Optional[List[str]]) -> Optional[str]:
    """CourtListener search supports multiple courts. Join with '+' like the front-end."""
    if not courts:
        return None
    cleaned = [c.strip() for c in courts if c and c.strip()]
    return "+".join(cleaned) if cleaned else None


@mcp.tool()
async def courtlistener_search(
    query: str,
    type: str = "o",
    courts: Optional[List[str]] = None,
    semantic: bool = False,
    order_by: Optional[str] = None,
    highlight: bool = False,
    limit: int = 10,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Search CourtListener via the v4 Search API."""
    if not query or not query.strip():
        raise ValueError("query is required")

    if type not in {"o", "r", "rd", "d", "p", "oa"}:
        raise ValueError("type must be one of: o, r, rd, d, p, oa")

    if semantic and type != "o":
        raise ValueError("semantic search is only available for type='o' (case law)")

    if limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")

    params: Dict[str, Any] = {
        "q": query,
        "type": type,
        "format": "json",
    }

    courts_joined = _courts_param(courts)
    if courts_joined:
        params["court"] = courts_joined

    if semantic:
        params["semantic"] = "true"

    if order_by:
        params["order_by"] = order_by

    if highlight:
        params["highlight"] = "on"

    if cursor:
        params["cursor"] = cursor

    raw = await _get_json(SEARCH_ENDPOINT, params=params)

    count = int(raw.get("count", 0))
    next_cursor = _extract_next_cursor(raw.get("next"))
    approximate = _approximate_count_flag(type, count)

    normalized_results: List[Dict[str, Any]] = []
    for item in (raw.get("results") or [])[:limit]:
        meta = item.get("meta") or {}
        score = None
        if isinstance(meta, dict):
            score_obj = meta.get("score")
            if isinstance(score_obj, dict):
                score = score_obj.get("bm25")

        normalized_results.append(
            {
                "title": item.get("caseName") or item.get("name") or item.get("docketNumber") or "(unknown)",
                "cluster_id": item.get("cluster_id"),
                "docket_id": item.get("docket_id"),
                "court": item.get("court"),
                "court_id": item.get("court_id"),
                "date_filed": item.get("dateFiled"),
                "url": ("https://www.courtlistener.com" + item["absolute_url"]) if item.get("absolute_url") else None,
                "citation": item.get("citation"),
                "snippet": item.get("snippet") or None,
                "score": score,
                "raw": item,
            }
        )

    return {
        "count": count,
        "approximate": approximate,
        "next_cursor": next_cursor,
        "results": normalized_results,
    }


@mcp.tool()
async def courtlistener_get_opinion(
    opinion_id: int,
    text_format: str = "html_with_citations",
) -> Dict[str, Any]:
    """Retrieve an opinion document by opinion ID."""
    if text_format not in {"html_with_citations", "plain_text"}:
        raise ValueError("text_format must be 'html_with_citations' or 'plain_text'")

    fields = [
        "id",
        "cluster",
        "type",
        "author_str",
        "per_curiam",
        "joined_by_str",
        text_format,
        "download_url",
        "local_path",
        "opinions_cited",
        "date_created",
        "date_modified",
    ]

    op_url = f"{OPINIONS_ENDPOINT}/{opinion_id}/"
    raw = await _get_json(op_url, params={"fields": ",".join(fields)})

    return {
        "opinion_id": raw.get("id"),
        "cluster": raw.get("cluster"),
        "type": raw.get("type"),
        "author": raw.get("author_str"),
        "per_curiam": raw.get("per_curiam"),
        "joined_by": raw.get("joined_by_str"),
        "text_format": text_format,
        "text": raw.get(text_format) or "",
        "download_url": raw.get("download_url"),
        "local_path": raw.get("local_path"),
        "opinions_cited": raw.get("opinions_cited") or [],
        "raw": raw,
    }


@mcp.tool()
async def courtlistener_get_cluster(
    cluster_id: int,
    include_opinions: bool = True,
    opinion_text_format: str = "html_with_citations",
) -> Dict[str, Any]:
    """Retrieve a cluster (case) by cluster ID."""
    if opinion_text_format not in {"html_with_citations", "plain_text"}:
        raise ValueError("opinion_text_format must be 'html_with_citations' or 'plain_text'")

    cluster_url = f"{CLUSTERS_ENDPOINT}/{cluster_id}/"
    cluster = await _get_json(cluster_url)

    result: Dict[str, Any] = {
        "cluster_id": cluster.get("id"),
        "absolute_url": cluster.get("absolute_url"),
        "url": ("https://www.courtlistener.com" + cluster["absolute_url"]) if cluster.get("absolute_url") else None,
        "case_name": cluster.get("case_name"),
        "case_name_full": cluster.get("case_name_full"),
        "docket": cluster.get("docket"),
        "court": None,
        "court_id": cluster.get("court_id"),
        "date_filed": cluster.get("date_filed"),
        "citations": cluster.get("citations"),
        "sub_opinions": cluster.get("sub_opinions") or [],
        "opinions": [],
        "raw": cluster,
    }

    if include_opinions:
        opinions: List[Dict[str, Any]] = []
        for op_uri in result["sub_opinions"]:
            m = re.search(r"/opinions/(\d+)/", str(op_uri))
            if not m:
                continue
            op_id = int(m.group(1))
            op = await courtlistener_get_opinion(opinion_id=op_id, text_format=opinion_text_format)
            opinions.append(op)
        result["opinions"] = opinions

    return result


@mcp.tool()
async def courtlistener_resolve_from_url(
    url: str,
    include_opinions: bool = True,
    opinion_text_format: str = "html_with_citations",
) -> Dict[str, Any]:
    """Resolve a CourtListener website URL to cluster/opinion data."""
    if not url or not url.strip():
        raise ValueError("url is required")

    m = re.search(r"/opinion/(\d+)/", url)
    if not m:
        raise ValueError("Unsupported URL format. Expected a CourtListener /opinion/<cluster_id>/ URL.")

    cluster_id = int(m.group(1))
    cluster = await courtlistener_get_cluster(
        cluster_id=cluster_id,
        include_opinions=include_opinions,
        opinion_text_format=opinion_text_format,
    )

    return {
        "resolved_type": "cluster",
        "cluster_id": cluster_id,
        "result": cluster,
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    mcp.run(transport="http", host="0.0.0.0", port=port)
