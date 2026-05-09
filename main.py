#!/usr/bin/env python3

"""A tool allowing to maintain a database of image metadata."""

import datetime
import logging
import math
import pathlib
import sqlite3
from typing import Any, Optional

import click
import exiftool  # type: ignore

logger = logging.getLogger(__name__)

exif_helper = exiftool.ExifToolHelper()


def get_exif(xmp_path: pathlib.Path) -> Optional[dict[str, Any]]:
    """Get exif data of an image."""
    try:
        xmp_data = exif_helper.get_metadata(xmp_path.as_posix())
    except exiftool.exceptions.ExifToolExecuteError:
        return None

    try:
        image_name = xmp_data[0]["XMP:DerivedFrom"]
        image_path = xmp_path.with_name(image_name)
    except KeyError:
        image_path = xmp_path.with_suffix("")

    if not image_path.exists():
        return None

    try:
        image_data = exif_helper.get_metadata(image_path.as_posix())
    except exiftool.exceptions.ExifToolExecuteError:
        return None

    date = datetime.datetime.strptime(
        image_data[0]["EXIF:CreateDate"], "%Y:%m:%d %H:%M:%S"
    )
    data = {
        "name": xmp_path.as_posix(),
        "date": date,
        "focal_length": image_data[0]["EXIF:FocalLength"],
        "model": image_data[0]["EXIF:Model"],
        "lens_model": image_data[0]["MakerNotes:LensModel"],
        "rate": xmp_data[0]["XMP:Rating"],
        "mtime": xmp_path.stat().st_mtime,
        "changed": "changed" in xmp_data[0].get("XMP:Subject", []),
    }

    return data


SCHEMA = {
    "images": (
        "name PRIMARY KEY",
        "date",
        "focal_length",
        "model",
        "lens_model",
        "rate",
        "mtime",
        "changed",
    ),
}


def adapt_datetime_iso(val: datetime.datetime) -> str:
    """Adapt datetime.datetime to timezone-naive ISO 8601 date."""
    return val.replace(tzinfo=None).isoformat()


def create_db(output_file: pathlib.Path) -> sqlite3.Connection:
    """Create and open databse."""
    db = sqlite3.connect(output_file)
    db.row_factory = sqlite3.Row
    sqlite3.register_adapter(datetime.datetime, adapt_datetime_iso)

    cur = db.cursor()

    cur.execute("SELECT * FROM sqlite_master WHERE type='table';")
    tables = {row["name"]: row["sql"] for row in cur.fetchall()}

    for table, field_names in SCHEMA.items():
        fields = ", ".join(field_names)
        req = f"CREATE TABLE {table}({fields})"

        if table in tables:
            if tables[table] == req:
                continue
            cur.execute(f"DROP TABLE {table};")

        cur.execute(req)

    return db


def scan_files(db: sqlite3.Connection, folders: list[pathlib.Path]) -> None:
    """Scan data of modified files."""
    cur = db.cursor()

    req = "Select name, mtime FROM images;"
    mtime_res = cur.execute(req)
    mtimes = {m["name"]: m["mtime"] for m in mtime_res.fetchall()}

    for folder in folders:
        if not folder.is_dir():
            continue

        logger.info("Scanning %s", folder)
        data = []

        files = folder.glob("**/*.cr*.xmp", case_sensitive=False)
        for idx, file in enumerate(files):
            if idx > 0 and idx % 100 == 0:
                logger.info("Scanned %s files in %s...", idx, folder)
            prevmtime = mtimes.get(file.as_posix())
            if prevmtime and math.isclose(file.stat().st_mtime, prevmtime):
                logger.debug("Skipping %s", file)
                continue

            logger.debug("Scanning %s", file)
            exif_data = get_exif(file)
            if exif_data:
                data.append(exif_data)
            else:
                logger.warning("Failed to scan %s", file)

        cur.executemany(
            "INSERT OR REPLACE INTO images "
            "VALUES(:name, :date, :focal_length, :model, :lens_model, :rate, "
            ":mtime, :changed);",
            data,
        )
        db.commit()
        logger.info("Found %s files to update in %s", len(data), folder)

    cur.close()


def delete_removed_files(
    db: sqlite3.Connection, folders: list[pathlib.Path]
) -> None:
    """Remove database entries for removed files."""
    cur = db.cursor()

    req = "Select name FROM images;"
    dbfiles_res = cur.execute(req)
    dbfiles = {f["name"] for f in dbfiles_res.fetchall()}

    fsfiles = {
        file.as_posix()
        for folder in folders
        if folder.is_dir()
        for file in folder.glob("**/*.cr*.xmp", case_sensitive=False)
    }

    extra_files = dbfiles.difference(fsfiles)

    if extra_files:
        logger.info("Removing %s entries from database", len(extra_files))
        logger.debug("Removed entries : %s", extra_files)
        cur.executemany(
            "DELETE FROM images WHERE name = ?;",
            [[name] for name in extra_files],
        )

    cur.close()
    db.commit()


@click.command()
@click.option(
    "--database",
    "-o",
    type=click.Path(dir_okay=False, path_type=pathlib.Path),
    help="Database output file",
    default="photo.sqlite3",
)
@click.argument(
    "folders",
    type=click.Path(exists=True, path_type=pathlib.Path),
    nargs=-1,
)
def main(folders: list[pathlib.Path], database: pathlib.Path) -> None:
    """Handle main entry point of the application."""
    logging.basicConfig(level=logging.INFO)

    db = create_db(database)

    delete_removed_files(db, folders)
    scan_files(db, folders)

    db.commit()
    db.close()
