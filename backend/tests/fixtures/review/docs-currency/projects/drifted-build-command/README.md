# Widget Service

A small HTTP service that stores widgets. Synthetic fixture project.

## Requirements

- Python 3.11 or newer
- PostgreSQL for storage

## Configuration

Set WIDGET_DATABASE_URL before starting the service.

## Build and run

Build the service with `make build`, then start it with
`python -m widget.server`.
