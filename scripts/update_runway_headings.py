"""Build display-only magnetic runway bearings from a published FAA NASR cycle."""

import argparse
import csv
import io
import json
import math
import zipfile
from datetime import date
from pathlib import Path


def magnetic_heading(true_alignment, variation, hemisphere):
    if not str(true_alignment).strip() or not str(variation).strip():
        return None
    try:
        true, delta = float(true_alignment), float(variation)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (true, delta)):
        return None
    hemisphere = str(hemisphere).strip().upper()
    if not 0 <= true <= 360 or not 0 <= delta <= 180:
        return None
    if hemisphere not in ("E", "W") and delta != 0:
        return None
    signed_delta = -delta if hemisphere == "W" else delta
    return round((true - signed_delta) % 360) % 360 or 360


def rows(archive, name):
    member = next(n for n in archive.namelist() if Path(n).name == name)
    with archive.open(member) as stream:
        yield from csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))


def build(archive, cycle):
    bases = {}
    airports = {}
    for row in rows(archive, "APT_BASE.csv"):
        if row["ARPT_STATUS"] != "O":
            continue
        key = (row["SITE_NO"], row["SITE_TYPE_CODE"])
        ident = row["ICAO_ID"].strip() or row["ARPT_ID"].strip()
        bases[key] = (ident, row)
    for row in rows(archive, "APT_RWY_END.csv"):
        base = bases.get((row["SITE_NO"], row["SITE_TYPE_CODE"]))
        if not base:
            continue
        ident, airport = base
        heading = magnetic_heading(row["TRUE_ALIGNMENT"], airport["MAG_VARN"],
                                   airport["MAG_HEMIS"])
        if heading is None:
            continue
        if row["EFF_DATE"].replace("/", "-") != cycle:
            raise ValueError("FAA data effective date does not match requested cycle")
        entry = airports.setdefault(ident, {"runways": {}})
        entry["runways"][row["RWY_END_ID"].strip()] = heading
    if len(airports) < 1000:
        raise ValueError("Unexpectedly small FAA heading dataset")
    return {"source": "FAA NASR APT_BASE + APT_RWY_END", "cycle": cycle,
            "airports": airports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--cycle", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with zipfile.ZipFile(args.zip) as archive:
        data = build(archive, args.cycle.isoformat())
    args.output.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"Wrote {len(data['airports'])} airport display-heading records")


if __name__ == "__main__":
    main()
