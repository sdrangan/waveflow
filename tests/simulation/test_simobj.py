import pytest

from waveflow.simulation.simobj import ActionOverlap, ActionRecord, SimObj
from waveflow.simulation.simulation import Simulation


def test_init_defaults_and_custom_name() -> None:
    """Verify constructor defaults and explicit naming/overlap-tracking overrides."""
    sim = Simulation()
    obj = SimObj(sim=sim)
    named = SimObj(sim=sim, name="worker", track_action_overlaps=False)

    assert obj.sim is sim
    assert obj.env is sim.env
    assert obj.type_name == "sim_obj"
    assert obj.name.startswith("sim_obj")
    assert obj.track_action_overlaps is True
    assert obj.processes == []
    assert obj.action_history == []
    assert obj.action_overlaps == []

    assert named.name == "worker"
    assert named.track_action_overlaps is False


def test_now_and_timeout_progress_time() -> None:
    """Ensure timeout advances simulation time and now reflects the progressed clock."""
    sim = Simulation()
    obj = SimObj(sim=sim)

    assert obj.now == 0.0
    evt = obj.timeout(3.5)
    assert evt.env is sim.env
    sim.env.run(until=evt)
    assert obj.now == 3.5


def test_timeout_rejects_negative_delay() -> None:
    """Reject negative timeout delays with a clear ValueError."""
    sim = Simulation()
    obj = SimObj(sim=sim)

    with pytest.raises(ValueError, match="delay must be non-negative"):
        obj.timeout(-1)


def test_event_creates_plain_event_in_same_environment() -> None:
    """Create a plain event bound to the same environment and support succeed/value flow."""
    sim = Simulation()
    obj = SimObj(sim=sim)
    evt = obj.event()

    assert evt.env is sim.env
    assert not evt.triggered
    evt.succeed("ok")
    sim.env.run(until=evt)
    assert evt.value == "ok"


def test_process_registers_and_completes() -> None:
    """Register a process in SimObj bookkeeping and preserve its return value on completion."""
    sim = Simulation()
    obj = SimObj(sim=sim)

    def worker():
        yield obj.timeout(2)
        return "done"

    proc = obj.process(worker())
    sim.env.run(until=proc)

    assert obj.processes == [proc]
    assert proc.value == "done"


def test_add_process_and_start_registered_processes() -> None:
    """Validate process factory registration semantics for autostart and deferred startup."""
    sim = Simulation()
    obj = SimObj(sim=sim)
    calls: list[str] = []

    def p1():
        calls.append("p1-start")
        yield obj.timeout(1)

    def p2():
        calls.append("p2-start")
        yield obj.timeout(1)

    obj.add_process("p1", p1, autostart=False)
    obj.add_process("p2", p2, autostart=True)
    assert len(obj.processes) == 1
    obj.start_registered_processes()
    sim.env.run(until=3)

    assert len(obj.processes) == 3
    assert calls.count("p1-start") == 1
    assert calls.count("p2-start") == 2


def test_add_process_requires_non_empty_name() -> None:
    """Require non-empty process names when registering process factories."""
    sim = Simulation()
    obj = SimObj(sim=sim)

    with pytest.raises(ValueError, match="name must be non-empty"):
        obj.add_process("", lambda: iter(()))


def test_resource_and_transaction_queue_helpers() -> None:
    """Construct queue/resource helpers and enforce positive resource capacity."""
    sim = Simulation()
    obj = SimObj(sim=sim)

    q = obj.transaction_queue(capacity=2)
    assert q.capacity == 2

    res = obj.resource(capacity=3)
    assert res.capacity == 3

    with pytest.raises(ValueError, match="capacity must be positive"):
        obj.resource(0)


def test_container_helper_creates_container_with_capacity_and_init() -> None:
    """Construct a container helper with the requested capacity and initial level."""
    sim = Simulation()
    obj = SimObj(sim=sim)

    c = obj.container(capacity=10, init=4)

    assert c.capacity == 10
    assert c.level == 4


