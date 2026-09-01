"""One-shot, resumable import of publicly discovered tannei store observations.

Only merchant metadata and per-store MCC observations are imported. Search has
no verified pagination contract: sitemap IDs and exact network searches are
unioned, without claiming complete coverage of tannei's internal database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .stores import StoreError, StoreRepository

BASE_URL = "https://tannei.by/api/v2/"
SITEMAP = "https://tannei.by/sitemap_index.xml"
_STORE_PATH = re.compile(r"/moneyback/stores?/(\d+)(?:/[^/?#]+)?/?$")
_NETWORK_PATH = re.compile(r"/moneyback/storenet/(\d+)(?:/[^/?#]+)?/?$")
_MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")


class SourceError(ValueError):
    """A source response cannot safely be interpreted or fetched."""


class SourceBlocked(SourceError):
    """Source access denied/challenged; the entire import must stop."""


class SourcePaused(SourceError):
    """Rate limiting cannot safely be retried now; stop the entire import."""


def _identifier(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceError(f"{field}: expected a positive numeric source ID")
    return value


def _text(value, field, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise SourceError(f"{field}: invalid source text")
    return value.strip()


def parse_store(raw: object, *, expected_id: int | None = None) -> dict:
    """Validate source fields, preserving exact store/network IDs and channel."""

    if not isinstance(raw, dict) or raw.get("type") != "store":
        raise SourceError("Expected a store object")
    store_id = _identifier(raw.get("id"), "store.id")
    if expected_id is not None and expected_id != store_id:
        raise SourceError("Source returned a different store ID")
    name = _text(raw.get("name"), "store.name")
    if len(name) > 180:
        raise SourceError("Store name exceeds supported length")
    if not isinstance(raw.get("is_online"), bool):
        raise SourceError("store.is_online must be boolean")
    network = raw.get("store_network")
    network_id = network_name = None
    if network is not None:
        if not isinstance(network, dict) or network.get("type") != "store_network":
            raise SourceError("Invalid nested store_network")
        network_id = _identifier(network.get("id"), "network.id")
        network_name = _text(network.get("name"), "network.name")
        if len(network_name) > 180:
            raise SourceError("Network name exceeds supported length")
    address = raw.get("address")
    if address is not None and not isinstance(address, dict):
        raise SourceError("Invalid address object")
    return {
        "id": store_id,
        "name": name,
        "is_online": raw["is_online"],
        "network_id": network_id,
        "network_name": network_name,
        "address": _text(address.get("address"), "address", optional=True) if address else None,
    }


def parse_observations(raw: object) -> list[dict]:
    """Validate MCC observations without inventing dates, precision or codes."""

    if not isinstance(raw, list):
        raise SourceError("Expected MCC observation array")
    observations = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("mcc"), str)
            or not re.fullmatch(r"[0-9]{4}", item["mcc"])
        ):
            raise SourceError("Invalid four-digit source MCC")
        month = item.get("payment_date")
        if month is not None and (not isinstance(month, str) or not _MONTH.fullmatch(month)):
            raise SourceError("Unsupported payment date precision")
        reference = item.get("reference")
        if reference is not None and not isinstance(reference, dict):
            raise SourceError("Invalid MCC reference")
        observations.append(
            {
                "mcc": item["mcc"],
                "payment_date": month,
                "merchant_type": _text(
                    reference.get("merchant_type"), "merchant_type", optional=True
                )
                if reference
                else None,
                "address_extra": _text(item.get("address_extra"), "address_extra", optional=True),
            }
        )
    return observations


class TanneiClient:
    """Sequential, cookie-free client with bounded retries and a 1 req/s ceiling."""

    def __init__(
        self,
        repository: StoreRepository,
        *,
        resume=False,
        client=None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.repository = repository
        self.resume = resume
        self.client = client or httpx.Client(
            timeout=30, follow_redirects=False, headers={"User-Agent": "mcc-bot-store-import/1.0"}
        )
        self.sleep = sleep
        self.clock = clock
        self._last_request = float("-inf")

    def close(self) -> None:
        """Close the HTTP connection pool."""

        self.client.close()

    @staticmethod
    def _url(url):
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "tannei.by"
            or parsed.query
            or parsed.fragment
        ):
            raise SourceError("Unexpected source URL")
        return url

    @staticmethod
    def _retry_after(value):
        if not value:
            return 2.0
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError) as exc:
                raise SourcePaused("Invalid Retry-After; import paused") from exc
        if not math.isfinite(delay):
            raise SourcePaused("Invalid Retry-After; import paused")
        if delay > 300:
            raise SourcePaused("Retry-After exceeds five minutes; resume the import later")
        return max(0, delay)

    def request(self, url: str, *, body=None, xml=False):
        """Fetch/cache one validated transport response; stop on access challenges."""

        url = self._url(url)
        key = (
            "response:"
            + hashlib.sha256(json.dumps([url, body], sort_keys=True).encode()).hexdigest()
        )
        if self.resume:
            cached = self.repository.checkpoint(key)
            if cached is not None:
                return cached
        for attempt in range(4):
            delay = 1.0 - (self.clock() - self._last_request)
            if delay > 0:
                self.sleep(delay)
            self._last_request = self.clock()
            # A Set-Cookie response must never establish a crawler session.
            self.client.cookies.clear()
            try:
                response = self.client.request(
                    "POST" if body is not None else "GET", url, json=body
                )
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise SourceError("Source transport failed after bounded retries") from exc
                self.sleep(2**attempt)
                continue
            text = response.text
            content_type = response.headers.get("content-type", "").lower()
            challenged = "text/html" in content_type and any(
                marker in text.casefold()
                for marker in ("captcha", "access denied", "cloudflare", "challenge")
            )
            if response.status_code in {401, 403} or challenged:
                raise SourceBlocked("Source denied/challenged access; stopped without bypass")
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == 3:
                    if response.status_code == 429:
                        raise SourcePaused(
                            "HTTP 429 after bounded retries; resume the import later"
                        )
                    raise SourceError(f"HTTP {response.status_code} after bounded retries")
                self.sleep(
                    self._retry_after(response.headers.get("retry-after"))
                    if response.status_code == 429
                    else 2**attempt
                )
                continue
            if response.status_code != 200:
                raise SourceError(f"Unexpected HTTP {response.status_code}")
            if len(response.content) > 20_000_000:
                raise SourceError("Source response too large")
            if xml:
                if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
                    raise SourceError("Unsupported XML declarations")
                try:
                    ET.fromstring(text)
                except ET.ParseError as exc:
                    raise SourceError("Invalid sitemap XML") from exc
                result = text
            else:
                if "json" not in content_type:
                    raise SourceBlocked("Unexpected non-JSON API response; stopped")
                try:
                    result = response.json()
                except ValueError as exc:
                    raise SourceError("Invalid source JSON") from exc
            self.repository.checkpoint(key, result, write=True)
            return result
        raise SourceError("Source request failed")


class StoreImporter:
    """Discover public records, import bounded batches, and retain checkpoints."""

    def __init__(self, repository: StoreRepository, client: TanneiClient, *, resume=False) -> None:
        self.repository = repository
        self.client = client
        self.resume = resume

    def discover(self) -> tuple[set[int], set[int], dict[int, dict], int]:
        """Union moneyback sitemap IDs with both channel searches per network."""

        stores, networks, metadata = set(), set(), {}
        tombstoned = self.repository.tannei_source_tombstones()
        ambiguity = 0
        pending, visited = deque([SITEMAP]), set()
        while pending:
            url = pending.popleft()
            if url in visited:
                continue
            visited.add(url)
            try:
                xml = self.client.request(url, xml=True)
            except (SourceBlocked, SourcePaused):
                raise
            except SourceError:
                if url == SITEMAP:
                    pending.append("https://tannei.by/sitemap.xml")
                    continue
                raise
            root = ET.fromstring(xml)
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] != "loc" or not node.text:
                    continue
                link = node.text.strip()
                parsed = urlparse(link)
                if parsed.netloc != "tannei.by" or parsed.scheme != "https":
                    continue
                if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
                    if "moneyback" in parsed.path:
                        pending.append(link)
                    continue
                if match := _STORE_PATH.fullmatch(parsed.path):
                    source_id = int(match[1])
                    if str(source_id) not in tombstoned:
                        stores.add(source_id)
                if match := _NETWORK_PATH.fullmatch(parsed.path):
                    networks.add(int(match[1]))
        for network_id in sorted(networks):
            for is_online in (False, True):
                rows = self.client.request(
                    BASE_URL + "moneyback/stores/search",
                    body={"storenet_id": network_id, "is_online": is_online},
                )
                if not isinstance(rows, list):
                    raise SourceError("Expected network store search array")
                for row in rows:
                    try:
                        parsed = parse_store(row)
                    except SourceError:
                        ambiguity += 1
                        continue
                    if parsed["network_id"] != network_id or parsed["is_online"] != is_online:
                        ambiguity += 1
                        continue
                    if str(parsed["id"]) not in tombstoned:
                        stores.add(parsed["id"])
                    if parsed["id"] in metadata and metadata[parsed["id"]] != parsed:
                        # The exact item endpoint will resolve conflicting search metadata.
                        metadata.pop(parsed["id"])
                        ambiguity += 1
                    else:
                        metadata[parsed["id"]] = parsed
        self.repository.checkpoint(
            "discovery", {"stores": sorted(stores), "networks": sorted(networks)}, write=True
        )
        return stores, networks, metadata, ambiguity

    def run(self, *, max_stores=None, dry_run=False, progress=None) -> dict:
        """Fetch one bounded batch, then publish every staged record atomically."""

        counters = {
            "found": 0,
            "networks": 0,
            "processed": 0,
            "imported": 0,
            "skipped": 0,
            "tombstoned": 0,
            "ambiguous": 0,
            "errors": 0,
            "remaining": 0,
            "dry_run": dry_run,
            "coverage": "publicly_discovered_only",
        }
        stores, networks, metadata, counters["ambiguous"] = self.discover()
        counters.update(found=len(stores), networks=len(networks))
        self.repository.checkpoint("last_run", counters, write=True)
        if progress:
            progress({"event": "discovery_complete", **counters})
        staged: list[tuple[dict, list[dict]]] = []
        for store_id in sorted(stores):
            key = f"done:{store_id}"
            if self.resume and self.repository.checkpoint(key):
                counters["skipped"] += 1
                continue
            if max_stores is not None and counters["processed"] >= max_stores:
                counters["remaining"] += 1
                continue
            counters["processed"] += 1
            try:
                store = metadata.get(store_id)
                if store is None:
                    store = parse_store(
                        self.client.request(BASE_URL + f"moneyback/stores/item/{store_id}/"),
                        expected_id=store_id,
                    )
                observations = parse_observations(
                    self.client.request(BASE_URL + f"moneyback/stores/item/{store_id}/mcc/")
                )
                if dry_run:
                    counters["imported"] += 1
                else:
                    staged.append((store, observations))
            except (SourceBlocked, SourcePaused) as exc:
                counters["errors"] += 1
                counters["status"] = "paused" if isinstance(exc, SourcePaused) else "blocked"
                counters["stop_reason"] = str(exc)
                self.repository.checkpoint("last_run", counters, write=True)
                raise
            except (SourceError, StoreError) as exc:
                counters["errors"] += 1
                self.repository.checkpoint(f"error:{store_id}", {"reason": str(exc)}, write=True)
            self.repository.checkpoint("last_run", counters, write=True)
            if progress and counters["processed"] % 50 == 0:
                progress({"event": "progress", **counters})
        if staged and not counters["errors"]:
            try:
                results = self.repository.import_stores(staged, checkpoint_done=True)
            except StoreError as exc:
                counters["errors"] += 1
                counters["status"] = "stopped"
                counters["stop_reason"] = str(exc)
            else:
                for result in results:
                    counters["imported" if result.changed else "skipped"] += 1
        self.repository.checkpoint("last_run", counters, write=True)
        return counters


def main(argv=None) -> None:
    """Run ``mcc-import-stores`` with JSON counters and no bank-catalog changes."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("MCC_STORES_PATH", "var/stores.sqlite3")),
    )
    parser.add_argument(
        "--resume", action="store_true", help="Reuse source checkpoints and skip completed records"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate in memory; leave the database untouched"
    )
    parser.add_argument(
        "--max-stores", type=int, help="Maximum stores processed this run (discovery still runs)"
    )
    parser.add_argument(
        "--status", action="store_true", help="Read existing counters without network or writes"
    )
    args = parser.parse_args(argv)
    if args.max_stores is not None and args.max_stores < 1:
        parser.error("--max-stores must be positive")
    if args.status:
        if not args.database.is_file():
            print(json.dumps({"status": "not_initialized"}))
            return
        repository = StoreRepository(args.database)
        discovery = repository.checkpoint("discovery") or {}
        print(
            json.dumps(
                {
                    **repository.counts(),
                    "last_run": repository.checkpoint("last_run"),
                    "discovered_stores": len(discovery.get("stores", [])),
                    "discovered_networks": len(discovery.get("networks", [])),
                },
                ensure_ascii=True,
            )
        )
        return
    repository = StoreRepository(":memory:" if args.dry_run else args.database)
    repository.initialize()
    client = TanneiClient(repository, resume=args.resume)
    try:
        counters = StoreImporter(repository, client, resume=args.resume).run(
            max_stores=args.max_stores,
            dry_run=args.dry_run,
            progress=lambda value: print(json.dumps(value, ensure_ascii=True), flush=True),
        )
        print(json.dumps(counters, ensure_ascii=True))
        if counters["errors"]:
            raise SystemExit(1)
    except SourceError as exc:
        status = "paused" if isinstance(exc, SourcePaused) else "stopped"
        print(json.dumps({"status": status, "reason": str(exc)}, ensure_ascii=True))
        raise SystemExit(2) from exc
    finally:
        client.close()


if __name__ == "__main__":
    main()
