from nornir import InitNornir

from nornir.core.task import AggregatedResult, MultiResult, Result
from cnaas_nms.scheduler.jobresult import JobResult

from dataclasses import dataclass
from typing import Optional


@dataclass
class NornirJobResult(JobResult):
    nrresult: Optional[MultiResult] = None
    change_score: Optional[float] = None


def cnaas_init():
    nr = InitNornir(
        core={"num_workers": 50},
        inventory={
            "plugin": "cnaas_nms.confpush.nornir_plugins.cnaas_inventory.CnaasInventory"
        },
        logging={"file": "/tmp/nornir.log", "level": "debug"}
    )
    return nr


def nr_result_serialize(result: AggregatedResult):
    if not isinstance(result, AggregatedResult):
        raise ValueError("result must be of type AggregatedResult")

    hosts = {}    
    for host, multires in result.items():
        hosts[host] = {'failed': False, 'job_tasks': []}
        for res in multires:
            hosts[host]['job_tasks'].append({
                'task_name': res.name,
                'result': res.result,
                'diff': res.diff,
                'failed': res.failed
            })
            if res.failed:
                hosts[host]['failed'] = True
    return hosts
