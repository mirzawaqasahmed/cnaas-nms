import os
import yaml
from typing import Optional
from ipaddress import IPv4Interface
from statistics import median
from hashlib import sha256

from nornir.plugins.tasks import networking, text
from nornir.plugins.functions.text import print_result
from nornir.core.filter import F

import cnaas_nms.db.helper
import cnaas_nms.confpush.nornir_helper
from cnaas_nms.db.session import sqla_session, redis_session
from cnaas_nms.confpush.get import get_uplinks, get_running_config_hash
from cnaas_nms.confpush.changescore import calculate_score
from cnaas_nms.tools.log import get_logger
from cnaas_nms.db.settings import get_settings
from cnaas_nms.db.device import Device, DeviceState, DeviceType
from cnaas_nms.db.interface import Interface, InterfaceConfigType
from cnaas_nms.db.joblock import Joblock, JoblockError
from cnaas_nms.db.git import RepoStructureException
from cnaas_nms.confpush.nornir_helper import NornirJobResult
from cnaas_nms.scheduler.wrapper import job_wrapper

from cnaas_nms.scheduler.jobtracker import Jobtracker
from cnaas_nms.scheduler.scheduler import Scheduler
from nornir.plugins.tasks.networking import napalm_get


logger = get_logger()
AUTOPUSH_MAX_SCORE = 10


def push_sync_device(task, dry_run: bool = True, generate_only: bool = False,
                     job_id: Optional[str] = None):
    """
    Nornir task to generate config and push to device

    Args:
        task: nornir task, sent by nornir when doing .run()
        dry_run: Don't commit config to device, just do compare/diff
        generate_only: Only generate text config, don't try to commit or
                       even do dry_run compare to running config

    Returns:

    """
    hostname = task.host.name
    with sqla_session() as session:
        dev: Device = session.query(Device).filter(Device.hostname == hostname).one()
        mgmt_ip = dev.management_ip
        if not mgmt_ip:
            raise Exception("Could not find management IP for device {}".format(hostname))
        devtype: DeviceType = dev.device_type
        if isinstance(dev.platform, str):
            platform: str = dev.platform
        else:
            raise ValueError("Unknown platform: {}".format(dev.platform))
        settings, settings_origin = get_settings(hostname, devtype)

        if devtype == DeviceType.ACCESS:
            neighbor_hostnames = dev.get_uplink_peers(session)
            if not neighbor_hostnames:
                raise Exception("Could not find any uplink neighbors for device {}".format(
                    hostname))
            mgmtdomain = cnaas_nms.db.helper.find_mgmtdomain(session, neighbor_hostnames)
            if not mgmtdomain:
                raise Exception(
                    "Could not find appropriate management domain for uplink peer devices: {}".
                    format(neighbor_hostnames))

            mgmt_gw_ipif = IPv4Interface(mgmtdomain.ipv4_gw)
            access_device_variables = {
                'mgmt_vlan_id': mgmtdomain.vlan,
                'mgmt_gw': mgmt_gw_ipif.ip,
                'mgmt_ipif': str(IPv4Interface('{}/{}'.format(mgmt_ip, mgmt_gw_ipif.network.prefixlen))),
                'mgmt_prefixlen': int(mgmt_gw_ipif.network.prefixlen)
            }
        elif devtype == DeviceType.DIST:
            dist_device_variables = {
                'mgmt_ipif': str(IPv4Interface('{}/32'.format(mgmt_ip))),
                'mgmt_prefixlen': 32,
                'interfaces': [],
                'mgmtdomains': []
            }
            if 'interfaces' in settings and settings['interfaces']:
                for intf in settings['interfaces']:
                    ifindexnum = 0
                    try:
                        ifindexnum = Interface.interface_index_num(intf['name'])
                    except ValueError as e:
                        pass
                    if 'ifclass' in intf and intf['ifclass'] == 'downlink':
                        dist_device_variables['interfaces'].append({
                            'name': intf['name'],
                            'ifclass': intf['ifclass'],
                            'indexnum': ifindexnum
                        })
                    elif 'ifclass' in intf and intf['ifclass'] == 'custom':
                        dist_device_variables['interfaces'].append({
                            'name': intf['name'],
                            'ifclass': intf['ifclass'],
                            'config': intf['config'],
                            'indexnum': ifindexnum
                        })
            for mgmtdom in cnaas_nms.db.helper.get_all_mgmtdomains(session, hostname):
                dist_device_variables['mgmtdomains'].append({
                    'ipv4_gw': mgmtdom.ipv4_gw,
                    'vlan': mgmtdom.vlan,
                    'description': mgmtdom.description,
                    'esi_mac': mgmtdom.esi_mac
                })

        intfs = session.query(Interface).filter(Interface.device == dev).all()
        uplinks = []
        access_auto = []
        intf: Interface
        for intf in intfs:
            if intf.configtype == InterfaceConfigType.ACCESS_AUTO:
                access_auto.append({'ifname': intf.name})
            elif intf.configtype == InterfaceConfigType.ACCESS_UPLINK:
                uplinks.append({'ifname': intf.name})
        device_variables = {
            'mgmt_ip': str(mgmt_ip),
            'uplinks': uplinks,
            'access_auto': access_auto
        }
        if 'access_device_variables' in locals() and access_device_variables:
            device_variables = {**access_device_variables, **device_variables}
        if 'dist_device_variables' in locals() and dist_device_variables:
            device_variables = {**dist_device_variables, **device_variables}

    # Merge device variables with settings before sending to template rendering
    # Device variables override any names from settings, for example the
    # interfaces list from settings are replaced with an interface list from
    # device variables that contains more information
    template_vars = {**settings, **device_variables}

    with open('/etc/cnaas-nms/repository.yml', 'r') as db_file:
        repo_config = yaml.safe_load(db_file)
        local_repo_path = repo_config['templates_local']

    mapfile = os.path.join(local_repo_path, platform, 'mapping.yml')
    if not os.path.isfile(mapfile):
        raise RepoStructureException("File {} not found in template repo".format(mapfile))
    with open(mapfile, 'r') as f:
        mapping = yaml.safe_load(f)
        template = mapping[devtype.name]['entrypoint']

    logger.debug("Generate config for host: {}".format(task.host.name))
    r = task.run(task=text.template_file,
                 name="Generate device config",
                 template=template,
                 path=f"{local_repo_path}/{task.host.platform}",
                 **template_vars)

    # TODO: Handle template not found, variables not defined
    # jinja2.exceptions.UndefinedError

    task.host["config"] = r.result
    task.host["template_vars"] = template_vars

    if generate_only:
        task.host["change_score"] = 0
    else:
        logger.debug("Synchronize device config for host: {}".format(task.host.name))

        task.host.open_connection("napalm", configuration=task.nornir.config)
        task.run(task=networking.napalm_configure,
                 name="Sync device config",
                 replace=True,
                 configuration=task.host["config"],
                 dry_run=dry_run
                 )
        task.host.close_connection("napalm")

        if task.results[1].diff:
            config = task.results[1].host["config"]
            diff = task.results[1].diff
            task.host["change_score"] = calculate_score(config, diff)
        else:
            task.host["change_score"] = 0
    if job_id:
        with redis_session() as db:
            db.lpush('finished_devices_' + str(job_id), task.host.name)


