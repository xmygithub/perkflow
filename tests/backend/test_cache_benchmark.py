from unittest.mock import AsyncMock

import pytest

from scripts.benchmark_shop_cache import BenchmarkError, summarize, verify_cache, verify_sample


def test_statistics_and_nearest_rank_p95():
    result = summarize([float(n) for n in range(1, 101)], selects=100)
    assert result["samples"] == 100
    assert result["mean_ms"] == result["median_ms"] == 50.5
    assert result["p95_ms"] == 95.0
    assert result["min_ms"] == 1.0 and result["max_ms"] == 100.0
    assert result["business_selects"] == 100


def test_empty_statistics_cannot_fake_a_performance_result():
    with pytest.raises(BenchmarkError, match="没有有效样本"):
        summarize([], selects=0)


@pytest.mark.parametrize(("mode", "selects"), [("hit", 1), ("mysql", 0), ("miss", 0)])
def test_fallback_or_identity_map_cannot_masquerade_as_correct_mode(mode, selects):
    with pytest.raises(BenchmarkError, match="实际业务 SELECT"):
        verify_sample(mode, '{"id":1}', '{"id":1}', selects)


def test_changed_payload_stops_comparison():
    with pytest.raises(BenchmarkError, match="数据发生变化"):
        verify_sample("hit", '{"id":2}', '{"id":1}', 0)


@pytest.mark.parametrize(("kind", "ttl"), [("none", -2), ("string", -1)])
async def test_failed_or_nonexpiring_refill_is_not_a_valid_miss(kind, ttl):
    cache = AsyncMock()
    cache.type.return_value = kind
    cache.ttl.return_value = ttl
    with pytest.raises(BenchmarkError, match="未成功回填"):
        await verify_cache(cache, "synthetic-test-key", "shop")
