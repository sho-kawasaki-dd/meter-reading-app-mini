CREATE TABLE staff (
    staff_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(staff_id) = 36 AND substr(staff_id, 9, 1) = '-' AND
        substr(staff_id, 14, 1) = '-' AND substr(staff_id, 19, 1) = '-' AND
        substr(staff_id, 24, 1) = '-' AND
        length(replace(staff_id, '-', '')) = 32 AND
        replace(staff_id, '-', '') NOT GLOB '*[^0-9a-f]*'
    ),
    display_name TEXT NOT NULL UNIQUE CHECK (length(trim(display_name)) > 0),
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    sender_address TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE staff_legacy_id_map (
    source_application TEXT NOT NULL CHECK (length(trim(source_application)) > 0),
    source_database_id TEXT NOT NULL CHECK (
        length(source_database_id) = 36 AND substr(source_database_id, 9, 1) = '-' AND
        substr(source_database_id, 14, 1) = '-' AND substr(source_database_id, 19, 1) = '-' AND
        substr(source_database_id, 24, 1) = '-' AND
        length(replace(source_database_id, '-', '')) = 32 AND
        replace(source_database_id, '-', '') NOT GLOB '*[^0-9a-f]*'
    ),
    legacy_staff_id INTEGER NOT NULL CHECK (legacy_staff_id > 0),
    staff_id TEXT NOT NULL REFERENCES staff(staff_id),
    PRIMARY KEY (source_application, source_database_id, legacy_staff_id)
) STRICT;

CREATE TABLE common_settings (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    timezone TEXT NOT NULL CHECK (length(trim(timezone)) > 0)
) STRICT;
