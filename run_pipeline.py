"""Backward-compatible script entry point."""

from smart_finqa.cli import build_parser, main

__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    main()
