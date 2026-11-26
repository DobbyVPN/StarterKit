import os
from typing import Any, AsyncIterator

import pytest
from adaptix import Retort
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dishka import AsyncContainer, Scope

from src.application.events.system import StarterKitWelcomeEvent
from src.core.config import AppConfig
from src.core.constants import REPOSITORY
from src.core.enums import Locale
from src.infrastructure.di.providers.i18n import I18nProvider
from src.infrastructure.services.notification import NotificationService
from src.telegram.keyboards import get_starterkit_welcome_keyboard


def test_welcome_text_has_no_upstream_promo() -> None:
    hub = I18nProvider().get_translator_hub(AppConfig.get(), Retort())
    i18n = hub.get_translator_by_locale(Locale.RU)
    event = StarterKitWelcomeEvent()
    text = str(i18n.get(event.event_key, **event.as_payload().i18n_kwargs))

    assert "DobbyVPN StarterKit" in text
    assert event.version in text
    for promo in ("boosty", "remna_shop", "звездочку", "донат"):
        assert promo not in text.lower()


def test_welcome_keyboard_points_to_starterkit() -> None:
    buttons = [b for row in get_starterkit_welcome_keyboard().inline_keyboard for b in row]
    assert [(b.text, b.url) for b in buttons] == [("btn-starterkit-info.guide", REPOSITORY)]
    assert REPOSITORY == "https://github.com/DobbyVPN/StarterKit"


def test_keyboard_is_translated_once() -> None:
    looked_up: list[str] = []
    service = NotificationService.__new__(NotificationService)

    def fake_translate(locale: Any, key: str) -> str:
        looked_up.append(key)
        return f"<{key}>"

    service._get_translated_text = fake_translate  # type: ignore[method-assign]
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="btn-goto.user-profile", url="https://t.me")]]
    )

    result = service._prepare_reply_markup(
        markup, disable_default_markup=False, delete_after=None, locale=Locale.RU, chat_id=1
    )

    assert result is not None
    assert all(not key.startswith("<") for key in looked_up)  # never a translated text
    texts = [b.text for row in result.inline_keyboard for b in row]  # type: ignore[union-attr]
    assert texts[0] == "<btn-goto.user-profile>"
    assert len(texts) == 2  # plus the close button


pytestmark_db = pytest.mark.skipif(os.getenv("DOBBY_DB_TESTS") != "1", reason="needs Postgres")


@pytest.fixture
async def container() -> AsyncIterator[AsyncContainer]:
    from aiogram_dialog import BgManagerFactory  # noqa: PLC0415
    from dishka import make_async_container  # noqa: PLC0415

    from src.infrastructure.di.providers import get_taskiq_providers  # noqa: PLC0415

    container = make_async_container(
        *get_taskiq_providers(),
        context={AppConfig: AppConfig.get(), BgManagerFactory: object()},
        skip_validation=True,
    )
    yield container
    await container.close()


@pytestmark_db
async def test_defaults_exist_before_anything_reads_settings(container: AsyncContainer) -> None:
    from sqlalchemy import delete  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415

    from src.application.common.dao import SettingsDao  # noqa: PLC0415
    from src.infrastructure.database.models.settings import Settings  # noqa: PLC0415
    from src.infrastructure.startup import ensure_defaults  # noqa: PLC0415

    # A fresh install: no settings row yet.
    async with container(scope=Scope.REQUEST) as request:
        session = await request.get(AsyncSession)
        await session.execute(delete(Settings))
        await session.commit()

    await ensure_defaults(container)
    await ensure_defaults(container)  # safe to run on every start

    async with container(scope=Scope.REQUEST) as request:
        settings = await (await request.get(SettingsDao)).get()
    assert settings is not None
