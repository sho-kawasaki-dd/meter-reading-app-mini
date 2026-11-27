from __future__ import annotations

import pytest

from building_manager_mini.launcher import ApplicationLauncher


def test_open_invokes_registered_application_and_returns_result() -> None:
    events: list[str] = []
    launcher = ApplicationLauncher()
    launcher.register("example", lambda: events.append("opened") or 23)

    assert launcher.open("example") == 23
    assert events == ["opened"]


def test_register_rejects_empty_and_duplicate_application_ids() -> None:
    launcher = ApplicationLauncher()
    launcher.register("example", lambda: 0)

    with pytest.raises(ValueError, match="空"):
        launcher.register("", lambda: 0)
    with pytest.raises(ValueError, match="既に登録"):
        launcher.register("example", lambda: 0)


def test_open_rejects_unregistered_application() -> None:
    with pytest.raises(LookupError, match="未登録"):
        ApplicationLauncher().open("missing")
