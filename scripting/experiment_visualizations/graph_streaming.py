#!/usr/bin/env python3

import argparse

import matplotlib.pyplot as plt
import pandas as pd


def parse_args():
    p = argparse.ArgumentParser(description="Graph CSV metrics")
    p.add_argument("csv", help="Path to CSV file")

    # Core toggles
    p.add_argument("--show-target", action="store_true", help="Plot target_bps")
    p.add_argument("--show-sent", action="store_true", help="Plot embd+chnk bytes sent")

    # Extra columns (flexible)
    p.add_argument("--add", nargs="*", default=[], help="Extra columns to plot")

    # Optional transforms
    p.add_argument("--rate", action="store_true", help="Convert bytes to bytes/sec using t_s")

    return p.parse_args()


def compute_rate(df, col):
    dt = df["t_s"].diff().fillna(0)
    return df[col].diff().fillna(0) / dt.replace(0, 1)


def main():
    args = parse_args()

    df = pd.read_csv(args.csv)

    plt.figure()

    # --- Target BPS ---
    if args.show_target:
        plt.plot(df["t_s"], df["target_bps"], label="target_bps")

    # --- Sent Bytes (embd + chnk) ---
    if args.show_sent:
        total_sent = df["embd_bytes_sent"] + df["chnk_bytes_sent"]

        if args.rate:
            total_sent = compute_rate(df.assign(tmp=total_sent), "tmp")

        plt.plot(df["t_s"], total_sent, label="embd+chnk")

    # --- Extra columns ---
    for col in args.add:
        if col not in df.columns:
            print(f"Skipping unknown column: {col}")
            continue

        y = df[col]
        if args.rate:
            y = compute_rate(df, col)

        plt.plot(df["t_s"], y, label=col)

    plt.xlabel("time (s)")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
