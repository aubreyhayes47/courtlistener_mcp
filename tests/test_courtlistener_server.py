import importlib
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx


@pytest_asyncio.fixture
async def server(monkeypatch):
    """Reload the module with a test token and clean up the client."""
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "test-token")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.modules.pop("courtlistener_server", None)
    module = importlib.import_module("courtlistener_server")
    try:
        yield module
    finally:
        await module.aclose_client()


@pytest.mark.asyncio
async def test_helper_utilities(server):
    assert server._extract_next_cursor(None) is None
    assert (
        server._extract_next_cursor("https://example.com/search/?cursor=test-cursor&foo=bar")
        == "test-cursor"
    )
    assert server._courts_param(None) is None
    assert server._courts_param(["ca1", " ca2 "]) == "ca1+ca2"


@respx.mock
@pytest.mark.asyncio
async def test_search_normalization(server):
    search_route = respx.get(server.SEARCH_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 2501,
                "next": f"{server.SEARCH_ENDPOINT}?cursor=next-cursor",
                "results": [
                    {
                        "caseName": "Test Case",
                        "cluster_id": 123,
                        "docket_id": 456,
                        "court": "Supreme Court",
                        "court_id": "scotus",
                        "dateFiled": "2020-01-01",
                        "absolute_url": "/opinion/123/test-case/",
                        "citation": "123 U.S. 456",
                        "snippet": "Example snippet",
                        "meta": {"score": {"bm25": 42.0}},
                    }
                ],
            },
        )
    )

    result = await server.courtlistener_search(
        query="test query",
        type="d",
        courts=["ca1", "ca2"],
        highlight=True,
        limit=5,
    )

    request = search_route.calls[0].request
    assert request.url.params["q"] == "test query"
    assert request.url.params["court"] == "ca1+ca2"
    assert request.url.params["highlight"] == "on"
    assert request.url.params["page_size"] == "5"
    assert result["approximate"] is True
    assert result["next_cursor"] == "next-cursor"
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "Test Case"
    assert result["results"][0]["url"] == "https://www.courtlistener.com/opinion/123/test-case/"
    assert result["results"][0]["score"] == 42.0


@respx.mock
@pytest.mark.asyncio
async def test_get_opinion(server):
    opinion_route = respx.get(f"{server.OPINIONS_ENDPOINT}/999/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 999,
                "cluster": 123,
                "type": "majority",
                "author_str": "Justice Test",
                "per_curiam": False,
                "joined_by_str": "Judge A",
                "plain_text": "Opinion text",
                "download_url": "https://example.com/opinion.pdf",
                "local_path": "/tmp/opinion",
                "opinions_cited": [1, 2, 3],
                "date_created": "2020-01-01",
                "date_modified": "2020-01-02",
            },
        )
    )

    result = await server.courtlistener_get_opinion(opinion_id=999, text_format="plain_text")

    assert opinion_route.called
    assert result["opinion_id"] == 999
    assert result["text"] == "Opinion text"
    assert result["opinions_cited"] == [1, 2, 3]


@respx.mock
@pytest.mark.asyncio
async def test_get_cluster_with_opinions(server):
    respx.get(f"{server.CLUSTERS_ENDPOINT}/123/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 123,
                "absolute_url": "/opinion/123/test-case/",
                "case_name": "Test Case",
                "case_name_full": "Test Case Full",
                "docket": 456,
                "court": "https://www.courtlistener.com/api/rest/v4/courts/scotus/",
                "court_id": "scotus",
                "date_filed": "2020-01-01",
                "citations": [],
                "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/999/"],
            },
        )
    )
    respx.get(f"{server.OPINIONS_ENDPOINT}/999/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 999,
                "cluster": 123,
                "type": "majority",
                "author_str": "Justice Test",
                "per_curiam": False,
                "joined_by_str": "Judge A",
                "html_with_citations": "<p>Opinion text</p>",
                "download_url": "https://example.com/opinion.pdf",
                "local_path": "/tmp/opinion",
                "opinions_cited": [],
                "date_created": "2020-01-01",
                "date_modified": "2020-01-02",
            },
        )
    )

    result = await server.courtlistener_get_cluster(cluster_id=123, include_opinions=True)

    assert result["cluster_id"] == 123
    assert result["url"] == "https://www.courtlistener.com/opinion/123/test-case/"
    assert result["court"] == "https://www.courtlistener.com/api/rest/v4/courts/scotus/"
    assert len(result["opinions"]) == 1
    assert result["opinions"][0]["opinion_id"] == 999


@respx.mock
@pytest.mark.asyncio
async def test_resolve_from_url(server):
    respx.get(f"{server.CLUSTERS_ENDPOINT}/2812209/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 2812209,
                "absolute_url": "/opinion/2812209/test-case/",
                "case_name": "Test Case",
                "case_name_full": "Test Case Full",
                "docket": 456,
                "court": "https://www.courtlistener.com/api/rest/v4/courts/scotus/",
                "court_id": "scotus",
                "date_filed": "2020-01-01",
                "citations": [],
                "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/999/"],
            },
        )
    )
    respx.get(f"{server.OPINIONS_ENDPOINT}/999/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 999,
                "cluster": 2812209,
                "type": "majority",
                "author_str": "Justice Test",
                "per_curiam": False,
                "joined_by_str": "Judge A",
                "plain_text": "Opinion text",
                "download_url": "https://example.com/opinion.pdf",
                "local_path": "/tmp/opinion",
                "opinions_cited": [],
                "date_created": "2020-01-01",
                "date_modified": "2020-01-02",
            },
        )
    )

    result = await server.courtlistener_resolve_from_url(
        url="https://www.courtlistener.com/opinion/2812209/test-case/",
        include_opinions=True,
        opinion_text_format="plain_text",
    )

    assert result["resolved_type"] == "cluster"
    assert result["cluster_id"] == 2812209
    assert result["result"]["cluster_id"] == 2812209
    assert result["result"]["opinions"][0]["text_format"] == "plain_text"


@pytest.mark.asyncio
async def test_resolve_from_url_validation(server):
    with pytest.raises(ValueError):
        await server.courtlistener_resolve_from_url(url="https://www.example.com/invalid")
