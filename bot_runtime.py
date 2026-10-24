"""Fail-closed runtime selection, without Telegram calls or storage access."""

from dataclasses import dataclass, field
from typing import Mapping


class RuntimeConfigurationError(ValueError):
    """A safe-to-display error that never includes credentials."""


@dataclass(frozen=True)
class BotRuntime:
    mode: str
    token: str | None = field(repr=False)
    credential_key: str | None = None


def select_bot_runtime(mode: str, environment: Mapping[str, str]) -> BotRuntime:
    """Select only the credential allowed in this execution environment."""
    if mode not in {"development", "production"}:
        raise RuntimeConfigurationError("Runtime must be development or production.")

    deployed = environment.get("REPLIT_DEPLOYMENT") == "1"
    production_token = environment.get("TELEGRAM_BOT_TOKEN", "").strip()
    if mode == "production":
        if not deployed:
            raise RuntimeConfigurationError(
                "Production polling is allowed only in a published Replit deployment."
            )
        token = production_token
        if not token:
            raise RuntimeConfigurationError(
                "Production requires TELEGRAM_BOT_TOKEN in production Secrets; legacy BOT_TOKEN is disabled."
            )
        return BotRuntime(mode, token, "TELEGRAM_BOT_TOKEN")

    if deployed:
        raise RuntimeConfigurationError(
            "Published deployments must explicitly select --runtime production."
        )
    if environment.get("BOT_WORKSPACE_PREVIEW") == "1":
        if not production_token:
            raise RuntimeConfigurationError(
                "Explicit workspace preview requires TELEGRAM_BOT_TOKEN."
            )
        return BotRuntime("preview", production_token, "TELEGRAM_BOT_TOKEN")
    token = environment.get("DEV_BOT_TOKEN", "").strip()
    if not token:
        # Never fall back to production credentials, even in shared Secrets.
        return BotRuntime(mode, None)
    retired_token = environment.get("BOT_TOKEN", "").strip()
    if any(
        forbidden and (
            token == forbidden
            or token.partition(":")[0] == forbidden.partition(":")[0]
        )
        for forbidden in (production_token, retired_token)
    ):
        raise RuntimeConfigurationError(
            "DEV_BOT_TOKEN must belong to a different bot than the production or retired token."
        )
    return BotRuntime(mode, token, "DEV_BOT_TOKEN")


def verify_production_bot_identity(bot, mode):
    """Never allow the retired bot to poll, including through a development key."""
    identity = bot.get_me()
    username = (getattr(identity, "username", None) or "").casefold()
    if username == "fvdhbbbot":
        raise RuntimeConfigurationError(
            "Retired bot @Fvdhbbbot is disabled; use @CinemixIL_bot in production."
        )
    if mode not in {"production", "preview"}:
        return None
    if username != "cinemixil_bot":
        raise RuntimeConfigurationError(
            "Production TELEGRAM_BOT_TOKEN must belong to @CinemixIL_bot; polling is disabled."
        )
    return identity
