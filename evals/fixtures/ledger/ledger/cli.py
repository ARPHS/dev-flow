import argparse
import sys

from . import store


def build_parser():
    parser = argparse.ArgumentParser(prog="ledger", description="极简记账工具")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="新增一条记录")
    add.add_argument("amount", type=float)
    add.add_argument("note")

    sub.add_parser("list", help="列出所有记录")
    return parser


def format_entry(index, entry):
    return f"{index:>3}  {entry['amount']:>10.2f}  {entry['note']}"


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "add":
        entry = store.append({"amount": args.amount, "note": args.note})
        print(f"已添加：{entry['amount']:.2f} {entry['note']}")
        return 0
    if args.command == "list":
        entries = store.load()
        if not entries:
            print("暂无记录")
            return 0
        for index, entry in enumerate(entries, start=1):
            print(format_entry(index, entry))
        print(f"合计：{sum(e['amount'] for e in entries):.2f}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
