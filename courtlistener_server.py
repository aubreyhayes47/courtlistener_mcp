from __future__ import annotations

import asyncio
import atexit
from datetime import datetime
import logging
import os
import re
from typing import Any, Dict, List, Optional

import courts_db
import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

port = int(os.environ.get("PORT", "10000"))

mcp = FastMCP("courtlistener", host="0.0.0.0", port=port)

API_BASE = "https://www.courtlistener.com/api/rest/v4"
SEARCH_ENDPOINT = f"{API_BASE}/search/"
CLUSTERS_ENDPOINT = f"{API_BASE}/clusters"
OPINIONS_ENDPOINT = f"{API_BASE}/opinions"

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
CLIENT_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=5)
USER_AGENT = "courtlistener-mcp/1.0 (+https://www.courtlistener.com/)"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_REQUEST_RETRIES = 3

# Lazily created shared async client to avoid import-time side effects.
client: Optional[httpx.AsyncClient] = None

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@mcp.custom_route("/", methods=["GET", "HEAD"], name="root")
async def root(request: Request) -> Response:
    """Lightweight health check endpoint for platform probes."""
    return JSONResponse(
        {
            "status": "ok",
            "service": "courtlistener-mcp",
            "mcp_endpoint": "/mcp",
        }
    )


def _get_api_token() -> str:
    token = os.environ.get("COURTLISTENER_API_TOKEN")
    if not token:
        raise RuntimeError("Missing COURTLISTENER_API_TOKEN environment variable")
    cleaned = token.strip()
    if not cleaned:
        raise RuntimeError("COURTLISTENER_API_TOKEN is empty or whitespace only")
    return cleaned


async def _get_client() -> httpx.AsyncClient:
    """Create (lazily) and return a shared AsyncClient."""
    global client
    if client is None:
        token = _get_api_token()
        headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        client = httpx.AsyncClient(
            headers=headers, timeout=DEFAULT_TIMEOUT, limits=CLIENT_LIMITS
        )
    return client


async def aclose_client() -> None:
    """Close the shared AsyncClient if it exists."""
    global client
    if client is not None:
        await client.aclose()
        client = None


def _schedule_client_close() -> None:
    """Best-effort cleanup for environments that import the module."""
    if client is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(aclose_client())
    else:
        loop.create_task(aclose_client())


atexit.register(_schedule_client_close)


def _extract_next_cursor(next_url: Optional[str]) -> Optional[str]:
    """Extract cursor token from a next-page URL."""
    if not next_url:
        return None
    m = re.search(r"[?&]cursor=([^&]+)", next_url)
    return m.group(1) if m else None


def _approximate_count_flag(result_type: str, count: int) -> bool:
    """Approximate counts apply for certain types over 2000 results."""
    return result_type in {"d", "r", "rd"} and count > 2000


