from widget.config import database_url


def main() -> None:
    print(f"serving widgets from {database_url()}")


if __name__ == "__main__":
    main()
