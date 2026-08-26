#!/usr/bin/env python3
"""Fetch the external handwriting fixture corpus. Run once before extraction work.

    python fetch_fixtures.py

Downloads a 100-prescription subset of the MIRAGE corpus from HuggingFace into
fixtures/external/, which is gitignored. Nothing from this dataset is committed
to this repository.

SOURCE AND LICENCE
------------------
chaithanyakota/100-handwritten-medical-records
https://huggingface.co/datasets/chaithanyakota/100-handwritten-medical-records
Licence: CC BY-ND 4.0 (Attribution — NoDerivatives)
https://creativecommons.org/licenses/by-nd/4.0/

Released alongside MIRAGE: Multimodal Identification and Recognition of
Annotations in Indian General Prescriptions (arXiv:2410.09729), whose full
743,118-image corpus is proprietary to Medyug Technology Pvt. Ltd. and is not
public. This 100-record subset is the part the authors released.

The NoDerivatives term shapes two design decisions in this codebase:

  * Images are fetched, never redistributed. This script exists so the corpus is
    reproducible without the repository carrying it.
  * Image bytes are written out exactly as stored, with no re-encoding, resizing
    or cropping. J2 shows the caregiver a highlight box drawn over the full
    image rather than a cropped region, so no modified copy is ever produced.

The dataset card does not state whether these prescriptions are real or
simulated. The MIRAGE paper describes its corpus as simulated records; this
script does not assert that about the subset beyond what each source says.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

REPO = "chaithanyakota/100-handwritten-medical-records"
LICENCE = "CC BY-ND 4.0"
DEST = Path(__file__).parent / "fixtures" / "external" / "mirage-100"


def main() -> int:
    try:
        import pyarrow.parquet as pq
        from huggingface_hub import snapshot_download
    except ImportError:
        print("Install dependencies first:  pip install -e '.[fixtures]'")
        return 1

    DEST.mkdir(parents=True, exist_ok=True)
    print(f"Fetching {REPO} ({LICENCE})…")
    local = snapshot_download(REPO, repo_type="dataset", allow_patterns=["data/*"])

    parquet_files = sorted(Path(local).rglob("*.parquet"))
    if not parquet_files:
        print("No parquet payload found in the repo snapshot.")
        return 1

    images_dir = DEST / "images"
    images_dir.mkdir(exist_ok=True)
    manifest = []

    for pf in parquet_files:
        table = pq.read_table(pf)
        print(f"  {pf.name}: {table.num_rows} rows, columns {table.column_names}")
        rows = table.to_pylist()
        for i, row in enumerate(rows):
            image_cell = next(
                (v for v in row.values() if isinstance(v, dict) and "bytes" in v), None
            )
            if image_cell is None or not image_cell.get("bytes"):
                continue
            # Written byte-for-byte as stored. No re-encoding: see the NoDerivatives
            # note above.
            raw = image_cell["bytes"]
            suffix = Path(image_cell.get("path") or "").suffix or _sniff(raw)
            name = f"rx-{i:03d}{suffix}"
            (images_dir / name).write_bytes(raw)
            text = next(
                (v for k, v in row.items() if isinstance(v, str) and k != "path"), None
            )
            manifest.append({"image": f"images/{name}", "ground_truth": text})

    (DEST / "manifest.json").write_text(
        json.dumps(
            {
                "source_repo": REPO,
                "source_url": f"https://huggingface.co/datasets/{REPO}",
                "licence": LICENCE,
                "licence_url": "https://creativecommons.org/licenses/by-nd/4.0/",
                "attribution": (
                    "MIRAGE: Multimodal Identification and Recognition of "
                    "Annotations in Indian General Prescriptions "
                    "(arXiv:2410.09729). Subset released by the authors. Full "
                    "corpus proprietary to Medyug Technology Pvt. Ltd."
                ),
                "note": (
                    "Not redistributed with this repository. Images written "
                    "byte-for-byte with no re-encoding, per the NoDerivatives term."
                ),
                "count": len(manifest),
                "records": manifest,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\n{len(manifest)} images -> {images_dir}")
    print(f"manifest            -> {DEST / 'manifest.json'}")
    print(f"\nLicence: {LICENCE}. Attribution required. Do not redistribute modified copies.")
    return 0


def _sniff(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    return ".bin"


if __name__ == "__main__":
    sys.exit(main())