def generate_only(hostname: str) -> (str, dict):
    """
    Generate configuration for a device and return it as a text string.

    Args:
        hostname: Hostname of device generate config for

    Returns:
        (string with config, dict with available template variables)
    """
    nr = cnaas_nms.confpush.nornir_helper.cnaas_init()
    nr_filtered = nr.filter(name=hostname).filter(managed=True)
    if len(nr_filtered.inventory.hosts) != 1:
        raise ValueError("Invalid hostname: {}".format(hostname))
    try:
        nrresult = nr_filtered.run(task=push_sync_device, generate_only=True)
        if nrresult.failed:
            print_result(nrresult)
            raise Exception("Failed to generate config for {}".format(hostname))
        return nrresult[hostname][1].result, nrresult[hostname][1].host["template_vars"]
    except Exception as e:
        logger.exception("Exception while generating config: {}".format(str(e)))
        return nrresult[hostname][1].result, nrresult[hostname][1].host["template_vars"]


def sync_check_hash(task, force=False, dry_run=True):
    """
    Start the task which will compare device configuration hashes.

    Args:
        task: Nornir task
        force: Ignore device hash
    """
    if force is True:
        return
    stored_hash = Device.get_config_hash(task.host.name)
    if stored_hash is None:
        return
    res = task.run(task=napalm_get, getters=["config"])
    running_config = dict(res.result)['config']['running'].encode()
    if running_config is None:
        raise Exception('Failed to get running configuration')
    hash_obj = sha256(running_config)
    running_hash = hash_obj.hexdigest()
    if stored_hash != running_hash:
        raise Exception('Device {} configuration is altered outside of CNaaS!'.format(task.host.name))


