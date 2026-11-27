"""旧起動入口。"""

import sys


def main() -> int:
    print(
        "この起動コマンドは廃止されました。"
        " `uv run python -m building_manager_mini` を実行してください。",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
