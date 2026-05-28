#!/usr/bin/env python3
"""
sanitise_logs.py

Processes all CSV files in a given folder, retaining only the specified
columns and writing sanitised versions with a _sanitised.csv suffix.

Usage:
    python3 grafana-sanitiser.py /path/to/folder
"""

import sys
import os
import csv

# Columns to retain in the sanitised output - CHANGE BASED ON REQUEST IF NEEDED
KEEP_COLUMNS = [
    "@timestamp",
    "country_code",
    "http_referer",
    "http_user_agent",
    "request_type",
    "request_url",
    "request_url_args",
    "status",
    "user_ip",
]


def sanitise_file(input_path, output_path):
    """Read a CSV, keep only KEEP_COLUMNS, write to output_path."""
    with open(input_path, newline="", encoding="utf-8") as infile:
        reader = csv.DictReader(infile)

        # Warn about any expected columns that are missing in this file
        missing = [col for col in KEEP_COLUMNS if col not in reader.fieldnames]
        if missing:
            print(f"  WARNING: The following columns were not found and will be skipped: {missing}")

        # Only write columns that actually exist in this file
        columns_to_write = [col for col in KEEP_COLUMNS if col in reader.fieldnames]

        with open(output_path, "w", newline="", encoding="utf-8") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=columns_to_write, extrasaction="ignore")
            writer.writeheader()

            row_count = 0
            for row in reader:
                writer.writerow({col: row[col] for col in columns_to_write})
                row_count += 1

    return row_count


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 sanitise_logs.py /path/to/folder")
        sys.exit(1)

    folder = sys.argv[1]

    if not os.path.isdir(folder):
        print(f"Error: '{folder}' is not a valid directory.")
        sys.exit(1)

    # Find all CSV files, excluding any already-sanitised outputs
    csv_files = [
        f for f in os.listdir(folder)
        if f.endswith(".csv") and not f.endswith("_sanitised.csv")
    ]

    if not csv_files:
        print("No CSV files found in the specified folder.")
        sys.exit(0)

    print(f"Found {len(csv_files)} CSV file(s) to process.\n")

    for filename in csv_files:
        input_path = os.path.join(folder, filename)
        base_name = os.path.splitext(filename)[0]
        output_path = os.path.join(folder, f"{base_name}_sanitised.csv")

        print(f"Processing: {filename}")
        row_count = sanitise_file(input_path, output_path)
        print(f"  Done — {row_count} rows written to {base_name}_sanitised.csv\n")

    print("All files processed.")


if __name__ == "__main__":
    main()