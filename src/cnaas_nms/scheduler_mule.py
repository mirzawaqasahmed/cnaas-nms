import json
import datetime
import os
import coverage
import atexit
import signal

from cnaas_nms.scheduler.scheduler import Scheduler
from cnaas_nms.plugins.pluginmanager import PluginManagerHandler
from cnaas_nms.db.session import sqla_session
from cnaas_nms.db.joblock import Joblock
from cnaas_nms.tools.log import get_logger


logger = get_logger()
logger.info("Code coverage collection for mule in pid {}: {}".format(
    os.getpid(), ('COVERAGE' in os.environ)))

if 'COVERAGE' in os.environ:
    cov = coverage.coverage(data_file='/coverage/.coverage-{}'.format(os.getpid()))
    cov.start()


    def save_coverage():
        cov.stop()
        cov.save()


    atexit.register(save_coverage)
    signal.signal(signal.SIGTERM, save_coverage)
    signal.signal(signal.SIGINT, save_coverage)


def main_loop():
    try:
        import uwsgi
    except Exception as e:
        logger.exception("Mule not running in uwsgi, exiting: {}".format(str(e)))
        print("Error, not running in uwsgi")
        return

    print("Running scheduler in uwsgi mule")
    scheduler = Scheduler()
    scheduler.start()

    pmh = PluginManagerHandler()
    pmh.load_plugins()

    try:
        with sqla_session() as session:
            Joblock.clear_locks(session)
    except Exception as e:
        logger.exception("Unable to clear old locks from database at startup: {}".format(str(e)))

    while True:
        mule_data = uwsgi.mule_get_msg()
        data: dict = json.loads(mule_data)
        if data['when'] and isinstance(data['when'], int):
            data['run_date'] = datetime.datetime.utcnow() + datetime.timedelta(seconds=data['when'])
            del data['when']
        kwargs = {}
        for k, v in data.items():
            if k not in ['func', 'trigger', 'id', 'run_date']:
                kwargs[k] = v
        scheduler.add_job(data['func'], trigger=data['trigger'], kwargs=kwargs,
                          id=data['id'], run_date=data['run_date'])


if __name__ == '__main__':
    main_loop()

