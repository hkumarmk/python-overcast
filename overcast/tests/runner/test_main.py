#
#   Copyright 2015 Reliance Jio Infocomm, Ltd.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
from contextlib import nested
import mock
import os.path
import unittest
from StringIO import StringIO
import yaml

import overcast.runner

yaml_data = '''---
foo:
  - bar
  - baz:
      wibble: []
'''

mappings_data = '''[images]
trusty = 7cd9416f-9167-4371-a04a-a7939c5372ab

[networks]
common = b2b2f6a6-228f-4d42-b4f7-0d340b3390e7

[flavors]
small = 34fb3740-d158-472c-8520-017278c75008

[routers]
* = 61047deb-b0bf-4668-8325-d853d5d53c40
'''

class NodeTests(unittest.TestCase):
    def setUp(self):
        self.dr = overcast.runner.DeploymentRunner()
        self.node = overcast.runner.Node('name', {}, self.dr)

    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    def test_poll(self, get_nova_client):
        nc = get_nova_client.return_value
        self.node.server_id = 'someuuid'

        nc.servers.get.return_value.status = 'ACTIVE'
        self.node.poll()

        nc.servers.get.assert_called_with('someuuid')
        self.assertEquals(len(nc.servers.get.mock_calls), 1)

        self.node.poll()
        self.assertEquals(len(nc.servers.get.mock_calls), 1,
                          'server.get() was called even though expected '
                          'state already reached')

    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_cinder_client')
    @mock.patch('overcast.runner.Node.create_nics')
    @mock.patch('overcast.runner.time')
    def test_build(self, time, create_nics, get_cinder_client, get_neutron_client, get_nova_client):
        self.node.info['image'] = 'someimage'
        self.node.info['flavor'] = 'someflavor'
        self.node.info['disk'] = 10
        self.node.info['networks'] = mock.sentinel.Networks

        novaclient = get_nova_client.return_value
        novaclient.flavors.get.return_value = 'flavor_obj'
        neutronclient = get_neutron_client.return_value

        cinderclient = get_cinder_client.return_value
        class Volume(object):
            def __init__(self, uuid):
                self.id = uuid
                self.statuses = ['downloading', 'downloading', 'available']

            @property
            def status(self):
                return self.statuses.pop()

        cinderclient.volumes.get.return_value = Volume('voluuid')
        create_nics.return_value = ['portuuid1', 'portuuid2']

        self.node.build()

        create_nics.assert_called_with(mock.sentinel.Networks)
        novaclient.flavors.get.assert_called_with('someflavor')

        novaclient.servers.create.assert_called_with('name', userdata=None,
                                                     nics=[{'port-id': 'portuuid1'}, {'port-id': 'portuuid2'}],
                                                     image=None,
                                                     block_device_mapping={'vda': 'voluuid:::1'},
                                                     key_name=None, flavor='flavor_obj')

    def test_floating_ip(self):
        self.node.ports = [{'floating_ip': '1.2.3.4'}]
        self.assertEquals(self.node.floating_ip, '1.2.3.4')

    @mock.patch('overcast.runner.DeploymentRunner.delete_server')
    @mock.patch('overcast.runner.DeploymentRunner.delete_port')
    @mock.patch('overcast.runner.DeploymentRunner.delete_floatingip')
    def test_clean(self, delete_floatingip, delete_port, delete_server):
        self.node.fip_ids = set(['fipuuid1', 'fipuuid2'])
        self.node.ports = [{'id': 'portuuid1'}, {'id': 'portuuid2'}]
        self.node.server_id = 'serveruuid'

        self.node.clean()

        delete_floatingip.assert_any_call('fipuuid1')
        delete_floatingip.assert_any_call('fipuuid2')
        self.assertEquals(self.node.fip_ids, set())

        delete_port.assert_any_call('portuuid1')
        delete_port.assert_any_call('portuuid2')
        self.assertEquals(self.node.ports, [])

        delete_server.assert_any_call('serveruuid')
        self.assertEquals(self.node.server_id, None)

    @mock.patch('overcast.runner.DeploymentRunner.create_port')
    @mock.patch('overcast.runner.DeploymentRunner.create_floating_ip')
    @mock.patch('overcast.runner.DeploymentRunner.associate_floating_ip')
    def test_create_nics(self, associate_floating_ip, create_floating_ip, create_port):
        self.dr.secgroups['secgroup1'] = 'secgroupuuid1'
        self.dr.secgroups['secgroup2'] = 'secgroupuuid2'
        networks = [{'network': 'network1',
                     'securitygroups': ['secgroup1']},
                    {'network': 'network2',
                     'securitygroups': ['secgroup1', 'secgroup2'],
                     'assign_floating_ip': True}]

        create_port.side_effect = [{'id': 'port1uuid'},
                                   {'id': 'port2uuid'}]
        create_floating_ip.side_effect = [('fip1uuid', '1.2.3.4')]

        nics = self.node.create_nics(networks)

        create_port.assert_any_call('name_eth0', 'network1', ['secgroupuuid1'])
        create_port.assert_any_call('name_eth1', 'network2', ['secgroupuuid1', 'secgroupuuid2'])

        self.assertEquals(nics, ['port1uuid', 'port2uuid'])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.dr = overcast.runner.DeploymentRunner()

    def test_load_yaml(self):
        with mock.patch('__builtin__.open') as m:
            m.return_value.__enter__.return_value = StringIO(yaml_data)
            self.assertEquals(overcast.runner.load_yaml(),
                              {'foo': ['bar', {'baz': {'wibble': []}}]})
            m.assert_called_once_with('.overcast.yaml', 'r')

    def test_load_mappings(self):
        with mock.patch('__builtin__.open') as m:
            m.return_value.__enter__.return_value = StringIO(mappings_data)
            self.assertEquals(overcast.runner.load_mappings(),
                              {'flavors': {'small': '34fb3740-d158-472c-8520-017278c75008'},
                               'images': {'trusty': '7cd9416f-9167-4371-a04a-a7939c5372ab'},
                               'networks': {'common': 'b2b2f6a6-228f-4d42-b4f7-0d340b3390e7'},
                               'routers': {'*': '61047deb-b0bf-4668-8325-d853d5d53c40'}})
            m.assert_called_once_with('.overcast.mappings.ini', 'r')

    def test_find_weak_refs(self):
        example_file = os.path.join(os.path.dirname(__file__),
                                    'examplestack1.yaml')
        stack = overcast.runner.load_yaml(example_file)
        self.assertEquals(overcast.runner.find_weak_refs(stack),
                          (set(['trusty']),
                           set(['bootstrap']),
                           set(['default'])))

    def test_run_cmd_once_simple(self):
        overcast.runner.run_cmd_once(shell_cmd='bash',
                                     real_cmd='true',
                                     environment={},
                                     deadline=None)

    def test_run_cmd_once_fail(self):
        self.assertRaises(overcast.exceptions.CommandFailedException,
                          overcast.runner.run_cmd_once, shell_cmd='bash',
                                                        real_cmd='false',
                                                        environment={},
                                                        deadline=None)

    def test_run_cmd_once_with_deadline(self):
        deadline = 10
        with mock.patch('overcast.runner.time') as time_mock:
            time_mock.time.return_value = 9
            overcast.runner.run_cmd_once(shell_cmd='bash',
                                         real_cmd='true',
                                         environment={},
                                         deadline=deadline)
            time_mock.time.return_value = 11
            self.assertRaises(overcast.exceptions.CommandTimedOutException,
                              overcast.runner.run_cmd_once, shell_cmd='bash',
                                                            real_cmd='true',
                                                            environment={},
                                                            deadline=deadline)


    @mock.patch('overcast.runner.run_cmd_once')
    def test_shell_step(self, run_cmd_once):
        details = {'cmd': 'true'}
        self.dr.shell_step(details, {})
        run_cmd_once.assert_called_once_with(mock.ANY, 'true', mock.ANY, None)

    @mock.patch('overcast.runner.run_cmd_once')
    def test_shell_step_failure(self, run_cmd_once):
        details = {'cmd': 'false'}
        self.dr.shell_step(details, {})
        run_cmd_once.assert_called_once_with(mock.ANY, 'false', mock.ANY, None)

    @mock.patch('overcast.runner.run_cmd_once')
    def test_shell_step_retries_if_failed_until_success(self, run_cmd_once):
        details = {'cmd': 'true',
                   'retry-if-fails': True}

        side_effects = [overcast.exceptions.CommandFailedException()]*100 + [True]
        run_cmd_once.side_effect = side_effects
        self.dr.shell_step(details, {})
        self.assertEquals(list(run_cmd_once.side_effect), [])

    @mock.patch('overcast.runner.time')
    @mock.patch('overcast.runner.run_cmd_once')
    def test_shell_step_retries_if_failed_until_success_with_delay(self, run_cmd_once, time):
        details = {'cmd': 'true',
                   'retry-if-fails': True,
                   'retry-delay': '5s'}

        curtime = [0]
        def sleep(s, curtime=curtime):
            curtime[0] += s

        time.time.side_effect = lambda:curtime[0]
        time.sleep.side_effect = sleep

        side_effects = [overcast.exceptions.CommandFailedException()]*2 + [True]
        run_cmd_once.side_effect = side_effects
        self.dr.shell_step(details, {})
        self.assertEquals(list(run_cmd_once.side_effect), [])
        self.assertEquals(curtime[0], 10)

    @mock.patch('overcast.runner.run_cmd_once')
    def test_shell_step_retries_if_timedout_until_success(self, run_cmd_once):
        details = {'cmd': 'true',
                   'retry-if-fails': True,
                   'timeout': '10s'}

        side_effects = [overcast.exceptions.CommandTimedOutException()]*10 + [True]
        run_cmd_once.side_effect = side_effects
        self.dr.shell_step(details, {})
        self.assertEquals(list(run_cmd_once.side_effect), [])

    def test_build_env_prefix(self):
        class Node(object):
            def __init__(self, name, ports, export):
                self.name = name
                self.info = {'export': export}
                self.ports = ports

        self.dr.nodes = {'node1': Node('node1', [{'fixed_ip': '1.2.3.4',
                                                  'network_name': 'network1'},
                                                 {'fixed_ip': '2.3.4.5',
                                                  'network_name': 'network2'}],
                                       True),
                         'node2': Node('node2', [{'fixed_ip': '1.2.3.5',
                                                  'network_name': 'network1'},
                                                 {'fixed_ip': '2.3.4.5',
                                                  'network_name': 'network3'}],
                                       False)}

        env_prefix = self.dr.build_env_prefix({})

        self.assertIn('OVERCAST_node1_network1_fixed=1.2.3.4', env_prefix)
        self.assertIn('OVERCAST_node1_network2_fixed=2.3.4.5', env_prefix)
        self.assertNotIn('OVERCAST_node2', env_prefix)


    @mock.patch('overcast.runner.time')
    @mock.patch('overcast.runner.run_cmd_once')
    def test_shell_step_retries_if_timedout_until_total_timeout(self,
                                                                run_cmd_once,
                                                                time):
        details = {'cmd': 'true',
                   'retry-if-fails': True,
                   'total-timeout': '10s'}

        time.time.return_value = 10

        side_effects = [overcast.exceptions.CommandTimedOutException()]*2
        def side_effect(*args, **kwargs):
            if len(side_effects) < 2:
                time.time.return_value = 100

            ret = side_effects.pop(0)
            if isinstance(ret, Exception):
                raise ret
            return ret

        run_cmd_once.side_effect = side_effect
        self.assertRaises(overcast.exceptions.CommandTimedOutException,
                          self.dr.shell_step, details, {})
        self.assertEquals(side_effects, [])

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    def test_detect_existing_resources_network_conflict(self, get_nova_client, get_neutron_client):
        neutron = get_neutron_client.return_value
        nova = get_nova_client.return_value

        neutron.list_networks.return_value = {'networks': [{'name': 'somename',
                                                            'id': 'uuid1'},
                                                           {'name': 'somename',
                                                            'id': 'uuid2'}]}

        neutron.list_security_groups.return_value = {'security_groups': []}
        nova.servers.list.return_value = []

        self.assertRaises(overcast.exceptions.DuplicateResourceException,
                          self.dr.detect_existing_resources)

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    def test_detect_existing_resources_network_conflict_in_other_suffix(self, get_nova_client, get_neutron_client):
        neutron = get_neutron_client.return_value
        nova = get_nova_client.return_value

        neutron.list_networks.return_value = {'networks': [{'name': 'somename_foo',
                                                            'id': 'uuid1'},
                                                           {'name': 'somename_foo',
                                                            'id': 'uuid2'}]}

        neutron.list_security_groups.return_value = {'security_groups': []}
        nova.servers.list.return_value = []

        self.dr.suffix = 'bar'
        self.dr.detect_existing_resources()

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    def test_detect_existing_resources_secgroup_conflict(self, get_nova_client, get_neutron_client):
        neutron = get_neutron_client.return_value
        nova = get_nova_client.return_value

        neutron.list_networks.return_value = {'networks': []}
        neutron.list_security_groups.return_value = {'security_groups':
                                                          [{'name': 'somename',
                                                            'id': 'uuid1'},
                                                           {'name': 'somename',
                                                            'id': 'uuid2'}]}

        nova.servers.list.return_value = []

        self.assertRaises(overcast.exceptions.DuplicateResourceException,
                          self.dr.detect_existing_resources)

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    def test_detect_existing_resources_secgroup_conflict_in_other_suffix(self, get_nova_client, get_neutron_client):
        neutron = get_neutron_client.return_value
        nova = get_nova_client.return_value

        neutron.list_networks.return_value = {'networks': []}
        neutron.list_security_groups.return_value = {'security_groups':
                                                          [{'name': 'somename_foo',
                                                            'id': 'uuid1'},
                                                           {'name': 'somename_foo',
                                                            'id': 'uuid2'}]}
        nova.servers.list.return_value = []

        self.dr.suffix = 'bar'
        self.dr.detect_existing_resources()

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    def test_detect_existing_resources_server_conflict(self, get_nova_client, get_neutron_client):
        neutron = get_neutron_client.return_value
        nova = get_nova_client.return_value

        class Server(object):
            def __init__(self, name, id):
                self.name = name
                self.id = id
                self.addresses = {}

        neutron.list_networks.return_value = {'networks': []}
        neutron.list_security_groups.return_value = {'security_groups': []}
        nova.servers.list.return_value = [Server('server1', 'uuid1'),
                                          Server('server1', 'uuid2')]

        self.assertRaises(overcast.exceptions.DuplicateResourceException,
                          self.dr.detect_existing_resources)

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    def test_detect_existing_resources_server_conflict_in_other_suffix(self, get_nova_client, get_neutron_client):
        neutron = get_neutron_client.return_value
        nova = get_nova_client.return_value

        class Server(object):
            def __init__(self, name, id):
                self.name = name
                self.id = id

        neutron.list_networks.return_value = {'networks': []}
        neutron.list_security_groups.return_value = {'security_groups': []}
        nova.servers.list.return_value = [Server('server1_foo', 'uuid1'),
                                          Server('server1_foo', 'uuid2')]

        self.dr.suffix = 'bar'
        self.dr.detect_existing_resources()

    def test_detect_existing_resources_no_suffix(self):
        self._test_detect_existing_resources(None,
                                             {'other': '98765432-e3c0-41a5-880d-ebeb6b1ded5e',
                                              'default_mysuffix': '12345678-e3c0-41a5-880d-ebeb6b1ded5e'},
                                             {'testnet': '123123123-524c-406b-b7c1-9bc069251d22',
                                              'testnet2_mysuffix': '123123123-524c-406b-b7c1-987665441d22'},
                                             ['server1_mysuffix', 'server1'])

    def test_detect_existing_resources_with_suffix(self):
        self._test_detect_existing_resources('mysuffix',
                                             {'default': '12345678-e3c0-41a5-880d-ebeb6b1ded5e'},
                                             {'testnet2': '123123123-524c-406b-b7c1-987665441d22'},
                                             ['server1'])

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    def _test_detect_existing_resources(self, suffix, expected_secgroups, expected_networks, expected_nodes,
                                        get_nova_client, get_neutron_client):
        neutron = get_neutron_client.return_value
        neutron.list_ports.return_value = {'ports': [{'status': 'ACTIVE',
                                                      'name': '1298eefe-7654-49e0-8d38-47a6e4f75bd4',
                                                      'admin_state_up': True,
                                                      'network_id': '123123123-524c-406b-b7c1-9bc069251d22',
                                                      'tenant_id': 'c5f19d06a4194f138c873e97950d1f3c',
                                                      "device_owner": "",
                                                      "mac_address": "02:12:98:ee:fe:76",
                                                      "fixed_ips": [{"subnet_id": "3bc91c43-06a0-4a99-8e99-83703818d908",
                                                                     "ip_address": "10.0.0.4"}],
                                                      "id": "1298eefe-7654-49e0-8d38-47a6e4f75bd4",
                                                      "security_groups": ["7acbf890-e3c0-41a5-880d-ebeb6b1ded5e"],
                                                      "device_id": ""}]}

        neutron.list_networks.return_value = {'networks': [{'status': 'ACTIVE',
                                                            'router:external': False,
                                                            'subnets': ['12345678-acab-4949-b06b-b095f9a6ca8c'],
                                                            'name': 'testnet',
                                                            'admin_state_up': True,
                                                            'tenant_id': '987654331abcdef98765431',
                                                            'shared': False,
                                                            'id': '123123123-524c-406b-b7c1-9bc069251d22'},
                                                           {'status': 'ACTIVE',
                                                            'router:external': False,
                                                            'subnets': ['98766544-acab-4949-b06b-b095f9a6ca8c'],
                                                            'name': 'testnet2_mysuffix',
                                                            'admin_state_up': True,
                                                            'tenant_id': '987654331abcdef98765431',
                                                            'shared': False,
                                                            'id': '123123123-524c-406b-b7c1-987665441d22'}]}

        neutron.list_security_groups.return_value = {'security_groups': [{'id': '98765432-e3c0-41a5-880d-ebeb6b1ded5e',
                                                                          'tenant_id': '987654331abcdef98765431',
                                                                          'description': None,
                                                                          'security_group_rules': [],
                                                                          'name': 'other'},
                                                                         {'id': '12345678-e3c0-41a5-880d-ebeb6b1ded5e',
                                                                          'tenant_id': '987654331abcdef98765431',
                                                                          'description': None,
                                                                          'security_group_rules': [],
                                                                          'name': 'default_mysuffix'}]}
        nova = get_nova_client.return_value

        class Server(object):
            def __init__(self, name, id):
                self.name = name
                self.id = id
                self.addresses = {'testnet%s' % (suffix,): [{"OS-EXT-IPS-MAC:mac_addr": "02:12:98:ee:fe:76",
                                                             "version": 4,
                                                             "addr": "10.0.0.4",
                                                             "OS-EXT-IPS:type": "fixed"}]}

        nova.servers.list.return_value = [Server('server1', 'server1uuid'),
                                          Server('server1_mysuffix', 'server1_mysuffixuuid')]

        self.dr.suffix = suffix
        self.dr.detect_existing_resources()

        self.assertEquals(self.dr.secgroups, expected_secgroups)
        self.assertEquals(self.dr.networks, expected_networks)
        for node in expected_nodes:
            self.assertIn(node, self.dr.nodes)


    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    def test_find_floating_network(self, get_neutron_client):
        nc = get_neutron_client.return_value
        nc.list_networks.return_value = {'networks': [{'id': 'netuuid'}]}

        self.assertEquals(self.dr.find_floating_network(), 'netuuid')

        nc.list_networks.assert_called_once_with(**{'router:external': True})

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    @mock.patch('overcast.runner.DeploymentRunner.find_floating_network')
    def test_create_floating_ip(self, find_floating_network, get_neutron_client):
        nc = get_neutron_client.return_value

        find_floating_network.return_value = 'netuuid'

        nc.create_floatingip.return_value = {'floatingip': {'id': 'theuuid',
                                                            'floating_ip_address': '1.2.3.4'}}

        self.assertEquals(self.dr.create_floating_ip(), ('theuuid', '1.2.3.4'))

        nc.create_floatingip.assert_called_once_with({'floatingip': {'floating_network_id': 'netuuid'}})

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    def test_create_port(self, get_neutron_client):
        nc = get_neutron_client.return_value
        nc.create_port.return_value = {'port': {
                                         'status': 'DOWN',
                                         'name': '1298eefe-7654-49e0-8d38-47a6e4f75bd4',
                                         'admin_state_up': True,
                                         'network_id': '1fcda898-ae86-462e-a2d0-2cc2384b5898',
                                         'tenant_id': 'c5f19d06a4194f138c873e97950d1f3c',
                                         "device_owner": "",
                                         "mac_address": "02:12:98:ee:fe:76",
                                         "fixed_ips": [{"subnet_id": "3bc91c43-06a0-4a99-8e99-83703818d908",
                                                        "ip_address": "10.0.0.4"}],
                                         "id": "1298eefe-7654-49e0-8d38-47a6e4f75bd4",
                                         "security_groups": ["7acbf890-e3c0-41a5-880d-ebeb6b1ded5e"],
                                         "device_id": ""}}

        port = self.dr.create_port('port_name', 'network_id',
                                   ["7acbf890-e3c0-41a5-880d-ebeb6b1ded5e"])

        nc.create_port.assert_called_once_with({'port': {'name': 'port_name',
                                                         'admin_state_up': True,
                                                         'security_groups': ["7acbf890-e3c0-41a5-880d-ebeb6b1ded5e"],
                                                         'network_id': 'network_id'}})

        self.assertEquals(port, {'id': "1298eefe-7654-49e0-8d38-47a6e4f75bd4",
                                 'network_name': 'network_id',
                                 'mac': '02:12:98:ee:fe:76',
                                 'fixed_ip': '10.0.0.4'})


    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    def test_create_network(self, get_neutron_client):
        nc = get_neutron_client.return_value
        nc.create_network.return_value = {'network': {'id': 'theuuid'}}
        nc.create_subnet.return_value = {'subnet': {'id': 'thesubnetuuid'}}

        self.dr.record_resource = mock.MagicMock()
        self.dr.create_network('netname', {'cidr': '10.0.0.0/12'})

        nc.create_network.assert_called_once_with({'network': {'name': 'netname',
                                                               'admin_state_up': True}})
        nc.create_subnet.assert_called_once_with({'subnet': {'name': 'netname',
                                                             'cidr': '10.0.0.0/12',
                                                             'ip_version': 4,
                                                             'network_id': 'theuuid'}})
        self.dr.record_resource.assert_any_call('network', 'theuuid')
        self.dr.record_resource.assert_any_call('subnet', 'thesubnetuuid')

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    def test_create_security_group(self, get_neutron_client):
        nc = get_neutron_client.return_value
        nc.create_security_group.return_value = {'security_group': {'id': 'theuuid'}}
        nc.create_security_group_rule.side_effect = [{'security_group_rule': {'id': 'theruleuuid1'}},
                                                     {'security_group_rule': {'id': 'theruleuuid2'}}]

        self.dr.record_resource = mock.MagicMock()
        self.dr.create_security_group('secgroupname', [{'source_group': 'secgroupname',
                                                        'protocol': 'tcp',
                                                        'from_port': 23,
                                                        'to_port': 24},
                                                       {'cidr': '12.0.0.0/12',
                                                        'protocol': 'tcp',
                                                        'from_port': 21,
                                                        'to_port': 22}])

        nc.create_security_group.assert_called_once_with({'security_group': {'name': 'secgroupname'}})
        nc.create_security_group_rule.assert_any_call({'security_group_rule': {'remote_ip_prefix': '12.0.0.0/12',
                                                                               'direction': 'ingress',
                                                                               'ethertype': 'IPv4',
                                                                               'port_range_min': 21,
                                                                               'port_range_max': 22,
                                                                               'protocol': 'tcp',
                                                                               'security_group_id': 'theuuid'}})
        nc.create_security_group_rule.assert_any_call({'security_group_rule': {'remote_group_id': 'theuuid',
                                                                               'direction': 'ingress',
                                                                               'ethertype': 'IPv4',
                                                                               'port_range_min': 23,
                                                                               'port_range_max': 24,
                                                                               'protocol': 'tcp',
                                                                               'security_group_id': 'theuuid'}})
        self.dr.record_resource.assert_any_call('secgroup', 'theuuid')
        self.dr.record_resource.assert_any_call('secgroup_rule', 'theruleuuid1')
        self.dr.record_resource.assert_any_call('secgroup_rule', 'theruleuuid2')

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    def test_create_security_group_without_rules(self, get_neutron_client):
        nc = get_neutron_client.return_value
        nc.create_security_group.return_value = {'security_group': {'id': 'theuuid'}}

        self.dr.create_security_group('secgroupname', None)
        nc.create_security_group.assert_called_once_with({'security_group': {'name': 'secgroupname'}})

    @mock.patch('overcast.runner.DeploymentRunner.create_port')
    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    @mock.patch('overcast.runner.DeploymentRunner.get_cinder_client')
    @mock.patch('overcast.runner.time')
    def test_create_node(self, time, get_cinder_client, get_neutron_client, get_nova_client, create_port):
        nc = get_nova_client.return_value
        self.dr.record_resource = mock.MagicMock()

        nc.flavors.get.return_value = 'smallflavorobject'
        nc.images.get.return_value = 'trustyimageobject'
        nc.servers.create.return_value.id = 'serveruuid'

        def _create_port(_infoname, network, secgroups):
            return {'ephemeral': {'id': 'nicuuid1'},
                    'passedthrough': {'id': 'nicuuid2'}}[network]

        create_port.side_effect = _create_port

        self.dr.networks = {'ephemeral': 'theoneIjustcreated'}
        self.dr.secgroups = {}
        self.dr.mappings = {'images': {'trusty': 'trustyuuid'},
                            'flavors': {'small': 'smallid'}}

        node = overcast.runner.Node('test1_x123',
                                    {'image': 'trusty',
                                     'flavor': 'small',
                                     'disk': 10,
                                     'networks': [{'network': 'ephemeral', 'assign_floating_ip': True},
                                                  {'network': 'passedthrough'}]},
                                    userdata='foo',
                                    keypair='key_x123',
                                    runner=self.dr)

        cinderclient = get_cinder_client.return_value
        class Volume(object):
            def __init__(self, uuid):
                self.id = uuid
                self.statuses = ['downloading', 'downloading', 'available']

            @property
            def status(self):
                return self.statuses.pop()

        cinderclient.volumes.get.return_value = Volume('voluuid')

        node.build()

        nc.flavors.get.assert_called_with('smallid')

        nc.servers.create.assert_called_with('test1_x123',
                                             nics=[{'port-id': 'nicuuid1'},
                                                   {'port-id': 'nicuuid2'}],
                                             block_device_mapping={'vda': 'voluuid:::1'},
                                             image=None,
                                             userdata='foo',
                                             key_name='key_x123',
                                             flavor='smallflavorobject')

        self.dr.record_resource.assert_any_call('port', 'nicuuid1')
        self.dr.record_resource.assert_any_call('port', 'nicuuid2')
        self.dr.record_resource.assert_any_call('server', 'serveruuid')

    def test_list_refs_human(self):
        self._test_list_refs(False, 'Images:\n  trusty\n\nFlavors:\n  bootstrap\n')

    def test_list_refs_cfg_tmpl(self):
        self._test_list_refs(True, '[images]\ntrusty = <missing value>\n\n[flavors]\nbootstrap = <missing value>\n\n')

    def _test_list_refs(self, tmpl_, expected_value):
        example_file = os.path.join(os.path.dirname(__file__),
                                    'examplestack1.yaml')
        class Args(object):
            stack = example_file
            tmpl = tmpl_

        args = Args()
        output = StringIO()
        overcast.runner.list_refs(args, output)
        self.assertEquals(output.getvalue(), expected_value)


    @mock.patch('overcast.runner.Node.build')
    def test__create_node(self, node_build):
        self.dr.nodes['existing_node'] = overcast.runner.Node('existing_node', {}, self.dr)

        self.assertEquals(self.dr._create_node('nodename', {}, 'keypair', ''),
                          'nodename')

        self.assertIn('nodename', self.dr.nodes)

        self.dr.nodes['nodename'].build.assert_called_once_with()

    def test_poll_pending_nodes_retry(self):
        self.dr.nodes['node1'] = node1 = mock.MagicMock()
        self.dr.nodes['node2'] = node2 = mock.MagicMock()

        def decrement_attempts_left(node):
            node.attempts_left -= 1

        node1.build.side_effect = lambda: decrement_attempts_left(node1)
        node2.build.side_effect = lambda: decrement_attempts_left(node2)

        node1.poll.side_effect = ['BUILD', 'BUILD', 'BUILD', 'ACTIVE']
        node2.poll.side_effect = ['BUILD', 'BUILD', 'ERROR', 'BUILD', 'BUILD', 'ERROR']

        node1.attempts_left = 2
        node2.attempts_left = 1

        self.dr.retry_count = 2

        pending_nodes = set(['node1', 'node2'])

        # Both are still BUILD
        pending_nodes = self.dr._poll_pending_nodes(pending_nodes)
        self.assertEquals(pending_nodes, set(['node1', 'node2']))

        # Both are still BUILD
        pending_nodes = self.dr._poll_pending_nodes(pending_nodes)
        self.assertEquals(pending_nodes, set(['node1', 'node2']))

        self.assertFalse(node1.clean.called)
        self.assertFalse(node1.build.called)
        self.assertFalse(node2.clean.called)
        self.assertFalse(node2.build.called)

        # node1 is still BUILD, node2 is ERROR
        pending_nodes = self.dr._poll_pending_nodes(pending_nodes)
        self.assertEquals(pending_nodes, set(['node1', 'node2']))

        self.assertFalse(node1.clean.called)
        self.assertFalse(node1.build.called)

        node2.clean.assert_called_with()
        node2.build.assert_called_with()

        self.assertEquals(self.dr.nodes['node2'], node2, 'Node obj was replaced')

        # node1 become ACTIVE, node2 is BUILD again
        pending_nodes = self.dr._poll_pending_nodes(pending_nodes)
        self.assertEquals(pending_nodes, set(['node2']))

        # node2 is still BUILD
        pending_nodes = self.dr._poll_pending_nodes(pending_nodes)
        self.assertEquals(pending_nodes, set(['node2']))

        # node2 fails, so we give up
        self.assertRaises(overcast.exceptions.ProvisionFailedException,
                          self.dr._poll_pending_nodes, pending_nodes)


    @mock.patch('overcast.runner.DeploymentRunner.create_network')
    @mock.patch('overcast.runner.DeploymentRunner.create_security_group')
    @mock.patch('overcast.runner.DeploymentRunner._create_node')
    @mock.patch('overcast.runner.DeploymentRunner._poll_pending_nodes')
    @mock.patch('overcast.runner.time')
    def test_provision_step(self, time, _poll_pending_nodes, _create_node,
                            create_security_group, create_network):
        create_network.return_value = 'netuuid'
        create_security_group.return_value = 'sguuid'
        self.dr.suffix = 'x123'
        _poll_pending_nodes.side_effect = [set(['other', 'bootstrap1', 'bootstrap2']),
                                           set(['bootstrap1', 'bootstrap2']),
                                           set(['bootstrap1']),
                                           set()]

        _create_node.side_effect = lambda base_name, node_info, keypair_name, userdata: base_name

        self.dr.provision_step({'stack': 'overcast/tests/runner/examplestack1.yaml'})

        create_network.assert_called_with('undercloud_x123', {'cidr': '10.240.292.0/24'})
        create_security_group.assert_called_with('jumphost',
                                                 [{'to_port': 22,
                                                   'cidr': '0.0.0.0/0',
                                                   'from_port': 22}])
        self.assertEquals(_poll_pending_nodes.mock_calls,
                          [mock.call(set(['other', 'bootstrap1', 'bootstrap2'])),
                           mock.call(set(['other', 'bootstrap1', 'bootstrap2'])),
                           mock.call(set(['bootstrap1', 'bootstrap2'])),
                           mock.call(set(['bootstrap1']))])
        _create_node.assert_any_call('other',
                                     {'networks': [{'securitygroups': ['jumphost'],
                                                    'network': 'default',
                                                    'assign_floating_ip': True},
                                               {'network': 'undercloud'}],
                                      'flavor': 'bootstrap',
                                      'image': 'trusty'},
                                     userdata=None,
                                     keypair_name=None)
        _create_node.assert_any_call('bootstrap1',
                                     {'networks': [{'securitygroups': ['jumphost'], 'network': 'default'},
                                                   {'network': 'undercloud'}],
                                      'flavor': 'bootstrap',
                                      'image': 'trusty'},
                                     userdata=None,
                                     keypair_name=None)
        _create_node.assert_any_call('bootstrap2',
                                     {'networks': [{'securitygroups': ['jumphost'], 'network': 'default'},
                                                   {'network': 'undercloud'}],
                                      'flavor': 'bootstrap',
                                      'image': 'trusty'},
                                     userdata=None,
                                     keypair_name=None)

    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    def test_delete_server(self, get_nova_client):
        nc = get_nova_client.return_value

        self.dr.delete_server('someuuid')

        nc.servers.delete.assert_called_with('someuuid')

    @mock.patch('overcast.runner.DeploymentRunner.get_nova_client')
    def test_delete_keypair(self, get_nova_client):
        nc = get_nova_client.return_value

        self.dr.delete_keypair('somename')

        nc.keypairs.delete.assert_called_with('somename')

    def test_delete_floatingip(self):
        self._test_delete_neutron_resource('floatingip')

    def test_delete_network(self):
        self._test_delete_neutron_resource('network')

    def test_delete_port(self):
        self._test_delete_neutron_resource('port')

    def test_delete_subnet(self):
        self._test_delete_neutron_resource('subnet')

    def test_delete_secgroup(self):
        self._test_delete_neutron_resource('secgroup',
                                           neutron_type='security_group')

    def test_delete_secgroup_rule(self):
        self._test_delete_neutron_resource('secgroup_rule',
                                           neutron_type='security_group_rule')

    @mock.patch('overcast.runner.DeploymentRunner.get_neutron_client')
    def _test_delete_neutron_resource(self, resource_type, get_neutron_client, neutron_type=None):
        nc = get_neutron_client.return_value

        delete_method = getattr(self.dr, 'delete_%s' % (resource_type,))
        delete_method('someuuid')

        neutron_type = neutron_type or resource_type
        nc_delete_method = getattr(nc, 'delete_%s' % (neutron_type,))
        nc_delete_method.assert_called_with('someuuid')
