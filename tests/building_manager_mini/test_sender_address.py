from __future__ import annotations

import pytest

from building_manager_mini.core.sender_address import normalize_sender_address


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        (" \t ", ""),
        ("  User.Name+tag@Example.COM  ", "User.Name+tag@Example.COM"),
        ("x" * 64 + "@" + "y" * 187 + ".z", "x" * 64 + "@" + "y" * 187 + ".z"),
    ],
)
def test_sender_address_normalization(value: str, expected: str) -> None:
    assert normalize_sender_address(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "x" * 64 + "@" + "y" * 188 + ".z",
        "x" * 65 + "@example.com",
        "@example.com",
        "a@",
        "a@example",
        "a@@example.com",
        ".a@example.com",
        "a.@example.com",
        "a..b@example.com",
        "a@.example.com",
        "a@example.com.",
        "a@example..com",
        "a b@example.com",
        "a\nb@example.com",
        "a@日本語.example",
        "Name <a@example.com>",
        *[f"a{char}b@example.com" for char in ",;<>\"'()[]\\"],
    ],
)
def test_sender_address_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="差出人メールアドレス"):
        normalize_sender_address(value)
