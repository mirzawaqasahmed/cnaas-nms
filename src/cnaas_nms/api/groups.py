from typing import List, Optional

from flask_restplus import Resource, Namespace
from flask_jwt_extended import jwt_required

from cnaas_nms.db.device import Device
from cnaas_nms.api.generic import empty_result
from cnaas_nms.db.settings import get_groups
from cnaas_nms.db.session import sqla_session
from cnaas_nms.version import __api_version__


api = Namespace('groups', description='API for handling groups',
                prefix='/api/{}'.format(__api_version__))


def groups_populate(group_name: Optional[str] = None):
    tmpgroups: dict = {}
    with sqla_session() as session:
        devices: List[Device] = session.query(Device).all()
        for dev in devices:
            groups = get_groups(dev.hostname)
            if not groups:
                continue
            for group in groups:
                if group_name and group != group_name:
                    continue
                if group not in tmpgroups:
                    tmpgroups[group] = []
                tmpgroups[group].append(dev.hostname)
    return tmpgroups


class GroupsApi(Resource):
    @jwt_required
    def get(self):
        """ Get all groups """
        tmpgroups = groups_populate()
        result = {'groups': tmpgroups}
        return empty_result(status='success', data=result)


class GroupsApiById(Resource):
    @jwt_required
    def get(self, group_name):
        """ Get a single group by ID """
        tmpgroups = groups_populate(group_name)
        result = {'groups': tmpgroups}
        return empty_result(status='success', data=result)


api.add_resource(GroupsApi, '')
api.add_resource(GroupsApiById, '/<string:group_name>')
