import os

USER_AGENT = os.environ["SCRAPER_USER_AGENT"]


def main() -> None:
    print(f"scraping as {USER_AGENT}")


if __name__ == "__main__":
    main()
