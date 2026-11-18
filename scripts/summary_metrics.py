"""Recompute measured summary values. Missing observation is never zero."""
import hashlib
import json
import math
from pathlib import Path

FIELDS = ('tokens', 'cost', 'humanInterventions', 'waitingSeconds', 'reworkAttempts')


def observation(path):
    if path is None:
        return None
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or not isinstance(value.get('source'), str) or not value['source']:
        raise ValueError('observation source required')
    for field in FIELDS:
        number = value.get(field)
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or number < 0):
            raise ValueError('invalid observation')
    for field in ('tokens', 'humanInterventions', 'reworkAttempts'):
        if value.get(field) is not None and type(value[field]) is not int:
            raise ValueError('integer observation required')
    if value.get('cost') is not None and (not isinstance(value.get('currency'), str) or not value['currency']):
        raise ValueError('cost currency required')
    return {**value, 'artifact': str(Path(path).resolve())}


def aggregate(units, directory, started_at, ended_at):
    metrics = {}
    for field in FIELDS:
        known = [r['observations'][field] for r in units
                 if r.get('observations', {}).get(field) is not None]
        unknown = len(units) - len(known)
        currencies = {r['observations']['currency'] for r in units
                      if r.get('observations', {}).get('cost') is not None} if field == 'cost' else set()
        mixed = len(currencies) > 1
        metrics[field] = dict(total=sum(known) if known and not unknown and not mixed else None,
                             knownSubtotal=sum(known) if known and not mixed else None,
                             knownUnits=len(known), unknownUnits=unknown,
                             status='known' if known and not unknown and not mixed else 'unknown')
        if field == 'cost':
            metrics[field]['currency'] = next(iter(currencies)) if len(currencies) == 1 else None
            metrics[field]['knownByCurrency'] = {currency: sum(r['observations']['cost'] for r in units
                if r.get('observations', {}).get('cost') is not None and r['observations']['currency'] == currency)
                for currency in currencies}
    decisions = Path(directory) / 'decisions.jsonl'
    observed = None
    if decisions.exists():
        identities = set()
        from drain import epoch
        invalid = False
        for line in decisions.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                automatic_send = row.get('kind') == 'teams-reply' and row.get('option') == 'send'
                if row.get('by') in ('button', 'todo-app') and not automatic_send and started_at <= epoch(row.get('at')) <= ended_at:
                    identities.add(hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest())
            except (ValueError, TypeError, AttributeError):
                invalid = True
        observed = None if invalid else len(identities)
    metrics['humanInterventions']['buttonDecisions'] = observed
    metrics['humanInterventions']['buttonSource'] = str(decisions) if decisions.exists() else None
    # Execution receipts supply actual outcomes. Repeated (key, unit) alone is not proof of rework.
    metrics['workerOutcomes'] = {outcome: sum(r.get('worker', {}).get('outcome') == outcome for r in units)
                                for outcome in ('completed', 'failed', 'timeout', 'launch-failed')}
    metrics['workerOutcomes']['unknownUnits'] = sum(not r.get('worker') for r in units)
    return metrics
