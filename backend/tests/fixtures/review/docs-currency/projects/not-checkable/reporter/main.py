import os

OUTPUT_DIR = os.environ.get("REPORTER_OUTPUT_DIR", "/tmp/reports")


def main() -> None:
    print(f"writing reports to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
