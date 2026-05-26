"""Tests for the aiosqlite Cache."""

from __future__ import annotations

import asyncio

import pytest

from citation_mcp.cache import Cache, make_cache_key


async def test_set_then_get(cache: Cache):
    await cache.set("k1", {"hello": "world"}, type_="test")
    assert await cache.get("k1") == {"hello": "world"}


async def test_get_missing_returns_none(cache: Cache):
    assert await cache.get("missing") is None


async def test_get_expired_returns_none(cache: Cache):
    await cache.set("k_exp", {"v": 1}, type_="test", ttl_seconds=1)
    await asyncio.sleep(1.2)
    assert await cache.get("k_exp") is None


async def test_delete(cache: Cache):
    await cache.set("k_del", {"v": 1}, type_="test")
    assert await cache.get("k_del") is not None
    await cache.delete("k_del")
    assert await cache.get("k_del") is None


async def test_purge_expired_removes_only_expired(cache: Cache):
    await cache.set("k_alive", {"v": 1}, type_="test", ttl_seconds=3600)
    await cache.set("k_dead", {"v": 2}, type_="test", ttl_seconds=1)
    await asyncio.sleep(1.2)
    removed = await cache.purge_expired()
    assert removed == 1
    assert await cache.get("k_alive") is not None
    assert await cache.get("k_dead") is None


def test_make_cache_key_doi_path():
    key = make_cache_key({"doi": "https://doi.org/10.1056/NEJMoa2034577"})
    assert key == "verify:doi:10.1056/nejmoa2034577"


def test_make_cache_key_metadata_path_is_stable():
    a = make_cache_key({
        "title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine",
        "authors": ["Polack, Fernando P."],
        "year": 2020,
    })
    b = make_cache_key({
        "title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine",
        "authors": ["Polack, Fernando P."],
        "year": 2020,
    })
    assert a == b
    assert a.startswith("verify:meta:")


def test_make_cache_key_doi_overrides_metadata():
    doi_key = make_cache_key({
        "doi": "10.1/abc",
        "title": "Something",
        "authors": ["Smith, J"],
        "year": 2020,
    })
    assert doi_key.startswith("verify:doi:")
