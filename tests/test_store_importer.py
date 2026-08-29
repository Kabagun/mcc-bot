from __future__ import annotations

import json

import httpx
import pytest

from mcc_bot.import_stores import (
    BASE_URL,
    SITEMAP,
    SourceBlocked,
    SourceError,
    SourcePaused,
    StoreImporter,
    TanneiClient,
    main,
    parse_observations,
    parse_store,
)
from mcc_bot.stores import StoreRepository


def source_store(store_id=1, network_id=10, online=False):
    return {
        "type": "store",
        "id": store_id,
        "name": "Branch",
        "is_online": online,
        "store_network": {"type": "store_network", "id": network_id, "name": "Chain"}
        if network_id
        else None,
        "address": None,
    }


def observations(mcc="5411"):
    return [
        {
            "mcc": mcc,
            "reference": {"merchant_type": "Groceries"},
            "payment_date": "2022-06",
            "address_extra": None,
        }
    ]


@pytest.fixture
def repository(tmp_path):
    repository = StoreRepository(tmp_path / "stores.sqlite3")
    repository.initialize()
    return repository


def test_source_schema_month_precision_and_no_reward_data():
    parsed = parse_store(source_store(), expected_id=1)
    assert parsed["network_id"] == 10
    assert parsed["address"] is None
    assert parse_observations(observations())[0]["payment_date"] == "2022-06"
    raw = source_store()
    raw["products"] = [{"bank": "must never import"}]
    assert "products" not in parse_store(raw)


@pytest.mark.parametrize(
    "change", [{"id": True}, {"is_online": 1}, {"type": "store_network"}, {"name": ""}]
)
def test_invalid_metadata_is_not_guessed(change):
    with pytest.raises(SourceError):
        parse_store({**source_store(), **change})


@pytest.mark.parametrize(
    "change",
    [{"mcc": 5411}, {"mcc": "411"}, {"payment_date": "2022-06-01"}, {"payment_date": "2022-13"}],
)
def test_invalid_observation_rejected(change):
    with pytest.raises(SourceError):
        parse_observations([{**observations()[0], **change}])


def test_client_rate_limit_retry_after_and_cookies(repository):
    now, times, sleeps = [0.0], [], []

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    def respond(request):
        times.append(now[0])
        assert "cookie" not in request.headers
        if len(times) == 1:
            return httpx.Response(429, headers={"Retry-After": "3", "Set-Cookie": "test=abc"})
        return httpx.Response(200, json=[], headers={"Set-Cookie": "test=abc"})

    client = TanneiClient(
        repository,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        sleep=sleep,
        clock=lambda: now[0],
    )
    client.request(BASE_URL + "one/")
    client.request(BASE_URL + "two/")
    assert times == [0, 3, 4]
    assert sleeps == [3, 1]


@pytest.mark.parametrize(
    "status,body,content_type",
    [(403, "denied", "text/plain"), (200, "captcha", "text/html"), (200, "login", "text/html")],
)
def test_access_blocks_stop_without_retry(repository, status, body, content_type):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, text=body, headers={"content-type": content_type})

    client = TanneiClient(repository, client=httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(SourceBlocked):
        client.request(BASE_URL + "stores/")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "retry_after,expected_requests", [("600", 1), ("0", 4), ("invalid", 1), ("nan", 1)]
)
def test_rate_limit_pauses_entire_import_before_second_store(
    repository, monkeypatch, retry_after, expected_requests
):
    requests, now = [], [0.0]

    def sleep(delay):
        now[0] += delay

    def respond(request):
        requests.append(str(request.url))
        if "/item/1/" in request.url.path:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json=observations())

    metadata = {store_id: parse_store(source_store(store_id)) for store_id in (1, 2)}
    monkeypatch.setattr(StoreImporter, "discover", lambda self: ({1, 2}, {10}, metadata, 0))
    client = TanneiClient(
        repository,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        sleep=sleep,
        clock=lambda: now[0],
    )
    with pytest.raises(SourcePaused):
        StoreImporter(repository, client).run()
    assert len(requests) == expected_requests
    assert all("/item/1/" in url for url in requests)
    assert repository.checkpoint("done:1") is None
    assert repository.checkpoint("done:2") is None
    assert repository.checkpoint("last_run")["status"] == "paused"
    assert repository.checkpoint("last_run")["processed"] == 1
    assert repository.checkpoint("last_run")["errors"] == 1