async def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Make an HTTP request with retries, consistent errors, and structured logging.

    Retries are limited to known-transient errors to avoid duplicating state-changing requests.
    """

    client_instance = await _get_client()
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_REQUEST_RETRIES + 1):
        try:
            resp = await client_instance.request(
                method, url, params=params, json=json_body
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            should_retry = (
                status in RETRYABLE_STATUS_CODES and attempt < MAX_REQUEST_RETRIES
            )
            log_data = {
                "status": status,
                "url": str(e.request.url),
                "attempt": attempt,
                "retry": should_retry,
            }
            logger.warning("CourtListener HTTP error", extra=log_data)
            if should_retry:
                await asyncio.sleep(0.5 * attempt)
                last_error = e
                continue
            raise RuntimeError(
                f"CourtListener HTTP error {status}: {e.response.text[:500]}"
            )
        except httpx.RequestError as e:
            should_retry = attempt < MAX_REQUEST_RETRIES
            logger.warning(
                "CourtListener request error",
                extra={"attempt": attempt, "retry": should_retry, "url": url},
            )
            if should_retry:
                await asyncio.sleep(0.5 * attempt)
                last_error = e
                continue
            raise RuntimeError(f"CourtListener request error: {str(e)}")

    # If we exit the loop without returning, surface the last error context.
    if last_error:
        raise RuntimeError(f"CourtListener request failed after retries: {last_error}")
    raise RuntimeError("CourtListener request failed for unknown reasons")


async def _get_json(
    url: str, params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """GET JSON with consistent error messages and retry behavior."""
    return await _request_json("GET", url, params=params)


async def _post_json(
    url: str, json_body: Dict[str, Any], params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """POST JSON with consistent error messages and retry behavior."""
    return await _request_json("POST", url, params=params, json_body=json_body)


def _courts_param(courts: Optional[List[str]]) -> Optional[str]:
    """CourtListener search supports multiple courts. Join with '+' like the front-end."""
    if not courts:
        return None
    cleaned = [c.strip() for c in courts if c and c.strip()]
    return "+".join(cleaned) if cleaned else None


def _parse_date_found(date_found: Optional[str]) -> Optional[datetime]:
    """Parse an ISO date/datetime string for courts_db filtering.

    Accepts:
    - YYYY-MM-DD
    - full ISO 8601 datetimes (optionally with trailing 'Z')
    """

    if date_found is None:
        return None
    cleaned = date_found.strip()
    if not cleaned:
        return None

    # Common case: date-only string.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        try:
            return datetime.strptime(cleaned, "%Y-%m-%d")
        except ValueError:
            raise ValueError("date_found must be a valid ISO date like 'YYYY-MM-DD'")

    # More flexible: ISO datetime. Normalize trailing Z.
    try:
        normalized = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
        return datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(
            "date_found must be ISO 8601 (e.g., 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS')"
        )


@mcp.tool()
async def courtlistener_find_court(
    query: str,
    bankruptcy: Optional[bool] = None,
    date_found: Optional[str] = None,
    include_records: bool = False,
) -> Dict[str, Any]:
    """Resolve a court name/string to CourtListener court_id codes.

    This uses the local `courts_db` dataset (maintained by Free Law Project) to map
    messy court strings to one or more CourtListener court identifiers.

    Args:
        query: CourtListener court identifier or a human court string.
        bankruptcy: Optional bankruptcy filter for ambiguous district courts.
        date_found: Optional ISO date/datetime string used to disambiguate historical courts.
        include_records: When true, return courts_db records for each resolved id.

    Returns:
        Dict with stable keys: court_ids, ambiguous, and (optionally) records_by_id.
    """

    if not query or not query.strip():
        raise ValueError("query is required")

    dt_found = _parse_date_found(date_found)

    kwargs: Dict[str, Any] = {}
    if bankruptcy is not None:
        kwargs["bankruptcy"] = bankruptcy
    if dt_found is not None:
        kwargs["date_found"] = dt_found

    court_ids = courts_db.find_court(query.strip(), **kwargs)
    if not isinstance(court_ids, list):
        court_ids = list(court_ids or [])

    result: Dict[str, Any] = {
        "query": query,
        "bankruptcy": bankruptcy,
        "date_found": date_found,
        "court_ids": court_ids,
        "ambiguous": len(court_ids) > 1,
    }

    if include_records:
        records_by_id: Dict[str, Any] = {}
        for court_id in court_ids:
            try:
                records_by_id[court_id] = courts_db.find_court_by_id(court_id)
            except Exception as e:
                records_by_id[court_id] = {"error": str(e)}
        result["records_by_id"] = records_by_id

    return result


@mcp.tool()
async def courtlistener_search(
    query: str,
    type: str = "o",
    courts: Optional[List[str]] = None,
    court_query: Optional[str] = None,
    court_bankruptcy: Optional[bool] = None,
    court_date_found: Optional[str] = None,
    semantic: bool = False,
    order_by: Optional[str] = None,
    highlight: bool = False,
    limit: int = 10,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Search CourtListener via the v4 Search API.

    Court filtering options:
    - Provide `courts` as one or more CourtListener `court_id` values (e.g., "scotus", "ca9").
    - Or provide `court_query` as a human string; it will be resolved via `courts_db` and merged
      with `courts` (deduped, preserving order).

    When `court_query` is used, the response includes a `court_resolution` block showing the
    resolved ids and the final `used_courts` list that was applied to the search.
    """
    if not query or not query.strip():
        raise ValueError("query is required")

    if type not in {"o", "r", "rd", "d", "p", "oa"}:
        raise ValueError("type must be one of: o, r, rd, d, p, oa")

    if semantic and type != "o":
        raise ValueError("semantic search is only available for type='o' (case law)")

    if limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")

    resolved_court_ids: List[str] = []
    court_resolution: Optional[Dict[str, Any]] = None
    if court_query and court_query.strip():
        resolution_kwargs: Dict[str, Any] = {}
        if court_bankruptcy is not None:
            resolution_kwargs["bankruptcy"] = court_bankruptcy
        dt_found = _parse_date_found(court_date_found)
        if dt_found is not None:
            resolution_kwargs["date_found"] = dt_found
        resolved_court_ids = courts_db.find_court(
            court_query.strip(), **resolution_kwargs
        )
        if not isinstance(resolved_court_ids, list):
            resolved_court_ids = list(resolved_court_ids or [])
        court_resolution = {
            "input": court_query,
            "bankruptcy": court_bankruptcy,
            "date_found": court_date_found,
            "resolved_ids": resolved_court_ids,
        }

    params: Dict[str, Any] = {
        "q": query,
        "type": type,
        "format": "json",
        "page_size": limit,
    }

    merged_courts: List[str] = []
    for value in (courts or []) + (resolved_court_ids or []):
        if not value or not str(value).strip():
            continue
        cid = str(value).strip()
        if cid not in merged_courts:
            merged_courts.append(cid)

    courts_joined = _courts_param(merged_courts)
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
                "title": item.get("caseName")
                or item.get("name")
                or item.get("docketNumber")
                or "(unknown)",
                "cluster_id": item.get("cluster_id"),
                "docket_id": item.get("docket_id"),
                "court": item.get("court"),
                "court_id": item.get("court_id"),
                "date_filed": item.get("dateFiled"),
                "url": ("https://www.courtlistener.com" + item["absolute_url"])
                if item.get("absolute_url")
                else None,
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
        **(
            {"court_resolution": {**court_resolution, "used_courts": merged_courts}}
            if court_resolution
            else {}
        ),
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
    include_opinions: bool = False,
    opinion_text_format: str = "html_with_citations",
) -> Dict[str, Any]:
    """Retrieve a cluster (case) by cluster ID."""
    if opinion_text_format not in {"html_with_citations", "plain_text"}:
        raise ValueError(
            "opinion_text_format must be 'html_with_citations' or 'plain_text'"
        )

    cluster_url = f"{CLUSTERS_ENDPOINT}/{cluster_id}/"
    cluster = await _get_json(cluster_url)

    result: Dict[str, Any] = {
        "cluster_id": cluster.get("id"),
        "absolute_url": cluster.get("absolute_url"),
        "url": ("https://www.courtlistener.com" + cluster["absolute_url"])
        if cluster.get("absolute_url")
        else None,
        "case_name": cluster.get("case_name"),
        "case_name_full": cluster.get("case_name_full"),
        "docket": cluster.get("docket"),
        "court": cluster.get("court"),
        "court_id": cluster.get("court_id"),
        "date_filed": cluster.get("date_filed"),
        "citations": cluster.get("citations"),
        "sub_opinions": cluster.get("sub_opinions") or [],
        "opinions": [],
        "raw": cluster,
    }

    if include_opinions and result["sub_opinions"]:
        opinion_tasks = []
        opinion_ids: List[int] = []
        for op_uri in result["sub_opinions"]:
            m = re.search(r"/opinions/(\d+)/", str(op_uri))
            if not m:
                continue
            op_id = int(m.group(1))
            opinion_ids.append(op_id)
            opinion_tasks.append(
                courtlistener_get_opinion(
                    opinion_id=op_id, text_format=opinion_text_format
                )
            )

        opinions: List[Dict[str, Any]] = []
        opinion_errors: List[Dict[str, Any]] = []
        for op_id, op_result in zip(
            opinion_ids, await asyncio.gather(*opinion_tasks, return_exceptions=True)
        ):
            if isinstance(op_result, Exception):
                opinion_errors.append({"opinion_id": op_id, "error": str(op_result)})
            else:
                opinions.append(op_result)

        result["opinions"] = opinions
        if opinion_errors:
            result["opinion_errors"] = opinion_errors

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
        raise ValueError(
            "Unsupported URL format. Expected a CourtListener /opinion/<cluster_id>/ URL."
        )

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
    mcp.run(transport="streamable-http")
