"""Save your Anthropic API key to .env (gitignored) for local runs."""

import getpass
import pathlib

ENV = pathlib.Path(__file__).parent / ".env"
NAME = "ANTHROPIC_API_KEY"


def main() -> None:
    key = getpass.getpass(f"{NAME}: ").strip()
    if not key:
        raise SystemExit("No key entered; nothing changed.")
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    lines = [line for line in lines if not line.startswith(f"{NAME}=")]
    lines.append(f"{NAME}={key}")
    ENV.write_text("\n".join(lines) + "\n")
    ENV.chmod(0o600)
    print(f"Saved {NAME} to {ENV.name}")


if __name__ == "__main__":
    main()
