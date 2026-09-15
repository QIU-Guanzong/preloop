# Ledger Tool

A command line ledger. Synthetic fixture project.

## Requirements

- click as the command line framework
- Redis for the shared queue

## Configuration

Set LEDGER_HOME to the directory holding the ledger files.

## Build and run

Run the tests with `make test`, then start the tool with
`python -m ledger.cli`.
