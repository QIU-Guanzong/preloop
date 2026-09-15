# Contributing to Preloop

We welcome contributions from the community.

## Code Style

- Python code should pass `ruff check .` and `ruff format .`.
- Frontend code should pass `npm run format:check` from `frontend/`.
- Keep changes focused and update docs when behavior or setup changes.

## Built-in tools

Every agent pays for a built-in tool in prompt budget and attack surface, so prefer
extending an existing tool with optional parameters over adding a new name.

If a change does add a built-in tool, the pull request body must state the
`default_enabled` decision explicitly and why: `true` means every account with a
matching tracker gets it without asking, `false` means it stays hidden until an
account, an agent or a flow allow-list selects it. Record the same value in
`backend/preloop/tools/builtin_defs.py` and in the REST catalogue. A pull request
that adds a tool without that statement is not ready for review.

## Testing

All new features and bug fixes should include tests when practical.

- Backend: `pytest`
- Frontend: `cd frontend && npm run test`

## Submitting Changes

1. Fork the repository and create a feature branch.
2. Make your changes and run the relevant checks locally.
3. Open a GitHub pull request with a clear description of the change.

Pull requests are reviewed by a core contributor before merge.
