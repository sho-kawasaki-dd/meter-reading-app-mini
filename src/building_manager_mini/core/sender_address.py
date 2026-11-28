from __future__ import annotations

_FORBIDDEN_CHARACTERS = frozenset(",;<>\"'()[]\\")


def normalize_sender_address(value: str) -> str:
    address = value.strip()
    if not address:
        return ""
    if (
        len(address) > 254
        or address.count("@") != 1
        or any(ord(char) < 33 or ord(char) > 126 for char in address)
        or any(char in _FORBIDDEN_CHARACTERS for char in address)
    ):
        raise ValueError("差出人メールアドレスの形式が不正です。")

    local, domain = address.split("@")
    if (
        not 1 <= len(local) <= 64
        or not domain
        or "." not in domain
        or any(
            part.startswith(".") or part.endswith(".") or ".." in part for part in (local, domain)
        )
    ):
        raise ValueError("差出人メールアドレスの形式が不正です。")
    return address
