"""Disposable Compose project; a fresh database and bucket for every test."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from tests.support.providers import Providers

from prospector.config import clear_settings_cache, get_settings
from prospector.store.checkpoint import close_pool, setup_checkpointer
from prospector.store.database import clear_engine_cache
from prospector.store.object_store import ObjectStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def providers(monkeypatch):
    scripted = Providers()
    monkeypatch.setattr("urllib.request.urlopen", scripted.urlopen)
    yield scripted
    scripted.close()


@pytest.fixture(scope="session")
def test_services():
    project = "prospector-tests-" + uuid4().hex[:12]
    compose = ["docker", "compose", "-f", str(ROOT / "tests/compose.yml"), "-p", project]

    def run(*args):
        return subprocess.run(
            [*compose, *args], check=True, capture_output=True, text=True, timeout=120
        ).stdout.strip()

    try:
        run("up", "-d", "--wait", "--wait-timeout", "60")
        pg = run("port", "postgres", "5432")
        s3 = run("port", "minio", "9000")
        yield f"postgresql://test:test@{pg}/postgres", f"http://{s3}"
    finally:
        # Only the UUID-named project created here; never the user's Compose project.
        run("down", "--volumes")


@pytest.fixture(autouse=True)
def storage(isolated_environment, test_services, monkeypatch):
    admin_url, endpoint = test_services
    name = "test_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    monkeypatch.setenv("DATABASE_URL", admin_url.rsplit("/", 1)[0] + "/" + name)
    monkeypatch.setenv("S3_ENDPOINT", endpoint)
    monkeypatch.setenv("S3_ACCESS_KEY", "test-admin")
    monkeypatch.setenv("S3_SECRET_KEY", "test-password")
    monkeypatch.setenv("S3_BUCKET", name.replace("_", "-"))
    clear_settings_cache()
    store = ObjectStore()
    bucket_created = False
    try:
        store.ensure_bucket()
        bucket_created = True
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        setup_checkpointer()
        yield get_settings()
    finally:
        close_pool()
        clear_engine_cache()
        clear_settings_cache()
        client = store._client
        try:
            if bucket_created:
                for page in client.get_paginator("list_objects_v2").paginate(Bucket=store.bucket):
                    objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                    if objects:
                        client.delete_objects(Bucket=store.bucket, Delete={"Objects": objects})
                client.delete_bucket(Bucket=store.bucket)
        finally:
            client.close()
            with psycopg.connect(admin_url, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
