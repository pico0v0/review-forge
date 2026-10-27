import argparse

import uvicorn

from app.config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local ReviewForge Hub")
    parser.add_argument("--config-dir", help="Directory containing config.yaml and projects.yaml")
    args = parser.parse_args()
    if args.config_dir:
        import os

        os.environ["AIH_CONFIG_DIR"] = args.config_dir
    settings = load_settings()
    uvicorn.run("app.main:app", host=settings.server.host, port=settings.server.port)


if __name__ == "__main__":
    main()
