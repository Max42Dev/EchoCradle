"""Scheduling, rollback and quotas with fake hosts/transports only."""

import asyncio
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator.service import ServiceConfig, _Connection, _Runtime, _Session
from orchestrator.service_protocol import JobSpec, ServiceError, TTL_SECONDS


def make_runtime(profile: str = "text") -> _Runtime:
    runtime = _Runtime(ServiceConfig(profile=profile), None, None)
    runtime.state = "ready"
    return runtime


async def finish(runtime: _Runtime) -> None:
    for worker in runtime.workers:
        worker.cancel()
    if runtime.housekeeping:
        runtime.housekeeping.cancel()
    await asyncio.gather(*runtime.workers, *([runtime.housekeeping] if runtime.housekeeping else []),
                         return_exceptions=True)
    for task in list(runtime.socket_cleanup):
        task.cancel()
    await asyncio.gather(*runtime.socket_cleanup, return_exceptions=True)
    for executor in runtime.executors.values():
        executor.shutdown(wait=True)


def test_stalled_close_cannot_hold_lane_or_reaper() -> None:
    async def run() -> None:
        runtime = make_runtime()
        runtime.loop = asyncio.get_running_loop()
        close_started = asyncio.Event()
        close_cancelled = asyncio.Event()

        class Socket:
            async def close(self, **kwargs: object) -> None:
                close_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    close_cancelled.set()

        sid = str(uuid4())
        session = _Session(sid, [])
        runtime.sessions[sid] = session
        connection = _Connection(runtime, session, Socket())
        connection.accepted = True
        session.connection = connection

        async def emit(*args: object) -> None:
            raise ServiceError("SOCKET_LOST", "lost", 409)

        connection.emit = emit
        first = runtime.submit(JobSpec.parse({"kind": "text", "text": "first", "session_id": sid}))
        second = runtime.submit(JobSpec.parse({"kind": "text", "text": "second"}))
        expired = runtime.submit(JobSpec.parse({"kind": "text", "text": "expired"}))
        expired.deadline = time.monotonic() - 1
        runtime.infer = lambda job: {"text": "ok", "calls": []}
        runtime.workers = [asyncio.create_task(runtime.lane("text"))]
        runtime.housekeeping = asyncio.create_task(runtime.reaper())
        try:
            await asyncio.wait_for(runtime.queues["text"].join(), 0.5)
            assert first.state == "cancelled" and not first.host_pending
            assert second.state == "succeeded"
            assert expired.state == "failed"
            assert session.connection is None
            await asyncio.wait_for(close_started.wait(), 0.1)
            assert runtime.socket_cleanup
            # Reaper still reaches unrelated session expiry while close stalls.
            sid2 = str(uuid4())
            runtime.sessions[sid2] = _Session(sid2, [], disconnected_at=time.monotonic() - 61)
            async def reaped() -> None:
                while sid2 in runtime.sessions:
                    await asyncio.sleep(0)
            await asyncio.wait_for(reaped(), 0.5)
            await asyncio.wait_for(close_cancelled.wait(), 1.5)
            assert not runtime.workers[0].done()
        finally:
            await finish(runtime)

    asyncio.run(run())


def test_partial_startup_rolls_back_once_and_sanitizes_error() -> None:
    async def run() -> None:
        runtime = make_runtime()
        runtime.state = "starting"
        runtime.loop = asyncio.get_running_loop()
        events = []
        runtime.store_lock = SimpleNamespace(release=lambda: events.append("release"))

        def load() -> dict:
            events.append("load-text")
            runtime.facade = SimpleNamespace(stop=lambda: events.append("stop"))
            raise RuntimeError("secret path/native traceback")

        runtime._load_profile = load
        try:
            await runtime.load_profile()
            assert runtime.state == "failed"
            assert runtime.startup_error["code"] == "STARTUP_FAILED"
            assert "secret" not in str(runtime.startup_error)
            assert events == ["load-text", "stop", "release"]
            await runtime.stop()
            await runtime.stop()
            assert events == ["load-text", "stop", "release"]
        finally:
            await finish(runtime)

    asyncio.run(run())


def test_cancelled_startup_never_unloads_pending_native_work() -> None:
    async def run() -> None:
        runtime = make_runtime()
        runtime.state = "starting"
        runtime.loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        events = []
        runtime.store_lock = SimpleNamespace(release=lambda: events.append("release"))

        def load() -> dict:
            runtime.facade = SimpleNamespace(stop=lambda: events.append("stop"))
            runtime.loop.call_soon_threadsafe(entered.set)
            release.wait(3)
            events.append("native-return")
            return {"probe": None, "planned": {}, "actual": {}}

        runtime._load_profile = load
        task = asyncio.create_task(runtime.load_profile())
        runtime.startup_task = task
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert events == [] and runtime.native_pending
            release.set()
            await asyncio.wait_for(runtime.startup_task, 1)
            assert events == ["native-return", "stop", "release"]
        finally:
            release.set()
            await finish(runtime)

    asyncio.run(run())


def test_failed_unload_keeps_store_lock_and_does_not_retry() -> None:
    async def run() -> None:
        runtime = make_runtime()
        runtime.state = "starting"
        runtime.loop = asyncio.get_running_loop()
        events = []
        runtime.store_lock = SimpleNamespace(release=lambda: events.append("release"))

        def stop() -> None:
            events.append("stop")
            raise RuntimeError("private unload failure")

        def load() -> dict:
            runtime.facade = SimpleNamespace(stop=stop)
            raise RuntimeError("private load failure")

        runtime._load_profile = load
        try:
            await runtime.load_profile()
            assert runtime.state == "failed" and events == ["stop"]
            await runtime.stop()
            await runtime.stop()
            assert runtime.state == "draining" and events == ["stop"]
        finally:
            await finish(runtime)

    asyncio.run(run())


