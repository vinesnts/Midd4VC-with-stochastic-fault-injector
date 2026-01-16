#!/usr/bin/env python3

from pathlib import Path
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
    # Experiments' paths
    folders = [
        '/home/vinesnts/Projetos/Midd4VC/experiments/20260115_135508',
        '/home/vinesnts/Projetos/Midd4VC/experiments/20260115_145508',
        '/home/vinesnts/Projetos/Midd4VC/experiments/20260115_155508',
        '/home/vinesnts/Projetos/Midd4VC/experiments/20260115_165508',
        '/home/vinesnts/Projetos/Midd4VC/experiments/20260115_175508',
        '/home/vinesnts/Projetos/Midd4VC/experiments/20260115_185508',
        '/home/vinesnts/Projetos/Midd4VC/experiments/20260115_195508',
        '/home/vinesnts/Projetos/Midd4VC/experiments/20260115_205508',
        '/home/vinesnts/Projetos/Midd4VC/experiments/20260115_125507',
        '/home/vinesnts/Projetos/Midd4VC/experiments/20260115_115507'
    ]
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            print(f"Error: {folder} is not a directory.", file=sys.stderr)
            sys.exit(2)

        paths = collect_csv_paths(folder)
        if not paths:
            print(f"No CSV files found in {folder}", file=sys.stderr)
            sys.exit(1)

        try:
            combined = combine_csvs(paths, sep=',')
        except Exception as e:
            print(f"Error combining CSVs: {e}", file=sys.stderr)
            sys.exit(3)

        output = str(folder) + ".csv"
        combined.to_csv(output, index=False)
        print(f"Wrote combined CSV to {output} ({len(combined)} rows, {len(combined.columns)} columns).")


if __name__ == "__main__":
    main()