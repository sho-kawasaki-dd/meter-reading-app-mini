"""アプリケーションの起動入口。"""

import sys

from building_manager_mini.launcher import ApplicationLauncher
from meter_reading_mini.app import run_app


def _open_meter_reading_app() -> int:
    return run_app()


def main() -> int:
    launcher = ApplicationLauncher()
    launcher.register("meter-reading", _open_meter_reading_app)
    return launcher.open("meter-reading")


if __name__ == "__main__":
    sys.exit(main())
