# Contributing to bambu-prep

Thanks for the interest! This project is pre-alpha — the public API is still moving — but bug reports, suggestions, and small PRs are welcome.

## Development setup

```
git clone https://github.com/ViddySlap/bambu-prep
cd bambu-prep
python -m venv .venv
.venv\Scripts\activate    # Windows
pip install -e .[dev]
pytest
```

Python 3.11+ is required (the loader uses stdlib `tomllib`).

## Project shape

- `bambu_prep/` — library code. One module per concern (`config`, `profiles`, `ams`, `meshes`, `plate`, `patch`, `cli`).
- `tests/` — pytest. Aim for behavior-focused tests, not implementation mirrors.
- `bambu_prep_config.example.toml` — config template. Real configs live at `%APPDATA%/bambu-prep/config.toml` and never carry secrets — credential values come in via `[secret_refs]` env vars.

## Submitting changes

1. Open an issue describing the change before non-trivial work, so we can align on the approach.
2. Keep PRs focused — one logical change per PR.
3. New behavior needs tests. Bug fixes need a regression test.
4. Run `pytest` and `ruff check` before pushing.

## Reporting issues

Useful: the Bambu Studio version, Python version, the `bambu-studio.exe` command bambu-prep tried to run (printed to stderr on failure), and the resulting stderr.

## Code of conduct

Be kind. Assume good faith.
