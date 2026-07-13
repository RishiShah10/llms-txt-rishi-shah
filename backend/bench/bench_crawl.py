"""Measure a real crawl, concurrent vs forced-sequential.

Run:  cd backend && .venv/bin/python bench/bench_crawl.py https://docs.python.org/3/

This exists because the theoretical "10x fetch concurrency" speedup from
Tasks 1-8 is not guaranteed: `extractor.extract` runs `BeautifulSoup(html,
"lxml")` inline on the event loop, and `discoverer._parse_sitemap` runs
`etree.fromstring` inline too. Both are CPU-bound and serialize on the GIL
regardless of how many fetches are in flight. On a 0.5 vCPU Fargate task,
ten concurrent fetches could just feed ten parses that queue up behind each
other -- so the network speedup may not translate into wall-clock speedup.
Measure, don't assume.
"""
import asyncio
import os
import statistics
import sys
import time

# Script lives in backend/bench/; the pipeline modules (crawler, discoverer,
# generator, ...) live in backend/. Running this file directly (rather than
# via pytest, which sets pythonpath = ["."] in pyproject.toml) needs the same
# path added by hand.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from lxml import etree as _lxml_etree

import crawler
import discoverer
import extractor
import generator
from generator import generate

TARGET = sys.argv[1] if len(sys.argv) > 1 else "https://docs.python.org/3/"
PAGES = 50
RUNS_PER_CONFIG = 2

# `from fetcher import FETCH_CONCURRENCY` binds by VALUE, so patching
# fetcher.FETCH_CONCURRENCY alone would not affect the modules that already
# imported it. Patch every module that holds a copy, or the knob doesn't turn
# and this benchmark silently measures the same thing twice.
_HOLDERS = (generator, crawler, discoverer)


# --------------------------------------------------------------------------
# Instrumentation. This is bench-only monkeypatching -- it does not modify
# any pipeline source file. Two things are measured beyond wall-clock:
#
# 1. Peak in-flight fetches, to PROVE the concurrency knob actually turns
#    (rather than trusting that patching FETCH_CONCURRENCY reached the loops).
# 2. Cumulative time spent inside the synchronous, event-loop-blocking parse
#    calls (BeautifulSoup in extractor.extract, etree.fromstring in
#    discoverer._parse_sitemap), to see how much of wall-clock is CPU-bound
#    parsing that concurrency cannot help with.
#
# Note on binding: `generator.py` does `from extractor import extract` and
# `from fetcher import fetch`, and `crawler.py` does `from extractor import
# extract` -- both are the same by-value binding trap as FETCH_CONCURRENCY.
# Patching `extractor.extract` or `fetcher.fetch` alone would NOT intercept
# calls made from generator.py or crawler.py, which already hold their own
# copies of those names. So each holder module's own attribute is patched.
# `etree.fromstring`, by contrast, is an attribute lookup on the shared
# `lxml.etree` module object (`etree.fromstring(...)` inside discoverer.py),
# so patching it in one place is sufficient.

_in_flight = 0
_peak_in_flight = 0
_parse_seconds = 0.0
_sitemap_parse_seconds = 0.0

_real_fetch = generator.fetch
_real_extract = extractor.extract
_real_fromstring = _lxml_etree.fromstring


async def _instrumented_fetch(url, client):
    global _in_flight, _peak_in_flight
    _in_flight += 1
    _peak_in_flight = max(_peak_in_flight, _in_flight)
    try:
        return await _real_fetch(url, client)
    finally:
        _in_flight -= 1


def _instrumented_extract(html, url):
    global _parse_seconds
    t0 = time.perf_counter()
    try:
        return _real_extract(html, url)
    finally:
        _parse_seconds += time.perf_counter() - t0


def _instrumented_fromstring(*args, **kwargs):
    global _sitemap_parse_seconds
    t0 = time.perf_counter()
    try:
        return _real_fromstring(*args, **kwargs)
    finally:
        _sitemap_parse_seconds += time.perf_counter() - t0


