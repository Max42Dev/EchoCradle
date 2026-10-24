"""Offline visual service contracts: fake facade, real containers, no GPU/downloads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from orchestrator.service import ServiceConfig, _Runtime, create_app
from orchestrator.service_protocol import (
    ARTIFACT_LIMIT, PCM_LIMIT, VISUAL_LIMIT, JobSpec, ServiceError, TTL_SECONDS,
)


def png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
            + chunk(b"IEND", b""))


def glb() -> bytes:
    document = json.dumps({"asset": {"version": "2.0"}, "scene": 0,
                           "scenes": [{"nodes": []}]}).encode()
    document += b" " * (-len(document) % 4)
    return (struct.pack("<4sII", b"glTF", 2, 20 + len(document))
            + struct.pack("<I4s", len(document), b"JSON") + document)


class FakeFacade:
    def __init__(self) -> None:
        self._gpu_lock = threading.RLock()
        self.visual = SimpleNamespace(availability=lambda: {
            kind: {"available": True, "reasons": []}
            for kind in ("image", "image_to_3d", "animate_3d")
        })
        self.text = SimpleNamespace(loaded_model_id="offline")
        self.calls: list[tuple] = []
        self.paths: list[Path] = []
        self.bad_output = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def generate_image(
        self, text: str, output: Path, *, seed: int, negative_prompt: str,
        should_stop: object,
    ) -> None:
        self.calls.append(("image", text, seed, negative_prompt))
        self.paths.append(output)
        self.entered.set()
        if self.block:
            self.release.wait(3)
        self.text.loaded_model_id = None
        output.write_bytes(b"invalid" if self.bad_output else png())

    def generate_3d(
        self, source: Path, output: Path, *, seed: int, should_stop: object,
    ) -> None:
        self.calls.append(("image_to_3d", source.read_bytes(), seed))
        self.paths.extend([source, output])
        output.write_bytes(glb())

    def animate_3d(
        self, source: Path, output: Path, *, seed: int, should_stop: object,
    ) -> None:
        self.calls.append(("animate_3d", source.read_bytes(), seed))
        self.paths.extend([source, output])
        output.write_bytes(glb())

    def ensure_model(self, modality: object, *, model_id: str | None) -> None:
        self.calls.append(("reload", model_id))
        self.text.loaded_model_id = model_id


def runtime(**options: object) -> _Runtime:
    service = _Runtime(ServiceConfig(visual_config=Path("automatic.local.json"), **options),
                       None, None)
    service.state = "ready"
    service.facade = FakeFacade()
    service._visual_diagnostics = service._inspect_visual_availability()
    return service


async def finish(service: _Runtime) -> None:
    for worker in service.workers:
        worker.cancel()
    await asyncio.gather(*service.workers, return_exceptions=True)
    for executor in service.executors.values():
        executor.shutdown(wait=True)


@pytest.mark.parametrize("changes", [
    {"seed": True}, {"seed": -1}, {"seed": 2**32}, {"seed": 1.0},
    {"deadline_ms": 999}, {"deadline_ms": 3_600_001}, {"deadline_ms": True},
    {"session_id": None}, {"streaming": False}, {"turn_id": str(uuid4())},
    {"messages": []}, {"max_tokens": 1}, {"temperature": 1}, {"json_schema": {}},
    {"input_artifact_id": str(uuid4())}, {"negative_prompt": None},
    {"idempotency_key": None}, {"text": ""},
])
def test_strict_visual_fields(changes: dict) -> None:
    with pytest.raises(ServiceError):
        JobSpec.parse({"kind": "image", "text": "portrait", "seed": 0, **changes})


@pytest.mark.parametrize("kind", ["image", "image_to_3d", "animate_3d"])
def test_visual_required_fields_and_limits(kind: str) -> None:
    data = {"kind": kind, "seed": 2**32 - 1}
    data.update({"text": "portrait"} if kind == "image" else
                {"input_artifact_id": str(uuid4())})
    assert JobSpec.parse(data).deadline_ms == 600_000
    assert JobSpec.parse({**data, "deadline_ms": 3_600_000}).seed == 2**32 - 1
    for key in ("seed", "text" if kind == "image" else "input_artifact_id"):
        with pytest.raises(ServiceError):
            JobSpec.parse({k: v for k, v in data.items() if k != key})
    if kind != "image":
        for extra in ({"text": "no"}, {"negative_prompt": "no"}):
            with pytest.raises(ServiceError):
                JobSpec.parse({**data, **extra})


@pytest.mark.parametrize("kind", ["text", "tts", "stt"])
def test_visual_fields_do_not_relax_existing_parser(kind: str) -> None:
    data = {"kind": kind, "text": "hello"} if kind != "stt" else {
        "kind": kind, "input_artifact_id": str(uuid4()),
    }
    for extra in ({"seed": 1}, {"negative_prompt": ""}):
        with pytest.raises(ServiceError):
            JobSpec.parse({**data, **extra})


def test_chaining_reuse_idempotency_integrity_and_deleted_input() -> None:
    async def run() -> None:
        service = runtime()
        service.loop = asyncio.get_running_loop()
        service.workers = [asyncio.create_task(service.lane("visual"))]
        try:
            spec = JobSpec.parse({"kind": "image", "text": "portrait", "seed": 7,
                                  "negative_prompt": "blur", "idempotency_key": "first"})
            first = service.submit(spec)
            assert service.submit(spec) is first
            await asyncio.wait_for(service.queues["visual"].join(), 2)
            assert first.state == "succeeded" and first.result["seed"] == 7
            assert first.result["experimental"] is True
            assert first.result["byte_length"] == len(png())
            assert first.result["sha256"] == hashlib.sha256(png()).hexdigest()
            image_id = first.result["artifact_id"]
            second = service.submit(JobSpec.parse({
                "kind": "image_to_3d", "input_artifact_id": image_id, "seed": 8,
            }))
            reused = service.submit(second.spec)
            assert image_id in service.artifacts
            assert second.input_visual == png()
            assert second.input_visual is not service.artifacts[image_id].pcm
            removed = service.artifacts.pop(image_id)
            service.artifact_bytes -= len(removed.pcm)
            await asyncio.wait_for(service.queues["visual"].join(), 2)
            assert second.state == reused.state == "succeeded"
            mesh_id = second.result["artifact_id"]
            final = service.submit(JobSpec.parse({
                "kind": "animate_3d", "input_artifact_id": mesh_id, "seed": 9,
            }))
            await asyncio.wait_for(service.queues["visual"].join(), 2)
            assert final.state == "succeeded"
            assert final.result["media_type"] == "model/gltf-binary"
            assert mesh_id in service.artifacts
            assert second.input_visual is None and final.input_visual is None
            assert [call[0] for call in service.facade.calls] == [
                "image", "image_to_3d", "image_to_3d", "animate_3d",
            ]
            assert not any(path.exists() for path in service.facade.paths)
            with pytest.raises(ServiceError) as caught:
                service.submit(JobSpec.parse({"kind": "image", "text": "different", "seed": 7,
                                               "idempotency_key": "first"}))
            assert caught.value.code == "IDEMPOTENCY_CONFLICT"
        finally:
            await finish(service)

    asyncio.run(run())


def test_unavailable_and_shippable_are_independent_of_text_startup() -> None:
    service = runtime(shippable_only=True)
    try:
        assert service.capabilities()["kinds"]["text"]["state"] == "ready"
        assert service.capabilities()["kinds"]["image"]["state"] == "unavailable"
        with pytest.raises(ServiceError) as caught:
            service.submit(JobSpec.parse({"kind": "image", "text": "x", "seed": 0}))
        assert caught.value.code == "NOT_SHIPPABLE"
        service.config = ServiceConfig()
        with pytest.raises(ServiceError) as caught:
            service.submit(JobSpec.parse({"kind": "image", "text": "x", "seed": 0}))
        assert caught.value.code == "VISUAL_UNAVAILABLE"
        service.config = ServiceConfig(visual_config=Path("missing.json"))
        service.facade.visual = None
        assert service.capabilities()["kinds"]["image"]["state"] == "unavailable"
    finally:
        for executor in service.executors.values():
            executor.shutdown(wait=True)


@pytest.mark.parametrize("deadline", [False, True])
def test_cancelled_native_completion_never_publishes(deadline: bool) -> None:
    async def run() -> None:
        service = runtime()
        service.loop = asyncio.get_running_loop()
        facade = service.facade
        facade.block = True
        job = service.submit(JobSpec.parse({"kind": "image", "text": "x", "seed": 1}))
        service.workers = [asyncio.create_task(service.lane("visual"))]
        try:
            assert await asyncio.to_thread(facade.entered.wait, 1)
            service.cancel_job(job, deadline=deadline)
            assert job.host_pending
            job.completed = time.monotonic() - TTL_SECONDS - 1
            service.prune()
            assert job.id in service.jobs
            facade.release.set()
            await asyncio.wait_for(service.queues["visual"].join(), 2)
            assert not service.artifacts and not job.host_pending and job.result is None
            assert job.state == ("failed" if deadline else "cancelled")
            assert not any(path.exists() for path in facade.paths)
        finally:
            facade.release.set()
            await finish(service)

    asyncio.run(run())


def test_bad_output_and_wrong_input_mime() -> None:
    async def run() -> None:
        service = runtime()
        service.loop = asyncio.get_running_loop()
        service.facade.bad_output = True
        pcm = service.publish(b"\0\0", 16000)
        try:
            with pytest.raises(ServiceError, match="media type"):
                service.submit(JobSpec.parse({"kind": "image_to_3d", "seed": 1,
                                               "input_artifact_id": pcm["artifact_id"]}))
            assert pcm["artifact_id"] in service.artifacts
            job = service.submit(JobSpec.parse({"kind": "image", "text": "x", "seed": 1}))
            service.workers = [asyncio.create_task(service.lane("visual"))]
            await asyncio.wait_for(service.queues["visual"].join(), 2)
            assert job.state == "failed" and job.error["code"] == "INVALID_OUTPUT"
            assert len(service.artifacts) == 1
            assert not any(path.exists() for path in service.facade.paths)
        finally:
            await finish(service)

    asyncio.run(run())


def test_input_copies_share_aggregate_quota_and_queued_cancel_releases() -> None:
    service = runtime()
    try:
        artifact = service.publish_visual(png(), "image/png")
        spec = JobSpec.parse({"kind": "image_to_3d", "seed": 1,
                              "input_artifact_id": artifact["artifact_id"]})
        job = service.submit(spec)
        service.artifact_bytes = ARTIFACT_LIMIT - len(png())
        with pytest.raises(ServiceError) as caught:
            service.submit(spec)
        assert caught.value.code == "RESOURCE_EXHAUSTED"
        service.cancel_job(job)
        assert job.input_visual is None and service.queues["visual"].empty()
        assert service.submit(spec).input_visual == png()
    finally:
        for executor in service.executors.values():
            executor.shutdown(wait=True)


def test_completed_visual_input_is_released_before_publication() -> None:
    async def run() -> None:
        service = runtime()
        service.loop = asyncio.get_running_loop()
        # Valid GLBs with padded JSON exercise the real aggregate byte quota.
        def sized_glb(size: int) -> bytes:
            document = glb()[20:].ljust(size - 20, b" ")
            return (struct.pack("<4sII", b"glTF", 2, size)
                    + struct.pack("<I4s", len(document), b"JSON") + document)

        source = sized_glb(24 * 1024 * 1024)
        output = sized_glb(20 * 1024 * 1024)
        artifact = service.publish_visual(source, "model/gltf-binary")
        job = service.submit(JobSpec.parse({
            "kind": "animate_3d", "seed": 1, "input_artifact_id": artifact["artifact_id"],
        }))
        assert service.artifact_bytes + len(job.input_visual) + len(output) > ARTIFACT_LIMIT

        def infer(current: object) -> tuple[bytes, str]:
            assert current.host_pending and current.input_visual == source
            return output, "model/gltf-binary"

        service.infer = infer
        service.workers = [asyncio.create_task(service.lane("visual"))]
        try:
            await asyncio.wait_for(service.queues["visual"].join(), 3)
            assert job.state == "succeeded", job.error
            assert job.input_visual is None and not job.host_pending
            assert artifact["artifact_id"] in service.artifacts
            assert service.artifact_bytes == len(source) + len(output)
            assert service.artifacts[job.result["artifact_id"]].pcm == output
        finally:
            await finish(service)

    asyncio.run(run())


def test_artifact_routes_preserve_pcm_and_validate_visuals() -> None:
    async def run() -> None:
        app = create_app(ServiceConfig(port=8765), token="offline")
        service = app.state.service
        service.state = "ready"
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
                headers={"Authorization": "Bearer offline"},
            ) as client:
                for media_type, data in [("image/png", png()), ("model/gltf-binary", glb())]:
                    response = await client.post("/v1/artifacts", content=data,
                                                 headers={"Content-Type": media_type})
                    assert response.status_code == 201, response.text
                    result = response.json()
                    downloaded = await client.get(f"/v1/artifacts/{result['artifact_id']}")
                    assert downloaded.content == data
                    assert downloaded.headers["content-type"] == media_type
                    assert downloaded.headers["x-content-sha256"] == result["sha256"]
                    assert "x-sample-rate" not in downloaded.headers
                    malformed = await client.post("/v1/artifacts", content=b"invalid",
                                                  headers={"Content-Type": media_type})
                    assert malformed.status_code == 422
                    excessive = await client.post("/v1/artifacts", content=b"",
                        headers={"Content-Type": media_type,
                                 "Content-Length": str(VISUAL_LIMIT + 1)})
                    assert excessive.status_code == 413
                response = await client.post("/v1/artifacts", content=b"\0\0", headers={
                    "Content-Type": "application/octet-stream", "X-Sample-Rate": "16000",
                })
                assert set(response.json()) == {"artifact_id"}
                downloaded = await client.get(f"/v1/artifacts/{response.json()['artifact_id']}")
                assert downloaded.headers["x-sample-rate"] == "16000"
                assert downloaded.headers["x-sample-count"] == "1"
                assert downloaded.content == b"\0\0"
                too_big = await client.post("/v1/artifacts", content=b"", headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(PCM_LIMIT + 1),
                })
                assert too_big.status_code == 413
        finally:
            await finish(service)

    asyncio.run(run())


@pytest.mark.parametrize("lock_name", ["_gpu_lock", "gpu_lock"])
def test_text_reloads_under_shared_gpu_lock_after_visual_work(lock_name: str) -> None:
    async def run() -> None:
        service = runtime()
        service.loop = asyncio.get_running_loop()
        service.diagnostics["actual"]["text"] = {"model_id": "selected-text"}
        if lock_name == "gpu_lock":
            service.facade.gpu_lock = service.facade._gpu_lock
            del service.facade._gpu_lock
        service.facade.block = True
        image = service.submit(JobSpec.parse({"kind": "image", "text": "x", "seed": 1}))
        service.workers = [asyncio.create_task(service.lane("visual")),
                           asyncio.create_task(service.lane("text"))]
        original = service._infer

        def infer(job: object) -> object:
            if job.spec.kind == "text":
                assert service.facade.text.loaded_model_id == "selected-text"
                service.facade.calls.append(("text",))
                return {"text": "ok", "calls": []}
            return original(job)

        service._infer = infer
        try:
            assert await asyncio.to_thread(service.facade.entered.wait, 1)
            text = service.submit(JobSpec.parse({"kind": "text", "text": "hello"}))
            await asyncio.sleep(0)
            assert not any(call[0] == "text" for call in service.facade.calls)
            service.facade.release.set()
            await asyncio.wait_for(asyncio.gather(service.queues["visual"].join(),
                                                  service.queues["text"].join()), 2)
            assert image.state == text.state == "succeeded"
            assert [call[0] for call in service.facade.calls] == ["image", "reload", "text"]
        finally:
            service.facade.release.set()
            await finish(service)

    asyncio.run(run())


def test_backend_availability_diagnostics_and_exceptions() -> None:
    service = runtime()
    try:
        service.facade.visual.availability = lambda: {
            "image": {"available": True, "reasons": []},
            "image_to_3d": {"available": False, "reasons": ["Missing cached worker"]},
        }
        service._visual_diagnostics = service._inspect_visual_availability()
        kinds = service.capabilities()["kinds"]
        assert kinds["image"]["state"] == "ready"
        assert kinds["image_to_3d"]["state"] == "unavailable"
        assert kinds["image_to_3d"]["reasons"] == ["Missing cached worker"]
        assert kinds["animate_3d"]["state"] == "unavailable"

        def unavailable() -> dict:
            raise RuntimeError("private configuration failure")

        service.facade.visual.availability = unavailable
        service._visual_diagnostics = service._inspect_visual_availability()
        kinds = service.capabilities()["kinds"]
        assert kinds["text"]["state"] == "ready"
        assert kinds["image"]["state"] == "unavailable"
        assert "private" not in str(kinds)
    finally:
        for executor in service.executors.values():
            executor.shutdown(wait=True)


def test_startup_inspection_is_off_loop_and_requests_use_cached_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        service = runtime()
        service.state = "starting"
        service._visual_diagnostics = None
        entered = threading.Event()
        release = threading.Event()
        inspections: list[int] = []
        loop_thread = threading.get_ident()

        def inspect_deployment() -> dict:
            inspections.append(threading.get_ident())
            entered.set()
            assert release.wait(3), "Event loop stalled during visual inspection"
            return {"image": {"available": True, "reasons": []},
                    "image_to_3d": {"available": False, "reasons": ["Missing worker"]}}

        monkeypatch.setattr(service, "_load_profile", lambda: service.diagnostics)
        service.facade.visual.availability = inspect_deployment
        try:
            await service.start()
            assert await asyncio.to_thread(entered.wait, 1)
            # The loop remains available while the native inspection is blocked.
            assert service.state == "starting"
            assert service.capabilities()["kinds"]["image"]["state"] == "unavailable"
            release.set()
            await asyncio.wait_for(service.startup_task, 2)
            assert len(inspections) == 1 and inspections[0] != loop_thread
            assert service.state == "ready"

            def unexpected_check() -> dict:
                pytest.fail("Request path repeated deployment inspection")

            service.facade.visual.availability = unexpected_check
            for _ in range(3):
                kinds = service.capabilities()["kinds"]
                assert kinds["image"]["state"] == "ready"
                assert kinds["image_to_3d"]["reasons"] == ["Missing worker"]
                assert kinds["animate_3d"]["state"] == "unavailable"
                service.submit(JobSpec.parse({"kind": "image", "text": "x", "seed": 1}))
            await asyncio.wait_for(service.queues["visual"].join(), 2)
            assert all(job.state == "succeeded" for job in service.jobs.values())
            assert len(inspections) == 1
        finally:
            release.set()
            if service.startup_task:
                await service.startup_task
            if service.housekeeping:
                service.housekeeping.cancel()
                await asyncio.gather(service.housekeeping, return_exceptions=True)
            await finish(service)

    asyncio.run(run())


@pytest.mark.parametrize("configured", [False, True])
def test_uninspected_deployment_is_not_assumed_ready(configured: bool) -> None:
    service = runtime()
    service._visual_diagnostics = None
    if not configured:
        service.config = ServiceConfig()
    try:
        assert service.capabilities()["kinds"]["text"]["state"] == "ready"
        assert service.visual_availability()["image"]["state"] == "unavailable"
        with pytest.raises(ServiceError) as caught:
            service.submit(JobSpec.parse({"kind": "image", "text": "x", "seed": 1}))
        assert caught.value.code == "VISUAL_UNAVAILABLE"
    finally:
        for executor in service.executors.values():
            executor.shutdown(wait=True)


@pytest.mark.parametrize("kind", ["animate_3d", "stt"])
@pytest.mark.parametrize("deadline", [False, True])
def test_cancelled_native_keeps_input_until_return(kind: str, deadline: bool) -> None:
    async def run() -> None:
        service = runtime(profile="voice")
        service.loop = asyncio.get_running_loop()
        entered = threading.Event()
        release = threading.Event()
        visual = kind == "animate_3d"
        data = glb() if visual else b"\0\0"
        artifact = (service.publish_visual(data, "model/gltf-binary") if visual else
                    service.publish(data, 16000))
        spec = {"kind": kind, "input_artifact_id": artifact["artifact_id"]}
        if visual:
            spec["seed"] = 1
        job = service.submit(JobSpec.parse(spec))

        def infer(current: object) -> object:
            entered.set()
            assert release.wait(3)
            assert (current.input_visual if visual else current.input_pcm) == data
            return (glb(), "model/gltf-binary") if visual else {"text": "ignored"}

        service.infer = infer
        lane = "visual" if visual else kind
        service.workers = [asyncio.create_task(service.lane(lane))]
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            service.cancel_job(job, deadline=deadline)
            assert job.host_pending
            assert (job.input_visual if visual else job.input_pcm) == data
            release.set()
            await asyncio.wait_for(service.queues[lane].join(), 2)
            assert job.input_visual is None and job.input_pcm is None
            assert not job.host_pending and job.result is None
            assert job.state == ("failed" if deadline else "cancelled")
            assert len(service.artifacts) == (1 if visual else 0)
        finally:
            release.set()
            await finish(service)

    asyncio.run(run())


def test_one_visual_lane_retains_input_until_native_completion() -> None:
    async def run() -> None:
        service = runtime()
        service.loop = asyncio.get_running_loop()
        facade = service.facade
        facade.block = True
        image = service.submit(JobSpec.parse({"kind": "image", "text": "x", "seed": 1}))
        artifact = service.publish_visual(png(), "image/png")
        mesh = service.submit(JobSpec.parse({"kind": "image_to_3d", "seed": 2,
                                            "input_artifact_id": artifact["artifact_id"]}))
        service.workers = [asyncio.create_task(service.lane("visual"))]
        try:
            assert await asyncio.to_thread(facade.entered.wait, 1)
            assert mesh.state == "queued" and mesh.input_visual == png()
            service.artifacts[artifact["artifact_id"]].created -= TTL_SECONDS + 1
            service.prune()
            assert artifact["artifact_id"] not in service.artifacts
            facade.release.set()
            await asyncio.wait_for(service.queues["visual"].join(), 2)
            assert image.state == mesh.state == "succeeded"
            assert mesh.input_visual is None
            assert facade.calls[1] == ("image_to_3d", png(), 2)
        finally:
            facade.release.set()
            await finish(service)

    asyncio.run(run())