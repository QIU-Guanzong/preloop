import os


def database_url() -> str:
    return os.environ["WIDGET_DATABASE_URL"]
