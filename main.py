"""`uv run main.py -c config.yaml` 로 실행할 수 있는 진입점."""

import sys

from db_migration.cli import main

if __name__ == "__main__":
    sys.exit(main())
