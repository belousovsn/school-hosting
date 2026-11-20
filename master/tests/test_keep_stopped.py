"""Остановленный проект не запускается снова из-за сборки, закончившейся после остановки."""

import asyncio


class FakeDocker:
    def __init__(self, running):
        self.running = running

    async def container_for(self, uuid):
        return {"Id": "c1"} if self.running else None


class FakeCoolify:
    def __init__(self, dep_status):
        self.dep_status, self.stopped = dep_status, []

    async def latest_deployment(self, uuid):
        return {"status": self.dep_status}

    async def stop(self, uuid):
        self.stopped.append(uuid)


def run(monkeypatch, running, dep_status):
    from app import pipeline
    cf = FakeCoolify(dep_status)
    events = []
    monkeypatch.setattr(pipeline, "dk", FakeDocker(running))
    monkeypatch.setattr(pipeline, "cf", cf)
    monkeypatch.setattr(pipeline.db, "event", lambda pid, level, text: events.append(level))
    did = asyncio.run(pipeline.keep_stopped({"id": 6, "app_uuid": "app"}))
    return did, cf.stopped, events


def test_running_container_of_stopped_project_is_stopped(monkeypatch):
    assert run(monkeypatch, True, "finished") == (True, ["app"], ["warn"])


def test_waits_for_deploy_in_progress(monkeypatch):
    assert run(monkeypatch, True, "in_progress") == (False, [], [])


def test_nothing_to_do_when_container_is_down(monkeypatch):
    assert run(monkeypatch, False, "finished") == (False, [], [])
