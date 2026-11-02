#!/usr/bin/env python3
"""Smoke test of the file sql feature.

The public mysqltest case of the competition needs the obclient runtime, so
this script starts a fresh seekdb, runs the statements of
tools/deploy/mysql_test/test_suite/file_sql/t/file_sql_basic.test with a plain
mysql client and compares the output with the expected result file.
"""

import base64
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pymysql
import pymysql.cursors

ROOT = Path(__file__).resolve().parents[3]
SUITE = ROOT / "tools/deploy/mysql_test/test_suite/file_sql"
RESULT = SUITE / "r/mysql/file_sql_basic.result"
BINARY = ROOT / "build_release/src/observer/seekdb"
WORK = Path(os.environ.get("FILE_SQL_WORK", "/tmp/file_sql_smoke"))
PORT = int(os.environ.get("FILE_SQL_PORT", "2881"))
READY_TIMEOUT = int(os.environ.get("FILE_SQL_READY_TIMEOUT", "600"))


class RawCursor(pymysql.cursors.Cursor):
    """Keeps the values as the server sent them."""

    def _conv_row(self, row):
        return row


def fail(message):
    print("FAIL: " + message)
    sys.exit(1)


def parse_result(path):
    """Splits the result file into the blocks of a header and its rows.

    A block ends at a comment and at a line with a different number of
    columns, the result file holds the result sets of the test case one
    after the other."""
    blocks = []
    header = None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            if header is not None:
                blocks.append((header, rows))
                header = None
                rows = []
            continue
        fields = line.split("\t")
        if header is None:
            header = fields
        elif len(fields) != len(header):
            blocks.append((header, rows))
            header = fields
            rows = []
        else:
            rows.append(fields)
    if header is not None:
        blocks.append((header, rows))
    return blocks


def connect():
    return pymysql.connect(host="127.0.0.1", port=PORT, user="root", password="",
                            charset="utf8mb4", cursorclass=RawCursor, autocommit=True)


def query(sql):
    """Runs a statement and returns the column names and the rows."""
    conn = connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            if cursor.description is None:
                return [], []
            header = [column[0] for column in cursor.description]
            rows = [[None if value is None else value.decode("utf-8", "replace")
                     for value in row] for row in cursor.fetchall()]
            return header, rows
    finally:
        conn.close()


def prepare_data():
    data_dir = WORK / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    for name in ("basic.csv", "basic.jsonl", "invalid.txt"):
        shutil.copyfile(SUITE / "data" / name, data_dir / name)
    encoded = (SUITE / "data" / "simple.parquet.b64").read_text(encoding="utf-8").strip()
    (data_dir / "simple.parquet").write_bytes(base64.b64decode(encoded))
    return data_dir


def start_server():
    if not BINARY.exists():
        fail("the seekdb binary is missing: " + str(BINARY))
    base_dir = WORK / "instance"
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True)
    console = open(WORK / "seekdb.log", "wb")
    process = subprocess.Popen([str(BINARY), "--base-dir=" + str(base_dir), "--port=" + str(PORT),
                                "--nodaemon"],
                               cwd=str(base_dir), stdout=console, stderr=subprocess.STDOUT)
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if process.poll() is not None:
            fail("the server stopped, see " + str(WORK / "seekdb.log"))
        try:
            conn = connect()
            conn.close()
            print("the server is ready")
            return process
        except Exception:
            time.sleep(3)
    fail("the server is not ready after " + str(READY_TIMEOUT) + " seconds")


def stop_server(process):
    process.terminate()
    try:
        process.wait(timeout=60)
    except Exception:
        process.kill()


def main():
    if not WORK.exists():
        WORK.mkdir(parents=True)
    data_dir = prepare_data()
    blocks = parse_result(RESULT)
    if len(blocks) != 8:
        fail("the result file has " + str(len(blocks)) + " blocks, 8 are expected")
    csv = str(data_dir / "basic.csv")
    jsonl = str(data_dir / "basic.jsonl")
    parquet = str(data_dir / "simple.parquet")
    export = str(data_dir / "export.csv")

    process = start_server()
    failures = []
    try:
        def check(index, sql, name):
            header, rows = query(sql)
            expected_header, expected_rows = blocks[index]
            if header != expected_header or rows != expected_rows:
                failures.append("{}\n  sql:      {}\n  expected: {}\n  got:      {}".format(
                    name, sql, (expected_header, expected_rows), (header, rows)))

        # the feature is off by default
        header, rows = query("select @@session.enable_file_sql AS default_enabled")
        if rows != [["0"]]:
            failures.append("enable_file_sql should default to 0, got " + str(rows))

        # the feature stays off until it is enabled
        try:
            query("select count(*) from read_csv('" + csv + "')")
            failures.append("reading a file without enable_file_sql should fail")
        except pymysql.err.OperationalError as error:
            if error.args[0] != 1235:
                failures.append("the error of a disabled feature should be 1235, got " + str(error.args[0]))

        query("set session enable_file_sql = ON")

        check(1, "select id, name, score, active, day, created, "
                 "replace(note, char(10), '<NL>') AS note, id__2 "
                 "from read_csv('" + csv + "') f order by id", "csv scan")
        check(2, "select count(*) AS row_count, sum(id) AS id_sum, round(avg(score), 2) AS score_avg, "
                 "sum(active) AS active_count from read_csv('" + csv + "', header = true, "
                 "sample_size = -1) f", "csv aggregate")
        check(3, "select id, name, score, active, day, note from read_json('" + jsonl + "', "
                 "format = 'newline_delimited') f order by id", "jsonl scan")
        check(4, "describe select * from read_csv('" + csv + "')", "describe select")
        check(5, "select count(*) AS row_count, min(c1) AS min_c1, max(c1) AS max_c1, sum(c1) AS sum_c1 "
                 "from read_parquet('" + parquet + "') p", "parquet scan")
        check(6, "select substring_index(file, '/', -1) AS file from glob('" + str(data_dir) + "/*') "
                 "order by file", "glob")

        # the copy exports the query and the file can be read back
        query("copy (select id, name, score from read_csv('" + csv + "') order by id) to '" + export +
              "' (format csv, header, nullstr 'NULL')")
        check(7, "select id, name, score from read_csv('" + export + "') order by id", "copy read back")
    finally:
        stop_server(process)

    if failures:
        for failure in failures:
            print("FAIL: " + failure)
        sys.exit(1)
    print("the file sql smoke test passed")


if __name__ == "__main__":
    main()
