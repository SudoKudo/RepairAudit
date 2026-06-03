"""Quick verification of classification distribution for a classified CSV.

Reads the output CSV produced by `classify.py` and prints:
- total rows
- number and fraction with a non-empty `primary_expertise_area`
- counts per `primary_expertise_area` (descending)
- counts per secondary expertise area (descending)

Optionally writes the primary-distribution to a CSV via `--output`.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
import sys

import pandas as pd


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the classified-dataset distribution check."""
    p = argparse.ArgumentParser(description="Verify expertise classification distribution")
    p.add_argument(
        "--input",
        default="data/datasets/classified/dataset_classified.csv",
        help="Path to the classified CSV (default: data/datasets/classified/dataset_classified.csv)",
    )
    p.add_argument(
        "--primary-column",
        default="primary_expertise_area",
        help="Column name for primary expertise (default: primary_expertise_area)",
    )
    p.add_argument(
        "--secondary-column",
        default="secondary_expertise_areas",
        help="Column name for secondary expertise (default: secondary_expertise_areas)",
    )
    p.add_argument(
        "--output",
        help="Optional path to write primary-distribution CSV (columns: label,count)",
    )
    return p.parse_args()


def main() -> int:
    """Print high-level classification coverage and label counts for one CSV."""
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 2

    df = pd.read_csv(input_path, dtype=str).fillna("")
    total = len(df)

    primary_col = args.primary_column
    if primary_col not in df.columns:
        print(f"Primary column '{primary_col}' not found in CSV", file=sys.stderr)
        return 3

    primary_series = df[primary_col].astype(str).str.strip()
    classified_mask = primary_series != ""
    n_classified = int(classified_mask.sum())
    n_unclassified = total - n_classified

    print(f"Total rows: {total}")
    print(f"Classified (non-empty {primary_col}): {n_classified} ({n_classified/total:.1%})")
    print(f"Unclassified: {n_unclassified} ({n_unclassified/total:.1%})")

    primary_counts = primary_series.value_counts(dropna=False)
    # Move empty key (if any) to a readable label
    primary_counts = {("<empty>" if (k == "" or k is None) else k): int(v) for k, v in primary_counts.items()}

    print("\nPrimary expertise area counts:")
    for label, count in sorted(primary_counts.items(), key=lambda kv: kv[1], reverse=True):
        print(f"- {label}: {count}")

    # Parse secondary expertise areas column if present
    secondary_col = args.secondary_column
    if secondary_col in df.columns:
        counter = Counter()
        for raw in df[secondary_col].astype(str):
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                # If the column contains plain text lists like "[\"A\"]", ignore parse errors
                continue
            if isinstance(parsed, list):
                for item in parsed:
                    if item is None:
                        continue
                    counter[str(item).strip()] += 1

        print("\nSecondary expertise area counts:")
        if counter:
            for label, count in counter.most_common():
                print(f"- {label}: {count}")
        else:
            print("- (none found)")

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="") as fh:
            fh.write("label,count\n")
            for label, count in sorted(primary_counts.items(), key=lambda kv: kv[1], reverse=True):
                fh.write(f"{label},{count}\n")
        print(f"\nPrimary distribution written to: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