def test_rate_limit_during_sitemap_discovery_does_not_try_fallback(repository):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "600"})

    client = TanneiClient(repository, client=httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(SourcePaused):
        StoreImporter(repository, client).run()
    assert requests == [SITEMAP]


def test_discovery_union_standalone_plural_paths_and_resume(repository):
    calls = []

    class Client:
        def request(self, url, *, body=None, xml=False):
            calls.append((url, body))
            if url == SITEMAP:
                return "<sitemapindex><sitemap><loc>https://tannei.by/sitemap/moneyback-0.xml</loc></sitemap></sitemapindex>"
            if xml:
                return "<urlset><url><loc>https://tannei.by/moneyback/stores/1/</loc></url><url><loc>https://tannei.by/moneyback/stores/3/slug/</loc></url><url><loc>https://tannei.by/moneyback/storenet/10/</loc></url></urlset>"
            if body is not None:
                return [source_store(2, online=body["is_online"])] if not body["is_online"] else []
            if url.endswith("/mcc/"):
                return observations()
            return source_store(int(url.rstrip("/").split("/")[-1]), network_id=None)

    importer = StoreImporter(repository, Client())
    first = importer.run(max_stores=1)
    assert first["found"] == 3
    assert first["imported"] == 1
    assert first["remaining"] == 2
    second = StoreImporter(repository, Client(), resume=True).run()
    assert second["skipped"] == 1
    assert second["imported"] == 2
    assert repository.counts()["source_stores"] == 3
    assert {body["is_online"] for _, body in calls if body} == {False, True}


def test_resumed_http_requests_use_checkpoints(repository):
    calls = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: (calls.append(req), httpx.Response(200, json=[]))[1]
        )
    )
    TanneiClient(repository, client=client).request(BASE_URL + "sample/")
    TanneiClient(repository, client=client, resume=True).request(BASE_URL + "sample/")
    assert len(calls) == 1


@pytest.mark.parametrize("trailing_slash", ["/", ""])
def test_discovery_preserves_all_network_ids_with_actual_seo_suffixes(repository, trailing_slash):
    networks = {
        1319671: "green",
        1410585: "leonardo",
        1439063: "minskaya-farmatsiya",
        1463370: "helix",
        1483230: "mak-by",
        1692239: "farmatsiya",
        1769069: "mastak",
        1837537: "belgosstrakh",
    }
    searched = set()

    class Client:
        def request(self, url, *, body=None, xml=False):
            if xml:
                links = [
                    f"https://tannei.by/moneyback/storenet/{network_id}/{slug}{trailing_slash}"
                    for network_id, slug in networks.items()
                ]
                links.append("https://tannei.by/moneyback/storenet/1996747/")
                return (
                    "<urlset>"
                    + "".join(f"<url><loc>{link}</loc></url>" for link in links)
                    + "</urlset>"
                )
            searched.add((body["storenet_id"], body["is_online"]))
            return []

    stores, discovered, _, ambiguity = StoreImporter(repository, Client()).discover()
    assert discovered == {*networks, 1996747}
    assert searched == {
        (network_id, online) for network_id in discovered for online in (False, True)
    }
    assert not stores
    assert ambiguity == 0


def test_status_does_not_create_database(tmp_path, capsys):
    database = tmp_path / "absent.sqlite3"
    main(["--database", str(database), "--status"])
    assert json.loads(capsys.readouterr().out) == {"status": "not_initialized"}
    assert not database.exists()


def test_dry_run_never_touches_requested_database(tmp_path, monkeypatch, capsys):
    database = tmp_path / "absent.sqlite3"
    monkeypatch.setattr(
        StoreImporter, "discover", lambda self: ({1}, set(), {1: parse_store(source_store())}, 0)
    )
    monkeypatch.setattr(TanneiClient, "request", lambda *a, **k: observations())
    main(["--database", str(database), "--dry-run"])
    assert not database.exists()
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["dry_run"] is True


def test_fetch_error_discards_every_staged_publication(repository, monkeypatch):
    metadata = {store_id: parse_store(source_store(store_id)) for store_id in (1, 2)}
    monkeypatch.setattr(StoreImporter, "discover", lambda self: ({1, 2}, {10}, metadata, 0))

    class Client:
        def request(self, url, **_kwargs):
            if "/item/2/" in url:
                raise SourceError("broken second source")
            return observations()

    result = StoreImporter(repository, Client()).run()
    assert result["errors"] == 1
    assert result["imported"] == 0
    assert repository.counts()["source_stores"] == 0
    assert repository.checkpoint("done:1") is None
    assert repository.checkpoint("done:2") is None