@job_wrapper
def sync_devices(hostname: Optional[str] = None, device_type: Optional[str] = None,
                 dry_run: bool = True, force: bool = False, auto_push = False,
                 job_id: Optional[str] = None) -> NornirJobResult:
    """Synchronize devices to their respective templates. If no arguments
    are specified then synchronize all devices that are currently out
    of sync.

    Args:
        hostname: Specify a single host by hostname to synchronize
        device_type: Specify a device type to synchronize
        dry_run: Don't commit generated config to device
        force: Commit config even if changes made outside CNaaS will get
               overwritten
        auto_push: Automatically do live-run after dry-run if change score is low
        job_id: job_id provided by scheduler when adding a new job

    Returns:
        NornirJobResult
    """
    nr = cnaas_nms.confpush.nornir_helper.cnaas_init()
    if hostname:
        nr_filtered = nr.filter(name=hostname).filter(managed=True)
    elif device_type:
        nr_filtered = nr.filter(F(groups__contains='T_'+device_type))  # device type
    else:
        nr_filtered = nr.filter(synchronized=False).filter(managed=True)  # all unsynchronized devices

    device_list = list(nr_filtered.inventory.hosts.keys())
    logger.info("Device(s) selected for synchronization: {}".format(
        device_list
    ))

    try:
        nrresult = nr_filtered.run(task=sync_check_hash,
                                   force=force,
                                   dry_run=dry_run)
        print_result(nrresult)
        if nrresult.failed:
            raise Exception
    except Exception as e:
        raise Exception('Configuration hash check failed for {}'.format(' '.join(nrresult.failed_hosts.keys())))

    if not dry_run:
        with sqla_session() as session:
            logger.info("Trying to acquire lock for devices to run syncto job: {}".format(job_id))
            if not Joblock.acquire_lock(session, name='devices', job_id=job_id):
                raise JoblockError("Unable to acquire lock for configuring devices")

    try:
        nrresult = nr_filtered.run(task=push_sync_device, dry_run=dry_run,
                                   job_id=job_id)
        print_result(nrresult)
    except Exception as e:
        logger.exception("Exception while synchronizing devices: {}".format(str(e)))
        try:
            if not dry_run:
                with sqla_session() as session:
                    logger.info("Releasing lock for devices from syncto job: {}".format(job_id))
                    Joblock.release_lock(session, job_id=job_id)
        except Exception:
            logger.error("Unable to release devices lock after syncto job")
        return NornirJobResult(nrresult=nrresult)

    failed_hosts = list(nrresult.failed_hosts.keys())
    for hostname in failed_hosts:
        logger.error("Synchronization of device '{}' failed".format(hostname))

    if nrresult.failed:
        logger.error("Not all devices were successfully synchronized")

    if not dry_run:
        # set new config hash
        for key in nrresult.keys():
            if key in failed_hosts:
                continue
            new_config_hash = get_running_config_hash(key)
            if new_config_hash is None:
                try:
                    if not dry_run:
                        with sqla_session() as session:
                            logger.info("Releasing lock for devices from syncto job: {}".format(job_id))
                            Joblock.release_lock(session, job_id=job_id)
                except Exception:
                    logger.error("Unable to release devices lock after syncto job")
                raise Exception('Failed to get configuration hash')
            Device.set_config_hash(key, new_config_hash)

    total_change_score = 1
    change_scores = []
    changed_hosts = []
    unchanged_hosts = []
    # calculate change impact score
    for host, results in nrresult.items():
        if results[2].diff:
            changed_hosts.append(host)
            if "change_score" in results[0].host:
                change_scores.append(results[0].host["change_score"])
                logger.debug("Change score for host {}: {}".format(
                    host, results[0].host["change_score"]))
        else:
            unchanged_hosts.append(host)
            change_scores.append(0)
            logger.debug("Empty diff for host {}, 0 change score".format(
                host))

    # set devices as synchronized if needed
    with sqla_session() as session:
        for hostname in changed_hosts:
            if not dry_run:
                dev: Device = session.query(Device).filter(Device.hostname == hostname).one()
                dev.synchronized = True
        for hostname in unchanged_hosts:
            dev: Device = session.query(Device).filter(Device.hostname == hostname).one()
            dev.synchronized = True
        if not dry_run:
            logger.info("Releasing lock for devices from syncto job: {}".format(job_id))
            Joblock.release_lock(session, job_id=job_id)

    if not change_scores or total_change_score >= 100 or failed_hosts:
        total_change_score = 100
    elif max(change_scores) > 1000:
        # If some device has a score higher than this, disregard any averages
        # and report max impact score
        total_change_score = 100
    else:
        # calculate median value and round up, use min value of 1 and max of 100
        total_change_score = max(min(int(median(change_scores) + 0.5), 100), 1)
    logger.info("Change impact score: {}".format(total_change_score))

    next_job_id = None
    if auto_push and len(device_list) == 1 and hostname and dry_run:
        if not changed_hosts:
            logger.info("None of the selected host has any changes (diff), skipping auto-push")
        elif total_change_score < AUTOPUSH_MAX_SCORE:
            scheduler = Scheduler()
            next_job_id = scheduler.add_onetime_job(
                'cnaas_nms.confpush.sync_devices:sync_devices',
                when=0,
                kwargs={'hostname': hostname, 'dry_run': False, 'force': force})
            logger.info(f"Auto-push scheduled live-run of commit as job id {next_job_id}")
        else:
            logger.info(
                f"Auto-push of config to device {hostname} failed because change score of "
                f"{total_change_score} is higher than auto-push limit {AUTOPUSH_MAX_SCORE}"
            )

    return NornirJobResult(nrresult=nrresult, next_job_id=next_job_id, change_score=total_change_score)
