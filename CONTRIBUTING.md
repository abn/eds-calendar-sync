# Contributing

Contributions welcome! Please test thoroughly before submitting changes, update documentation for
any new features, and run `pre-commit run --all-files` before committing.

## Development setup

Poetry 2.x is used for dependency management during development. The project venv is
configured with `system-site-packages = true` (see `poetry.toml`) so the system
`python3-gobject` package is accessible without reinstalling it from PyPI.

```bash
sudo dnf install python3-gobject evolution-data-server
pipx install poetry        # or: curl -sSL https://install.python-poetry.org | python3 -
poetry install             # creates .venv with system-site-packages
poetry shell               # activate the venv
```

## Pre-commit hooks

Ruff (lint + format) hooks are configured in `.pre-commit-config.yaml`:

```bash
pre-commit install          # install hooks once
pre-commit run --all-files  # run manually
```

## See also

- [Evolution Data Server Documentation](https://wiki.gnome.org/Projects/Evolution)
- [GNOME Calendar](https://wiki.gnome.org/Apps/Calendar)
- [iCalendar RFC 5545](https://tools.ietf.org/html/rfc5545)
