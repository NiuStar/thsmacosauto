"""Bounded memory-only Engine spans; reviewed schema and external Writer ownership.

No filesystem, worker creation, ticket waits, retry, raw error text or business IDs.
The source anchor is sealed once, after evidence is known, never by changing Trace.
"""
from contextlib import contextmanager
import threading
import time
import uuid

from .observability import CODES, MAX_STEPS, STAGES, TelemetryError, validate


def failure_code(error):
    code = getattr(error, 'code', None)
    if code in ('STALE_CAPTURE', 'STALE_FORM', 'STALE_SIGNAL'):
        return 'STALE_CAPTURE'
    if code in ('REAL_IDENTITY_UNPROVEN', 'FORM_CONTROL_SCOPE_UNPROVEN'):
        return 'IDENTITY_REJECTED'
    if code in ('JOURNAL_PERSISTENCE_FAILED', 'CONTROL_PERSISTENCE_FAILED'):
        return 'PERSISTENCE_FAILED'
    return 'FAILED'


class DeferredTrace:
    """One local operation; at most 32 serial nonoverlapping leaf spans."""
    def __init__(self, entry_ms):
        self.request_id = uuid.uuid4().hex
        self._start = time.monotonic_ns()
        self._utc = time.time_ns()
        self._earliest = entry_ms
        self._steps = []
        self._open = False
        self._finished = False
        self._primary = None

    def observe_source(self, value):
        # Invalid evidence still goes through the original business validation.
        if type(value) is dict:
            start = value.get('captured_start_ms')
            if type(start) is int and 0 <= start <= 2**63-1:
                self._earliest = min(self._earliest, start)

    @contextmanager
    def span(self, stage):
        if self._finished or self._open or stage not in STAGES or len(self._steps) >= MAX_STEPS:
            raise TelemetryError('INVALID_STAGE')
        self._open = True
        start = time.monotonic_ns()
        code = 'OK'
        try:
            yield
        except BaseException as exc:
            code = ('RESTORE_FAILED' if stage == 'RESTORE' else
                    'PERSISTENCE_FAILED' if stage in ('PREPARED','SETTLE') else failure_code(exc))
            self.note(code)
            raise
        finally:
            end = time.monotonic_ns()
            self._steps.append(dict(stage=stage, event_code=code,
                                    offset_ns=start-self._start, duration_ns=end-start))
            self._open = False

    def note(self, code):
        if code not in CODES:
            raise TelemetryError('INVALID_STAGE')
        if code != 'OK' and self._primary is None:
            self._primary = code

    def finish(self, result=None, error=None):
        if self._finished or self._open:
            raise TelemetryError('UNSETTLED_TRACE')
        total = time.monotonic_ns()-self._start
        utc = time.time_ns()
        status = result.get('status') if type(result) is dict else None
        outcome = 'FAILED' if error else 'UNKNOWN' if status == 'UNKNOWN' else 'BLOCKED' if status == 'BLOCKED' else 'OK'
        if error:
            self.note(failure_code(error))
        if outcome == 'OK' and self._primary:
            outcome = 'FAILED'
        code = self._primary or ('UNKNOWN' if outcome == 'UNKNOWN' else 'REJECTED' if outcome == 'BLOCKED' else 'OK')
        leaf = sum(step['duration_ns'] for step in self._steps)
        record = dict(version=1, request_id=self.request_id,
                      source_capture_start_ms=self._earliest, started_utc_ns=self._utc,
                      ended_utc_ns=utc, wall_clock_discontinuous=abs((utc-self._utc)-total)>1_000_000_000,
                      total_ns=total, leaf_ns=leaf, unattributed_ns=total-leaf,
                      outcome=outcome, event_code=code, steps=self._steps)
        raw = validate(record)
        self._finished = True
        return raw


class EngineTelemetry:
    """External owner must start READY, then shutdown only AFTER Engine.close().

    Retain at most three receipts (one terminal latest + writer's two live slots).
    Rejections latch degraded, never wait, retry or alter a completed order result.
    """
    def __init__(self, writer):
        if writer.start(0) != 'READY':
            raise ValueError('Telemetry writer must already be READY')
        self.writer = writer
        self._lock = threading.Lock()
        self._tickets = []
        self._degraded = False
        self._latest = None
        self._admission = None
        self._submitted = self._rejected = 0

    def _poll(self):
        live = []
        for ticket in self._tickets:
            status = ticket.query().status
            if status not in ('PENDING_UNKNOWN', 'FILE_AND_DIRECTORY_SYNCED'):
                self._degraded = True
            if status == 'PENDING_UNKNOWN':
                live.append(ticket)
        self._tickets = live

    def degraded(self):
        # A short in-memory critical section only; no writer/GUI/press/journal lock.
        with self._lock:
            self._poll()
            return self._degraded

    def submit(self, trace, result=None, error=None):
        try:
            raw = trace.finish(result, error)
            # Never retain our bookkeeping lock across even a nonwaiting writer
            # call. New admission, state reads and stop must remain independent.
            admission = self.writer.submit(raw)
            with self._lock:
                self._poll()
                self._submitted += 1
                self._admission = admission.status
                self._latest = admission.ticket
                if admission.status == 'ACCEPTED':
                    self._tickets.append(admission.ticket)
                else:
                    self._rejected += 1
                    self._degraded = True
            return admission.status
        except Exception:
            with self._lock:
                self._admission = 'SUBMISSION_FAILED'
                self._degraded = True
                self._rejected += 1
            return 'NOT_RECORDED'

    def state(self):
        # For caller-side status only, never within a GUI stage or press callback.
        snapshot = self.writer.snapshot()
        with self._lock:
            self._poll()
            if snapshot.fenced or not snapshot.accepting:
                self._degraded = True
            return dict(enabled=True, degraded=self._degraded, writer_fenced=snapshot.fenced,
                        writer_accepting=snapshot.accepting, pending=snapshot.pending,
                        in_flight=snapshot.in_flight, retained_tickets=len(self._tickets),
                        latest_admission=self._admission,
                        latest_status=self._latest.query().status if self._latest else None,
                        latest_request_id=self._latest.request_id if self._latest else None,
                        submitted=self._submitted, rejected=self._rejected)
