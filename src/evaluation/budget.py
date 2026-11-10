import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

DEFAULT_LIMITS = dict(calls=300, input=600000, output=100000, dev_calls=60)


class BudgetStop(RuntimeError):
    pass


@contextmanager
def exclusive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BudgetStop('Another evaluation process owns the lock') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


class Budget:
    def allow_reserved_unknown(self, authorization, *, max_total=5):
        """Explicit opt-in: retain unknown reservations; never fabricate provider usage."""
        if not authorization.strip():
            raise ValueError('Explicit authorization is required')
        if type(max_total) is not int or not 1<=max_total<=20:
            raise ValueError('Unknown reservation cap must be between 1 and 20')
        with exclusive(self.lock):
            data = json.loads(self.path.read_text(encoding='utf-8'))
            entry = data['entries'][-1] if data['entries'] else None
            if data['pending'] and (not entry or entry['id'] != data['pending'] or entry['status'] != 'unknown_usage'):
                raise BudgetStop('Cannot release an in-flight reservation')
            if data['halted'] and (not entry or entry['status'] != 'unknown_usage'):
                raise BudgetStop('Cannot override an over-reservation halt')
            if data.get('unknown_streak',0)>=3:
                raise BudgetStop('Repeated unknown requests require investigation')
            if sum(e['status'] in ('unknown_usage','reserved_unknown_usage') for e in data['entries'])>max_total:
                raise BudgetStop('Too many unknown requests require reconciliation')
            data['unknown_policy'] = dict(authorization=authorization,max_total=max_total,max_consecutive=3)
            data.setdefault('policy_changes',[]).append(dict(authorization=authorization,
                action='retain_unknown_reservation_and_continue',entry_id=data['pending']))
            if data['pending']:
                entry['status']='reserved_unknown_usage'
                data['unknown_streak']=1
                data['pending']=None
            data['halted']=False
            atomic_json(self.path,data)

    def __init__(self, path, limits=None):
        self.path = Path(path)
        self.lock = self.path.with_suffix('.lock')
        with exclusive(self.lock):
            if not self.path.exists():
                atomic_json(self.path, dict(version=1, limits=limits or DEFAULT_LIMITS, calls=0,
                    dev_calls=0, input=0, output=0, pending=None, halted=False, entries=[]))

    def reserve(self, split, input_bound, output_bound):
        with exclusive(self.lock):
            data = json.loads(self.path.read_text(encoding='utf-8'))
            limits = data['limits']
            if data['halted'] or data['pending']:
                raise BudgetStop('Unreconciled request or missing usage; manual reconciliation required')
            if input_bound < 1 or output_bound < 1 or any((
                data['calls'] + 1 > limits['calls'],
                data['dev_calls'] + (split == 'dev') > limits['dev_calls'],
                data['input'] + input_bound > limits['input'], data['output'] + output_bound > limits['output'])):
                raise BudgetStop('Cumulative evaluation budget exhausted')
            identity = uuid4().hex
            data['calls'] += 1
            data['dev_calls'] += split == 'dev'
            data['input'] += input_bound
            data['output'] += output_bound
            data['pending'] = identity
            data['entries'].append(dict(id=identity, split=split, input_reserved=input_bound,
                output_reserved=output_bound, status='reserved', context=getattr(self, 'context', {})))
            atomic_json(self.path, data)
            return identity

    def settle(self, identity, usage):
        with exclusive(self.lock):
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data['pending'] != identity:
                raise BudgetStop('Reservation mismatch')
            entry = data['entries'][-1]
            valid = isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0
                for key in ('input_tokens', 'output_tokens'))
            if not valid:
                data['halted'], entry['status'] = True, 'unknown_usage'
                policy=data.get('unknown_policy')
                if policy:
                    data['unknown_streak']=data.get('unknown_streak',0)+1
                    total=sum(e['status'] in ('unknown_usage','reserved_unknown_usage') for e in data['entries'])
                    if total<=policy['max_total'] and data['unknown_streak']<policy['max_consecutive']:
                        entry['status']='reserved_unknown_usage'
                        data['pending']=None
                        data['halted']=False
            else:
                data['unknown_streak']=0
                for key in ('input', 'output'):
                    value = usage[key + '_tokens']
                    data[key] += value - entry[key + '_reserved']
                    if value > entry[key + '_reserved']:
                        data['halted'] = True
                entry.update(status='settled', usage=usage)
                data['pending'] = None
            atomic_json(self.path, data)
            if data['halted']:
                raise BudgetStop('Usage unavailable or exceeded reservation; no more requests allowed')
