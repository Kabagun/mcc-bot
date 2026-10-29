"""Explicit rollout gate for the new form interface, independent of user roles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


@dataclass(frozen=True, slots=True)
class FormFlowRollout:
    """Select the form interface without granting any underlying permissions."""

    user_ids: frozenset[int] = frozenset()
    all_users: bool = False

    def enabled_for(self, user_id: int) -> bool:
        """Enable only valid Telegram identities explicitly included in the rollout."""

        if type(user_id) is not int or not 0 < user_id < 2**63:
            return False
        if self.all_users is True:
            return True
        return isinstance(self.user_ids, frozenset) and any(
            type(allowed_id) is int and allowed_id == user_id for allowed_id in self.user_ids
        )


def is_form_flow_v2_enabled(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Fail closed unless application data contains the known rollout configuration."""

    rollout = context.application.bot_data.get("form_flow_v2")
    return type(rollout) is FormFlowRollout and rollout.enabled_for(user_id)
