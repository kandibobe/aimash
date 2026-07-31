"""Backward-compatible import path for the legacy aiogram campaign wizard."""

from campaigns.wizard_store import (  # noqa: F401
    CampaignDraftStore,
    DraftSnapshot,
    _draft_select,
    empty_wizard_state,
)

__all__ = ["CampaignDraftStore", "DraftSnapshot", "_draft_select", "empty_wizard_state"]