@pytest.mark.parametrize("cancel_worker_twice", [False, True])
def test_cancelled_native_job_reserves_capacity_until_return(cancel_worker_twice: bool) -> None:
    async def run() -> None:
        runtime = make_runtime()
        runtime.loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        events = []
        runtime.facade = SimpleNamespace(stop=lambda: events.append("stop"))
        runtime.store_lock = SimpleNamespace(release=lambda: events.append("release"))

        def infer(job: object) -> dict:
            runtime.loop.call_soon_threadsafe(entered.set)
            release.wait(3)
            events.append("native-return")
            return {"text": "late", "calls": []}

        runtime.infer = infer
        job = runtime.submit(JobSpec.parse({"kind": "text", "text": "test"}))
        worker = asyncio.create_task(runtime.lane("text"))
        runtime.workers = [worker]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            runtime.cancel_job(job)
            assert job.state == "cancelled" and job.host_pending
            job.completed = time.monotonic() - TTL_SECONDS - 1
            runtime.prune()
            assert job.id in runtime.jobs
            if cancel_worker_twice:
                worker.cancel()
                await asyncio.sleep(0)
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                assert job.host_pending and runtime.native_pending
            shutdown = asyncio.create_task(runtime.stop())
            await asyncio.sleep(0)
            assert events == []
            release.set()
            await asyncio.wait_for(shutdown, 1)
            assert events == ["native-return", "stop", "release"]
            assert not job.host_pending and job.result is None
            assert runtime.state == "stopped"
        finally:
            release.set()
            await finish(runtime)

    asyncio.run(run())


def test_multibyte_stt_is_failed_before_success() -> None:
    async def run() -> None:
        runtime = make_runtime("voice")
        runtime.loop = asyncio.get_running_loop()
        artifact = runtime.publish(b"\0\0", 16000)
        job = runtime.submit(JobSpec.parse({"kind": "stt", "input_artifact_id": artifact["artifact_id"]}))
        runtime.infer = lambda job: {"text": "🙂" * 16_384}
        runtime.workers = [asyncio.create_task(runtime.lane("stt"))]
        try:
            await asyncio.wait_for(runtime.queues["stt"].join(), 1)
            assert job.state == "failed" and job.result is None
            assert job.error["code"] == "OUTPUT_LIMIT"
            assert not job.host_pending and job.input_pcm is None
            runtime.job_snapshot(job)
        finally:
            await finish(runtime)

    asyncio.run(run())


def test_idempotency_quota_and_expiry_accounting() -> None:
    runtime = make_runtime("voice")
    try:
        spec = JobSpec.parse({"kind": "text", "text": "test", "idempotency_key": "one"})
        first = runtime.submit(spec)
        assert runtime.submit(spec) is first
        with pytest.raises(ServiceError) as caught:
            runtime.submit(JobSpec.parse({"kind": "text", "text": "changed", "idempotency_key": "one"}))
        assert caught.value.code == "IDEMPOTENCY_CONFLICT"
        for _ in range(15):
            runtime.submit(JobSpec.parse({"kind": "text", "text": "test"}))
        artifact = runtime.publish(b"\0\0", 16000)
        with pytest.raises(ServiceError) as caught:
            runtime.submit(JobSpec.parse({"kind": "stt", "input_artifact_id": artifact["artifact_id"]}))
        assert caught.value.code == "RESOURCE_BUSY"
        # Definite admission rejection does not consume the uploaded artifact.
        assert artifact["artifact_id"] in runtime.artifacts
        with pytest.raises(ServiceError) as caught:
            runtime.submit(JobSpec.parse({"kind": "text", "text": "overflow"}))
        assert caught.value.code == "RESOURCE_BUSY"
        for job in list(runtime.jobs.values()):
            runtime.cancel_job(job)
            job.completed = time.monotonic() - TTL_SECONDS - 1
        assert runtime.queues["text"].empty()
        runtime.artifacts[artifact["artifact_id"]].created -= TTL_SECONDS + 1
        runtime.prune()
        assert not runtime.jobs and not runtime.idempotency
        assert not runtime.artifacts and runtime.artifact_bytes == 0
        assert runtime.submit(spec).id != first.id
    finally:
        for executor in runtime.executors.values():
            executor.shutdown(wait=True)


def test_retained_job_quota_and_cancel_queue_accounting() -> None:
    runtime = make_runtime()
    try:
        for _ in range(256):
            job = runtime.submit(JobSpec.parse({"kind": "text", "text": "test"}))
            runtime.cancel_job(job)
        assert runtime.queues["text"].empty()
        assert runtime.queues["text"]._unfinished_tasks == 0
        with pytest.raises(ServiceError) as caught:
            runtime.submit(JobSpec.parse({"kind": "text", "text": "test"}))
        assert caught.value.code == "RESOURCE_BUSY"
        for job in runtime.jobs.values():
            job.completed = time.monotonic() - TTL_SECONDS - 1
        runtime.prune()
        assert not runtime.jobs
        runtime.submit(JobSpec.parse({"kind": "text", "text": "test"}))
    finally:
        for executor in runtime.executors.values():
            executor.shutdown(wait=True)