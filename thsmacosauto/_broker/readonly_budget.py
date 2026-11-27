"""One cooperative monotonic budget, private to one owner execution."""
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic_ns, process_time_ns, thread_time_ns
import threading

class BudgetError(Exception):
    def __init__(self,code='READ_BUDGET_EXCEEDED'):
        self.code=code
        super().__init__(code)

# Fixed cardinality; inclusive spans overlap and MUST NOT be summed.
_COST_SECTIONS=('READ_TOTAL','LEASE_TOTAL','CANONICAL_LOCK','GRANT_OPEN_HELD','GRANT_NAMED','MARKER','GRANT_OWNER','RPC_TIMEOUT','AX_GET','APP_IDENTITY')
_MAX_COST=(1<<63)-1
def _bounded(value):return min(_MAX_COST,max(0,int(value)))
def _clock():return monotonic_ns(),process_time_ns(),thread_time_ns()

@contextmanager
def cost(section):
    # No budget checks, deadlines or exception authority in instrumentation.
    b=current();start=None
    if b is not None:
        try:
            index=_COST_SECTIONS.index(section)
            start=_clock()
        except BaseException:pass
    try:yield
    finally:
        if start is not None:
            try:b.accumulate(index,start,_clock())
            except BaseException:pass

_active=ContextVar('readonly_budget',default=None)
_effect=ContextVar('readonly_effect',default=None)
_proof=ContextVar('readonly_proof',default=None)
_passive=ContextVar('readonly_passive_rpc',default=False)
_stage=ContextVar('readonly_stage',default='ENGINE')
_STAGES=frozenset(('ENGINE','OWNER_PROOF','OWNER_LEASE','NATIVE_CALLBACK','AX_GET','AX_ACTIONS','AX_SETTABLE','AX_SET','AX_PERFORM','CONNECT','IDENTITY','PANEL_DETECT','SWITCH_TARGET','TABLE_READ','CONTINUITY','RESTORE'))

@contextmanager
def at(stage):
    # Callers supply fixed code identifiers, never selectors/account payloads.
    if stage not in _STAGES:raise ValueError('UNKNOWN_BUDGET_STAGE')
    token=_stage.set(stage)
    try:yield
    finally:_stage.reset(token)

def current():return _active.get()
def check():
    b=current()
    if b is not None:b.check()

def mark_rpc():
    check()
    e=_effect.get()
    if e is not None:e['actual_rpc_entered']=True
    b=current()
    if b is not None:b.set(dispatch_attempted=True)

@dataclass(frozen=True)
class ReadBudget:
    started_ns:int=field(default_factory=lambda:monotonic_ns())
    read_only:bool=False
    work_deadline_ns:int=field(init=False)
    restore_deadline_ns:int=field(init=False)
    absolute_deadline_ns:int=field(init=False)
    def __post_init__(self):
        deltas=(12_000_000_000,14_500_000_000,15_000_000_000) if self.read_only else (3_000_000_000,4_900_000_000,5_000_000_000)
        for key,delta in zip(('work_deadline_ns','restore_deadline_ns','absolute_deadline_ns'),deltas):object.__setattr__(self,key,self.started_ns+delta)
        object.__setattr__(self,'_cost',[[0,0,0,0] for _ in _COST_SECTIONS])
        object.__setattr__(self,'_state',dict(phase='WORK',owner=None,events=[],dropped_count=0,dispatch_attempted=False,restore_required=False,restored_verified=False,poisoned=False,terminal_late=False))
    def __getattr__(self,key):
        if key in self._state:return self._state[key]
        raise AttributeError(key)
    def set(self,**fields):self._state.update(fields)
    def check(self,phase=None):
        phase=phase or self.phase
        deadline={'WORK':self.work_deadline_ns,'RESTORE':self.restore_deadline_ns,'FINALIZE':self.absolute_deadline_ns}[phase]
        now=monotonic_ns()
        if now>=deadline:
            if self._state.get('first_exceeded') is None:
                self.set(first_exceeded={'phase':phase,'stage':_stage.get(),'elapsed_ns':max(0,now-self.started_ns)})
            raise BudgetError()
    def remaining_seconds(self,cap=None):
        self.check()
        deadline={'WORK':self.work_deadline_ns,'RESTORE':self.restore_deadline_ns,'FINALIZE':self.absolute_deadline_ns}[self.phase]
        remaining=(deadline-monotonic_ns())/1e9
        if remaining<=0:raise BudgetError()
        return remaining if cap is None else min(cap,remaining)
    def event(self,reason):
        if len(self.events)<16:self.events.append((self.phase,max(0,monotonic_ns()-self.started_ns),reason))
        else:self.set(dropped_count=self.dropped_count+1)
    def accumulate(self,index,start,end):
        row=self._cost[index]
        row[0]=_bounded(row[0]+1)
        for i in range(3):row[i+1]=_bounded(row[i+1]+_bounded(end[i]-start[i]))
    def cost_receipt(self):
        return {'schema':'READ_COST_V1','timing':'INCLUSIVE_OVERLAPPING_DO_NOT_SUM',
                'sections':{name:dict(zip(('count','wall_ns','process_cpu_ns','thread_cpu_ns'),row))
                            for name,row in zip(_COST_SECTIONS,self._cost)}}
    def receipt(self):
        record={**self._state,'events':tuple(self.events),'owner':None}
        try:record['cost']=self.cost_receipt()
        except BaseException:pass
        return record

@contextmanager
def bind(budget):
    if current() is not None or budget.owner is not None:raise BudgetError('READ_BUDGET_UNSUPPORTED')
    budget.set(owner=threading.get_ident())
    budget.event('OWNER_BOUND')
    token=_active.set(budget)
    try:yield budget
    finally:_active.reset(token)

def require(budget):
    if current() is not budget or budget.owner!=threading.get_ident():raise BudgetError('READ_BUDGET_UNSUPPORTED')
    budget.check()

@contextmanager
def effect(record,proof):
    a=_effect.set(record);b=_proof.set(proof)
    try:yield
    finally:_proof.reset(b);_effect.reset(a)
