"""後方互換用。保存先定義の正本は building_manager_mini.paths。"""

from building_manager_mini.paths import (
    APP_NAME,
    DATA_DIR_ENV,
    AppPaths,
    ensure_dirs,
    resolve_paths,
)

__all__ = ["APP_NAME", "DATA_DIR_ENV", "AppPaths", "ensure_dirs", "resolve_paths"]
