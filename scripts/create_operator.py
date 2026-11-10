"""Create local operator credentials without printing them or changing .env."""
from pathlib import Path
import secrets


def main():
    path = Path(__file__).resolve().parents[1] / ".local" / "operator.env"
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        path.write_text("OPERATOR_ID=demo_operator\nOPERATOR_TOKEN=" + secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    print("Local operator configuration ready in ignored .local/operator.env; token not printed.")


if __name__ == "__main__":
    main()
