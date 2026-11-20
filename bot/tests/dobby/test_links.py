from uuid import UUID

import pytest

from src.infrastructure.dobby.links import TemplateLinkProvider


def test_template_link() -> None:
    links = TemplateLinkProvider("https://sub.example.invalid/s/{id}?app=dobby")
    uuid = UUID("00000000-0000-0000-0000-000000000001")
    assert links.url_for(uuid) == f"https://sub.example.invalid/s/{uuid}?app=dobby"


def test_template_without_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        TemplateLinkProvider("https://sub.example.invalid/")
