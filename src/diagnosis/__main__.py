"""
python -m src.diagnosis outputs/run_<session_id>.jsonl
"""
import argparse

from .report import print_run_summary


def main() -> None:
    p = argparse.ArgumentParser(description="打印运行诊断 JSONL 摘要")
    p.add_argument("jsonl", help="run_<session_id>.jsonl 路径")
    args = p.parse_args()
    print_run_summary(args.jsonl)


if __name__ == "__main__":
    main()
