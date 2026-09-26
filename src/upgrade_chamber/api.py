"""Minimal public API before execution profiles are validated."""

from fastapi import FastAPI

from upgrade_chamber.profiles import public_catalog


app = FastAPI(title="Upgrade Chamber")


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/profiles")
def profiles() -> dict[str, list[dict[str, object]]]:
    return public_catalog()