def test_call_after_runs_later_and_does_not_block_the_caller() -> None:
    """The deferred work starts after the delay, its own duration counts, and the caller runs on."""
    sim = Simulation()
    obj = SimObj(sim=sim)
    log = []

    def work(tag, dur):
        log.append((tag, "start", obj.now))
        yield obj.timeout(dur)
        log.append((tag, "end", obj.now))

    def caller():
        obj.call_after(5.0, work, "a", 2.0)
        log.append(("caller", "returned", obj.now))
        yield obj.timeout(1.0)
        obj.call_after(4.0, work, "b", 2.0)

    sim.env.process(caller())
    sim.env.run()
    assert log == [("caller", "returned", 0.0),
                   ("a", "start", 5.0), ("b", "start", 5.0),   # equal fire times: call order
                   ("a", "end", 7.0), ("b", "end", 7.0)]
    assert obj.processes == [], "deferred calls must not accumulate in SimObj.processes"


def test_call_after_slot_bounds_work_in_flight() -> None:
    """With a slot, the caller blocks once the slots are all held, and each finishing call frees
    one -- so at most `capacity` deferred calls are outstanding."""
    sim = Simulation()
    obj = SimObj(sim=sim)
    slots = obj.container(capacity=2, init=2)
    issued = []

    def work():
        yield obj.timeout(1.0)

    def caller():
        for _ in range(4):
            yield slots.get(1)
            issued.append(obj.now)
            obj.call_after(10.0, work, slot=slots)

    sim.env.process(caller())
    sim.env.run()
    assert issued == [0.0, 0.0, 11.0, 11.0]
    assert slots.level == 2


def test_call_after_rejects_negative_delay() -> None:
    obj = SimObj(sim=Simulation())
    with pytest.raises(ValueError, match="delay must be non-negative"):
        obj.call_after(-1.0, lambda: iter(()))


def test_action_records_duration_and_validation() -> None:
    """Record action timing windows and validate action input arguments."""
    sim = Simulation()
    obj = SimObj(sim=sim)

    def runner():
        record0 = yield from obj.action("instant")
        record1 = yield from obj.action("decode", processing_delay=2.0)
        return record0, record1

    proc = obj.process(runner())
    sim.env.run(until=proc)
    rec0, rec1 = proc.value

    assert isinstance(rec0, ActionRecord)
    assert rec0.name == "instant"
    assert rec0.start == rec0.end == 0.0
    assert rec1.name == "decode"
    assert rec1.start == 0.0
    assert rec1.end == 2.0
    assert len(obj.action_history) == 2

    with pytest.raises(ValueError, match="name must be non-empty"):
        sim2 = Simulation()
        obj2 = SimObj(sim=sim2)
        sim2.env.process(obj2.action(""))
        sim2.env.run()

    with pytest.raises(ValueError, match="processing_delay must be non-negative"):
        sim3 = Simulation()
        obj3 = SimObj(sim=sim3)
        sim3.env.process(obj3.action("bad", processing_delay=-1))
        sim3.env.run()


def test_action_overlap_tracking_and_clear_logs() -> None:
    """Detect overlapping actions when enabled and verify clear_action_logs resets records."""
    sim = Simulation()
    obj = SimObj(sim=sim)

    def first():
        yield from obj.action("first", processing_delay=5)

    def second():
        yield obj.timeout(2)
        yield from obj.action("second", processing_delay=3)

    obj.process(first())
    obj.process(second())
    sim.env.run(until=10)

    assert len(obj.action_history) == 2
    assert obj.active_overlap_count() == 1
    overlap = obj.action_overlaps[0]
    assert isinstance(overlap, ActionOverlap)
    assert {overlap.previous.name, overlap.current.name} == {"first", "second"}

    obj.clear_action_logs()
    assert obj.action_history == []
    assert obj.action_overlaps == []


def test_action_overlap_tracking_can_be_disabled() -> None:
    """Do not collect overlap records when overlap tracking is explicitly disabled."""
    sim = Simulation()
    obj = SimObj(sim=sim, track_action_overlaps=False)

    def first():
        yield from obj.action("first", processing_delay=5)

    def second():
        yield obj.timeout(2)
        yield from obj.action("second", processing_delay=3)

    obj.process(first())
    obj.process(second())
    sim.env.run(until=10)

    assert len(obj.action_history) == 2
    assert obj.active_overlap_count() == 0
