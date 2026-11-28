from __future__ import annotations

from importlib.metadata import entry_points

from building_manager_mini.launcher import ApplicationLauncher

_APPLICATION_ENTRY_POINT_GROUP = "building_manager_mini.application"


def main() -> int:
    registered_applications = tuple(entry_points(group=_APPLICATION_ENTRY_POINT_GROUP))
    if len(registered_applications) != 1:
        raise RuntimeError("起動するアプリの登録を一つに特定できません。")

    application = registered_applications[0]
    loaded_opener = application.load()
    if not callable(loaded_opener):
        raise TypeError(f"アプリの起動処理が呼び出し可能ではありません: {application.name}")

    def open_application() -> int:
        result = loaded_opener()
        if not isinstance(result, int):
            raise TypeError(f"アプリの終了コードが整数ではありません: {application.name}")
        return result

    launcher = ApplicationLauncher()
    launcher.register(application.name, open_application)
    return launcher.open(application.name)
