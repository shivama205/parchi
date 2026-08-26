"""The image must be able to import what the code imports.

A deploy failed because google-cloud-storage and python-multipart were
installed locally and missing from requirements.txt. Nothing caught it: the
Cloud Storage path was only reached once PARCHI_BUCKET was set, and until then
the container started fine on the fallback. This closes that gap.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Third-party top-level module -> the distribution that provides it.
PROVIDED_BY = {
    "google": ("google-adk", "google-genai", "google-cloud-firestore",
               "google-cloud-storage"),
    "fastapi": ("fastapi",),
    "uvicorn": ("uvicorn",),
    "pydantic": ("fastapi",),        # pulled in by fastapi
    "starlette": ("fastapi",),
    "pyarrow": (),                   # fixtures extra only, not in the image
    "huggingface_hub": (),
    "PIL": (),
}

STDLIB_OK = {"__future__"}


def _top_level_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import, our own package
                continue
            if node.module:
                found.add(node.module.split(".")[0])
    return found - STDLIB_OK


def test_requirements_covers_every_third_party_import():
    import sys

    requirements = (ROOT / "requirements.txt").read_text().lower()
    missing = []
    for path in sorted((ROOT / "parchi").rglob("*.py")):
        for module in _top_level_imports(path):
            if module in sys.stdlib_module_names or module == "parchi":
                continue
            dists = PROVIDED_BY.get(module)
            if dists is None:
                missing.append(f"{path.name}: unknown module {module!r} — add it "
                               f"to PROVIDED_BY and to requirements.txt")
            elif dists and not any(d in requirements for d in dists):
                missing.append(f"{path.name}: {module!r} needs one of {dists}")
    assert missing == [], "\n".join(missing)


def test_the_multipart_dependency_is_declared():
    """FastAPI's Form/UploadFile route raises at request time without it, so a
    missing declaration is invisible until somebody uploads a file."""
    assert "python-multipart" in (ROOT / "requirements.txt").read_text()


def test_cloud_storage_is_declared():
    """blobs.GcsBlobStore imports it lazily, so its absence only surfaces when
    PARCHI_BUCKET is set — which is exactly when it is needed."""
    assert "google-cloud-storage" in (ROOT / "requirements.txt").read_text()


def test_the_image_only_copies_the_package():
    """make_documents.py and fetch_fixtures.py need Pillow, pyarrow and
    huggingface_hub, none of which belong in a runtime image. The Dockerfile
    must therefore not copy them in."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY parchi ./parchi" in dockerfile
    assert "COPY . " not in dockerfile
