from __future__ import annotations

from .provider import ResilientExtractProvider, load_plugin_settings


def register(ctx) -> None:
    ctx.register_web_search_provider(
        ResilientExtractProvider(**load_plugin_settings())
    )
