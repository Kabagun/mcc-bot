"""Small, reviewed exceptions applied while importing Tannei observations.

The normal importer publishes every validated observation.  A reviewed policy
is intentionally narrow: it is keyed by the source network ID and changes only
the observation set for that network.  Source metadata and provenance are
still retained, so the policy can be audited or changed explicitly later.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

NETWORK_21VEK = 1086508


@dataclass(frozen=True, slots=True)
class ReviewedNetworkPolicy:
    """An immutable import rule reviewed for one Tannei network."""

    network_id: int
    offline_observations: str = "suppress"
    online_mccs: frozenset[str] = frozenset()

    def apply(self, metadata: Mapping[str, object], observations: Iterable[Mapping]) -> list[dict]:
        """Return observations allowed for the source metadata.

        The returned dictionaries are copies.  The caller can therefore
        normalize MCC values or attach snapshot state without mutating a
        client response held by another stage of the importer.
        """

        network_id = metadata.get("network_id")
        try:
            matches_network = int(network_id) == self.network_id
        except (TypeError, ValueError):
            matches_network = False
        if not matches_network:
            return [dict(item) for item in observations]
        if metadata.get("is_online") is False and self.offline_observations == "suppress":
            return []
        if metadata.get("is_online") is True and self.online_mccs:
            return [
                dict(item)
                for item in observations
                if str(item.get("mcc", "")).strip() in self.online_mccs
            ]
        return [dict(item) for item in observations]


REVIEWED_NETWORK_POLICIES: tuple[ReviewedNetworkPolicy, ...] = (
    ReviewedNetworkPolicy(network_id=NETWORK_21VEK, online_mccs=frozenset({"5300"})),
)


def policy_for_network(network_id: object) -> ReviewedNetworkPolicy | None:
    """Return a reviewed policy for ``network_id`` when one exists."""

    try:
        numeric_id = int(network_id)
    except (TypeError, ValueError):
        return None
    return next(
        (policy for policy in REVIEWED_NETWORK_POLICIES if policy.network_id == numeric_id),
        None,
    )


def filter_observations(
    metadata: Mapping[str, object], observations: Iterable[Mapping]
) -> list[dict]:
    """Apply the reviewed policy, if any, to one source response."""

    network_id = metadata.get("network_id")
    policy = policy_for_network(network_id)
    return policy.apply(metadata, observations) if policy else [dict(item) for item in observations]


# Friendly aliases used by callers that want to make the policy boundary
# explicit in code and tests.
apply_reviewed_policy = filter_observations


__all__ = [
    "NETWORK_21VEK",
    "REVIEWED_NETWORK_POLICIES",
    "ReviewedNetworkPolicy",
    "apply_reviewed_policy",
    "filter_observations",
    "policy_for_network",
]
