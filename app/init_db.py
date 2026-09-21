"""命令行建库工具：python -m app.init_db"""

from .database import init_db


def main() -> None:
    init_db()
    print("SQLite schema initialized.")


if __name__ == "__main__":
    main()
