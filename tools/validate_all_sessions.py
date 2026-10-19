#!/usr/bin/env python3
"""Validate every collected session directory below a CSI data root."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from validate_session import validate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('data_root', nargs='?', default='data', type=Path)
    parser.add_argument('--require-core-actions', action='store_true')
    args = parser.parse_args()

    manifests = sorted(args.data_root.rglob('session_manifest.csv'))
    if not manifests:
        print(json.dumps({
            'status': 'FAIL',
            'data_root': str(args.data_root.resolve()),
            'error': 'No session_manifest.csv found',
        }, ensure_ascii=False, indent=2))
        return 1

    results = []
    for manifest in manifests:
        with manifest.open(encoding='utf-8', newline='') as handle:
            session_ids = sorted({
                row.get('session_id', '').strip()
                for row in csv.DictReader(handle)
                if row.get('session_id', '').strip()
            })
        if session_ids:
            results.extend(
                validate(
                    manifest.parent,
                    require_core_actions=args.require_core_actions,
                    session_id=session_id,
                )
                for session_id in session_ids
            )
        else:
            results.append(validate(
                manifest.parent,
                require_core_actions=args.require_core_actions,
            ))
    status = 'FAIL' if any(item['status'] == 'FAIL' for item in results) else (
        'WARN' if any(item['status'] == 'WARN' for item in results) else 'PASS')
    summary = {
        'status': status,
        'data_root': str(args.data_root.resolve()),
        'sessions': len(results),
        'recordings': sum(item['recordings'] for item in results),
        'results': results,
    }
    (args.data_root / 'validation_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if status == 'FAIL' else 0


if __name__ == '__main__':
    sys.exit(main())
