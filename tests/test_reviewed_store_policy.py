from __future__ import annotations

from mcc_bot.reviewed_store_policy import NETWORK_21VEK, filter_observations


def test_21vek_policy_keeps_only_online_5300_and_suppresses_offline() -> None:
    observations = [
        {"mcc": "5300", "payment_date": "2026-08"},
        {"mcc": "5399", "payment_date": "2026-08"},
    ]

    online = filter_observations({"network_id": NETWORK_21VEK, "is_online": True}, observations)
    offline = filter_observations(
        {"network_id": str(NETWORK_21VEK), "is_online": False}, observations
    )

    assert online == [{"mcc": "5300", "payment_date": "2026-08"}]
    assert offline == []
    assert observations[1]["mcc"] == "5399"


def test_unreviewed_network_is_unchanged_and_returns_copies() -> None:
    observations = [{"mcc": "5411", "payment_date": None}]
    result = filter_observations({"network_id": 123, "is_online": False}, observations)

    assert result == observations
    assert result is not observations
    assert result[0] is not observations[0]
