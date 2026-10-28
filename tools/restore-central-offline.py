#!/usr/bin/env python3
"""Restore central SQLite only after the AURA server was stopped.

The tool refuses an active SQLite writer, verifies backup integrity and schema,
retains a copy of the original and aborts on existing WAL sidecars.
"""
import argparse
import datetime
import os
from pathlib import Path
import shutil
import sqlite3
import sys


def verify(path: Path):
    uri = 'file:' + str(path.resolve()) + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=0.25)
    try:
        if conn.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('Integritätsprüfung fehlgeschlagen')
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'central_events', 'schema_migrations', 'room_sync_receipts'}.issubset(tables):
            raise ValueError('Kein kompatibles AURA-Central-Schema')
    finally:
        conn.close()


def restore(source: Path, target: Path):
    if not source.is_file() or not target.is_file():
        raise ValueError('Sicherung und bestehende Zieldatenbank müssen vorhanden sein')
    if source.resolve() == target.resolve():
        raise ValueError('Sicherung und Ziel sind identisch')
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(target) + suffix).exists():
            raise ValueError('Ziel besitzt SQLite-Seitendateien; Server stoppen und sauber beenden: ' + suffix)
        if Path(str(source) + suffix).exists():
            raise ValueError('Quellsicherung ist nicht in sich abgeschlossen: ' + suffix)
    verify(source)
    # Keep a separate consistent pre-restore snapshot for rollback.
    conn = sqlite3.connect(str(target), timeout=0.25)
    backup = target.with_name(target.name + '.before-restore-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S') + '.sqlite')
    try:
        verify(target)
        with sqlite3.connect(str(backup)) as oldcopy:
            conn.backup(oldcopy)
        os.chmod(backup, 0o600)
    finally:
        conn.close()
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(target) + suffix).exists():
            raise ValueError('SQLite-Seitendatei nach Sicherung vorhanden; Wiederherstellung abgebrochen: '+suffix)
    temp = target.with_name(target.name + '.restore-pending')
    try:
        shutil.copy2(source, temp)
        os.chmod(temp, 0o600)
        verify(temp)
        # Concurrent daemon access remains an operational prohibition.
        # Don't replace a live SQLite file, even if it appears unlocked.
        os.replace(temp, target)
    finally:
        if temp.exists(): temp.unlink()
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--confirm-server-stopped', action='store_true')
    args = parser.parse_args()
    if not args.confirm_server_stopped:
        parser.error('Server zuerst vollständig stoppen; danach --confirm-server-stopped angeben')
    try:
        backup = restore(args.backup, args.database)
        print('Wiederherstellung abgeschlossen. Vorheriger Stand:', backup)
        print('AURA-Server erst jetzt wieder starten.')
    except (OSError, sqlite3.Error, ValueError) as error:
        print('Wiederherstellung abgebrochen:', error, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
