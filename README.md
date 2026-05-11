# bambu-prep

Agent-driven print prep for Bambu Lab printers. Consume a list of `PlateItem`s (STL path + scale + AMS slot) plus a print profile name, and emit a single arranged, unsliced Bambu Studio `.3mf` project file. Open in Bambu Studio, click Slice, click Print.

Built primarily for the Bambu Lab A1 + AMS lite. The library shells out to `bambu-studio.exe` for layout, profile loading, and `.3mf` assembly, and uses [bambulabs-api](https://github.com/mchrisgm/bambulabs-api) for live AMS-state queries.

## Status

Pre-alpha. Stage 1 implementation in progress.

## Install

```
pip install -e .
```

Python 3.11+ required.

## Use

```python
from bambu_prep import prepare_plate, PlateItem

result = prepare_plate(
    items=[PlateItem("case.stl", scale=s, ams_slot=1) for s in [1.01, 1.02, 1.03, 1.04, 1.05]],
    machine_profile="Bambu Lab A1 0.4 nozzle",
    process_profile="0.20mm Standard @BBL A1",
    output_path="iphone-cases.3mf",
)
# result.fit, result.requested, result.dropped, result.output_path
```

Or via the CLI:

```
python -m bambu_prep prepare --stl case.stl --scales 1.01:1.10:0.01 --slot 1 \
    --machine "Bambu Lab A1 0.4 nozzle" --process "0.20mm Standard @BBL A1" \
    --output iphone-cases.3mf
```

## Configuration

On first run, copy `bambu_prep_config.example.toml` to `%APPDATA%\bambu-prep\config.toml` (or set `BAMBU_PREP_CONFIG=path/to/config.toml`). Printer credentials (IP, access code, serial) are referenced via env vars so they never live in checked-in files.

## License

MIT