generator.fetch = _instrumented_fetch
generator.extract = _instrumented_extract
crawler.extract = _instrumented_extract
_lxml_etree.fromstring = _instrumented_fromstring


async def run(concurrency: int) -> dict:
    global _in_flight, _peak_in_flight, _parse_seconds, _sitemap_parse_seconds
    _in_flight = 0
    _peak_in_flight = 0
    _parse_seconds = 0.0
    _sitemap_parse_seconds = 0.0

    originals = [module.FETCH_CONCURRENCY for module in _HOLDERS]
    for module in _HOLDERS:
        module.FETCH_CONCURRENCY = concurrency
    try:
        start = time.perf_counter()
        async with httpx.AsyncClient() as client:
            result = await generate(TARGET, client, max_pages=PAGES, crawl=True)
        elapsed = time.perf_counter() - start
        parse_total = _parse_seconds + _sitemap_parse_seconds
        print(
            f"  concurrency={concurrency:2d}  {elapsed:6.2f}s  "
            f"{len(result['pages']):3d} pages  "
            f"peak_in_flight={_peak_in_flight:2d}  "
            f"parse={parse_total:5.2f}s ({100 * parse_total / elapsed:4.1f}% of wall)"
        )
        return {
            "concurrency": concurrency,
            "elapsed": elapsed,
            "pages": len(result["pages"]),
            "peak_in_flight": _peak_in_flight,
            "parse_seconds": parse_total,
            "extract_seconds": _parse_seconds,
            "sitemap_seconds": _sitemap_parse_seconds,
        }
    finally:
        for module, original in zip(_HOLDERS, originals):
            module.FETCH_CONCURRENCY = original


async def main() -> None:
    print(f"Target: {TARGET}  ({PAGES} pages, {RUNS_PER_CONFIG} runs per configuration)")
    print()

    # Interleave configurations (1, 10, 1, 10, ...) rather than running all of
    # one config then all of the other, so network drift over the run doesn't
    # bias one configuration more than the other.
    results: dict[int, list[dict]] = {1: [], 10: []}
    for i in range(RUNS_PER_CONFIG):
        print(f"-- round {i + 1} --")
        results[1].append(await run(1))
        results[10].append(await run(10))
        print()

    seq_times = [r["elapsed"] for r in results[1]]
    conc_times = [r["elapsed"] for r in results[10]]
    seq_peaks = [r["peak_in_flight"] for r in results[1]]
    conc_peaks = [r["peak_in_flight"] for r in results[10]]

    seq_mean = statistics.mean(seq_times)
    conc_mean = statistics.mean(conc_times)

    print("=" * 60)
    print(f"concurrency=1   times: {[f'{t:.2f}s' for t in seq_times]}  "
          f"mean={seq_mean:.2f}s  peak_in_flight={seq_peaks}")
    print(f"concurrency=10  times: {[f'{t:.2f}s' for t in conc_times]}  "
          f"mean={conc_mean:.2f}s  peak_in_flight={conc_peaks}")
    print()

    if max(seq_peaks) <= 1 and max(conc_peaks) > 1:
        print("Knob check: PASSED — concurrency=1 never exceeded 1 in-flight "
              "fetch, concurrency=10 did. The patch is reaching the loops.")
    else:
        print("Knob check: SUSPECT — peak in-flight fetches do not distinguish "
              "the two configurations. Do not trust the speedup number below.")

    print(f"\nSpeedup (mean): {seq_mean / conc_mean:.2f}x")

    parse_fracs = [r["parse_seconds"] / r["elapsed"] for r in results[1] + results[10]]
    print(f"Parse phase (extract + sitemap XML) as % of wall clock, "
          f"across all runs: {[f'{100 * f:.0f}%' for f in parse_fracs]}")


if __name__ == "__main__":
    asyncio.run(main())
