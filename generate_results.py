#!/usr/bin/env python3
"""
generate_results.py

Read all CSV files in a folder and combine them into one DataFrame such that
rows correspond between files (i.e. combination is horizontal concatenation).
By default each file's columns are prefixed with the file stem to avoid name collisions.

Usage:
    python generate_results.py --input-folder path/to/csvs --output combined.csv
"""

from pathlib import Path
import argparse
import re
import pandas as pd
import sys




def natural_sort_key(s: str):
    parts = re.split(r'(\d+)', s)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def collect_csv_paths(folder: Path, pattern: str = "*.csv"):
    files = sorted(folder.glob(pattern), key=lambda p: natural_sort_key(p.name))
    return files


def combine_csvs(
    paths,
    sep=",",
    add_prefix=True,
    prefix_sep="_",
):
    if not paths:
        raise ValueError("No CSV files to combine.")

    dfs = []
    expected_len = None
    for p in paths:
        df = pd.read_csv(p, sep=sep)
        if expected_len is None:
            expected_len = len(df)
        elif len(df) != expected_len:
            raise ValueError(
                f"Row count mismatch: {paths[0].name} has {expected_len} rows but {p.name} has {len(df)} rows."
            )
        df = df.reset_index(drop=True)
        if add_prefix:
            df = df.add_prefix(p.stem + prefix_sep)
        dfs.append(df)

    combined = pd.concat(dfs, axis=1)
    return combined


def main():
    parser = argparse.ArgumentParser(description="Combine CSV files horizontally (rows aligned).")
    parser.add_argument("--input-folder", "-i", required=True, help="Folder containing CSV files.")
    parser.add_argument("--output", "-o", default="combined.csv", help="Output CSV file path.")
    parser.add_argument("--sep", default=",", help="CSV separator (default: ',').")
    parser.add_argument("--no-prefix", action="store_true", help="Don't prefix columns with filename.")
    args = parser.parse_args()

    folder = Path(args.input_folder)
    if not folder.is_dir():
        print(f"Error: {folder} is not a directory.", file=sys.stderr)
        sys.exit(2)

    paths = collect_csv_paths(folder)
    if not paths:
        print(f"No CSV files found in {folder}", file=sys.stderr)
        sys.exit(1)

    try:
        combined = combine_csvs(paths, sep=args.sep, add_prefix=not args.no_prefix)
    except Exception as e:
        print(f"Error combining CSVs: {e}", file=sys.stderr)
        sys.exit(3)

    combined.to_csv(args.output, index=False)
    print(f"Wrote combined CSV to {args.output} ({len(combined)} rows, {len(combined.columns)} columns).")


if __name__ == "__main__":
    main()