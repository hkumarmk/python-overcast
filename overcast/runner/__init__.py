#!/usr/bin/env python
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

import argparse
import ConfigParser
import logging
import os
import pipes
import select
import subprocess
import sys
import time
import yaml

from novaclient.exceptions import Conflict

from overcast import utils
from overcast import exceptions

def load_yaml(f='.overcast.yaml'):
    with open(f, 'r') as fp:
        return yaml.load(fp)

def load_mappings(f='.overcast.mappings.ini'):
    with open(f, 'r') as fp:
        parser = ConfigParser.SafeConfigParser()
        parser.readfp(fp)
        mappings = {}
        for t in ('flavors', 'networks', 'images'):
            mappings[t] = {}
            if parser.has_section(t):
                mappings[t].update(parser.items(t))

        return mappings

def find_weak_refs(stack):
    images = set()
    flavors = set()
    networks = set()
    for node_name, node in stack['nodes'].items():
        images.add(node['image'])
        flavors.add(node['flavor'])
        networks.update([n['network'] for n in node['networks']])

    dynamic_networks = set()
    for network_name, network in stack.get('networks', {}).items():
        dynamic_networks.add(network_name)

    return images, flavors, networks-dynamic_networks

def list_refs(args, stdout=sys.stdout):
    stack = load_yaml(args.stack)
    images, flavors, networks = find_weak_refs(stack)
    if args.tmpl:
        cfg = ConfigParser.SafeConfigParser()
        cfg.add_section('images')
        cfg.add_section('flavors')
        for image in images:
            cfg.set('images', image, '<missing value>')
        for flavor in flavors:
            cfg.set('flavors', flavor, '<missing value>')
        cfg.write(stdout)
    else:
        stdout.write('Images:\n  ')

        if images:
            stdout.write('  '.join(images))
        else:
            stdout.write('None')

        stdout.write('\n\nFlavors:\n  ')

        if flavors:
            stdout.write('  '.join(flavors))
        else:
            stdout.write('None')

        stdout.write('\n')

def run_cmd_once(shell_cmd, real_cmd, environment, deadline):
    proc = subprocess.Popen(shell_cmd,
                            env=environment,
                            shell=True,
                            stdin=subprocess.PIPE)
    stdin = real_cmd + '\n'
    while True:
        if stdin:
            _, rfds, xfds = select.select([], [proc.stdin], [proc.stdin], 1)
            if rfds:
                proc.stdin.write(stdin[0])
                stdin = stdin[1:]
                if not stdin:
                    proc.stdin.close()
            if xfds:
                if proc.stdin.feof():
                    stdin = ''

        if proc.poll() is not None:
            if proc.returncode == 0:
                return True
            else:
                raise exceptions.CommandFailedException(stdin)

        if deadline and time.time() > deadline:
            if proc.poll() is None:
                proc.kill()
            raise exceptions.CommandTimedOutException(stdin)


def get_creds_from_env():
    d = {}
    d['username'] = os.environ['OS_USERNAME']
    d['password'] = os.environ['OS_PASSWORD']
    d['auth_url'] = os.environ['OS_AUTH_URL']
    d['tenant_name'] = os.environ['OS_TENANT_NAME']
#    d['region_name'] = os.environ.get('OS_REGION_NAME')
#    d['cacert'] = os.environ.get('OS_CACERT', None)
    return d


class Node(object):

    def __init__(self, name, info, keypair, userdata, runner):
        self.record_resource = lambda *args, **kwargs: None
        self.name = name
        self.info = info
        self.runner = runner
        self.keypair = keypair
        self.userdata = userdata
        self.server_id = None
        self.fip_ids = set()
        self.port_ids = set()
        self.fip_addresses = set()
        self.server_status = None
        self.nc = self.runner.get_nova_client()
        if self.info['image'] in self.runner.mappings.get('images', {}):
            self.info['image'] = self.runner.mappings['images'][self.info['image']]

        if self.info['flavor'] in self.runner.mappings.get('flavors', {}):
            self.info['flavor'] = self.runner.mappings['flavors'][self.info['flavor']]
        self.image = self.nc.images.get(self.info['image'])
        self.flavor = self.nc.flavors.get(self.info['flavor'])

    def poll(self, desired_status = 'ACTIVE'):
        """
        This one poll nova and return the server status
        """
        if self.server_status != desired_status:
            self.server_status = self.nc.servers.get(self.server_id).status
        return self.server_status

    def clean(self):
        """
        Cleaner: This method remove server, fip, port etc.
        We could keep fip and may be ports (ports are getting deleted with current
        neutron client), but that is going to be bit more complex to make sure
        right port is assigned to right fip etc, so atm, just removing them.
        """
        for fip_id in self.fip_ids:
            self.runner.delete_floating_ip(fip_id)
        self.fip_ids = set()
        self.fip_addresses = set()

        for port_id in self.port_ids:
            self.runner.delete_port(port_id)
        self.port_ids = set()

        server = self.nc.servers.delete(self.server_id)
        self.server_id = None

    def build(self):

        def _map_network(network):
            if network in self.runner.mappings.get('networks', {}):
                netid = self.runner.mappings['networks'][network]
            elif network in self.runner.networks:
                netid = self.runner.networks[network]
            else:
                netid = network

            return netid

        nics = []
        for eth_idx, network in enumerate(self.info['networks']):
           port_name = '%s_eth%d' % (self.name, eth_idx)
           port_id = self.runner.create_port(port_name, _map_network(network['network']),
                                      [self.runner.secgroups[secgroup] for secgroup in network.get('secgroups', [])])
           self.runner.record_resource('port', port_id)
           self.port_ids.add(port_id)

           if network.get('assign_floating_ip', False):
              fip_id, fip_address = self.runner.create_floating_ip()
              self.runner.associate_floating_ip(port_id, fip_id)
              self.fip_addresses.add(fip_address)
              self.fip_ids.add(fip_id)

           nics.append({'port-id': port_id})

        bdm = [{'source_type': 'image',
                'uuid': self.info['image'],
                'destination_type': 'volume',
                'volume_size': self.info['disk'],
                'delete_on_termination': 'true',
                'boot_index': '0'}]
        server = self.nc.servers.create(self.name, image=None,
                                   block_device_mapping_v2=bdm,
                                   flavor=self.flavor, nics=nics,
                                   key_name=self.keypair, userdata=self.userdata)
        self.runner.record_resource('server', server.id)
        self.server_id = server.id

class DeploymentRunner(object):
    def __init__(self, config=None, suffix=None, mappings=None, key=None,
                 record_resource=None):
        self.cfg = config
        self.suffix = suffix
        self.mappings = mappings or {}
        self.key = key
        self.record_resource = lambda *args, **kwargs: None

        self.conncache = {}
        self.networks = {}
        self.secgroups = {}
        self.nodes = {}

    def get_keystone_session(self):
        from keystoneclient import session as keystone_session
        from keystoneclient.auth.identity import v2 as keystone_auth_id_v2
        if 'keystone_session' not in self.conncache:
            self.conncache['keystone_auth'] = keystone_auth_id_v2.Password(**get_creds_from_env())
            self.conncache['keystone_session'] = keystone_session.Session(auth=self.conncache['keystone_auth'])
        return self.conncache['keystone_session']

    def get_keystone_client(self):
        from keystoneclient.v2_0 import client as keystone_client
        if 'keystone' not in self.conncache:
            ks = self.get_keystone_session()
            self.conncache['keystone'] = keystone_client.Client(session=ks)
        return self.conncache['keystone']

    def get_nova_client(self):
        import novaclient.client as novaclient
        if 'nova' not in self.conncache:
            ks = self.get_keystone_session()
            self.conncache['nova'] = novaclient.Client("1.1", session=ks)
        return self.conncache['nova']

    def get_neutron_client(self):
        import neutronclient.neutron.client as neutronclient
        if 'neutron' not in self.conncache:
            ks = self.get_keystone_session()
            self.conncache['neutron'] = neutronclient.Client('2.0', session=ks)
        return self.conncache['neutron']

    def detect_existing_resources(self):
        neutron = self.get_neutron_client()

        suffix = self.add_suffix('')
        if suffix:
            strip_suffix = lambda s:s[:-len(suffix)]
        else:
            strip_suffix = lambda s:s

        for network in neutron.list_networks()['networks']:
            if network['name'].endswith(suffix):
                base_name = strip_suffix(network['name'])
                if base_name in self.networks:
                    raise exceptions.DuplicateResourceException('Network', network['name'])

                self.networks[base_name] = network['id']

        for secgroup in neutron.list_security_groups()['security_groups']:
            if secgroup['name'].endswith(suffix):
                base_name = strip_suffix(secgroup['name'])
                if base_name in self.secgroups:
                    raise exceptions.DuplicateResourceException('Security Group', secgroup['name'])

                self.secgroups[base_name] = secgroup['id']

        nova = self.get_nova_client()

        for node in nova.servers.list():
            if node.name.endswith(suffix):
                base_name = strip_suffix(node.name)
                if base_name in self.nodes:
                    raise exceptions.DuplicateResourceException('Node', node.name)

                self.nodes[base_name] = node.id

    def delete_port(self, uuid):
        nc = self.get_neutron_client()
        nc.delete_port(uuid)

    def delete_network(self, uuid):
        nc = self.get_neutron_client()
        nc.delete_network(uuid)

    def delete_subnet(self, uuid):
        nc = self.get_neutron_client()
        nc.delete_subnet(uuid)

    def delete_secgroup(self, uuid):
        nc = self.get_neutron_client()
        nc.delete_security_group(uuid)

    def delete_secgroup_rule(self, uuid):
        nc = self.get_neutron_client()
        nc.delete_security_group_rule(uuid)

    def delete_floatingip(self, uuid):
        nc = self.get_neutron_client()
        nc.delete_floatingip(uuid)

    def delete_keypair(self, name):
        nc = self.get_nova_client()
        nc.keypairs.delete(name)

    def delete_server(self, uuid):
        nc = self.get_nova_client()
        nc.servers.delete(uuid)

    def create_port(self, name, network, secgroups):
        nc = self.get_neutron_client()
        port = {'name': name,
                'admin_state_up': True,
                'network_id': network,
                'security_groups': secgroups}
        port = nc.create_port({'port': port})
        return port['port']['id']

    def create_keypair(self, name, keydata):
        nc = self.get_nova_client()
        try:
            nc.keypairs.create(name, keydata)
        except Conflict:
            pass

    def find_floating_network(self, ):
        nc = self.get_neutron_client()
        networks = nc.list_networks(**{'router:external': True})
        return networks['networks'][0]['id']

    def create_floating_ip(self):
        nc = self.get_neutron_client()
        floating_network = self.find_floating_network()
        floatingip = {'floating_network_id': floating_network}
        floatingip = nc.create_floatingip({'floatingip': floatingip})
        self.record_resource('floatingip', floatingip['floatingip']['id'])
        return (floatingip['floatingip']['id'],
                floatingip['floatingip']['floating_ip_address'])

    def associate_floating_ip(self, port_id, fip_id):
        nc = self.get_neutron_client()
        nc.update_floatingip(fip_id, {'floatingip': {'port_id': port_id}})

    def create_network(self, name, info):
        nc = self.get_neutron_client()
        network = {'name': name, 'admin_state_up': True}
        network = nc.create_network({'network': network})
        self.record_resource('network', network['network']['id'])

        subnet = {"network_id": network['network']['id'],
                  "ip_version": 4,
                  "cidr": info['cidr'],
                  "name": name}
        subnet = nc.create_subnet({'subnet': subnet})
        self.record_resource('subnet', subnet['subnet']['id'])

        return network['network']['id']

    def create_security_group(self, name, info):
        nc = self.get_neutron_client()
        secgroup = {'name': name}
        secgroup = nc.create_security_group({'security_group': secgroup})
        self.record_resource('secgroup', secgroup['security_group']['id'])

        for rule in (info or []):
            secgroup_rule = {"direction": "ingress",
                             "remote_ip_prefix": rule['cidr'],
                             "ethertype": "IPv4",
                             "port_range_min": rule['from_port'],
                             "port_range_max": rule['to_port'],
                             "protocol": rule['protocol'],
                             "security_group_id": secgroup['security_group']['id']}
            secgroup_rule = nc.create_security_group_rule({'security_group_rule': secgroup_rule})
            self.record_resource('secgroup_rule', secgroup_rule['security_group_rule']['id'])
        return secgroup['security_group']['id']

    def shell_step(self, details, environment=None):
        env_prefix = 'ALL_NODES=%s' % pipes.quote(' '.join([self.add_suffix(s) for s in self.nodes.keys()]))

        cmd = self.shell_step_cmd(details, env_prefix)

        if details.get('total-timeout', False):
            overall_deadline = time.time() + utils.parse_time(details['total-timeout'])
        else:
            overall_deadline = None

        if details.get('timeout', False):
            individual_exec_limit = utils.parse_time(details['timeout'])
        else:
            individual_exec_limit = None

        if details.get('retry-delay', False):
            retry_delay = utils.parse_time(details['retry-delay'])
        else:
            retry_delay = 0

        def wait():
            time.sleep(retry_delay)

        # Four settings matter here:
        # retry-if-fails: True/False
        # retry-delay: Time to wait between retries
        # timeout: Max time per command execution
        # total-timeout: How long time to spend on this in total
        while True:
            if individual_exec_limit:
                deadline = time.time() + individual_exec_limit
                if overall_deadline:
                    if deadline > overall_deadline:
                        deadline = overall_deadline
            elif overall_deadline:
                deadline = overall_deadline
            else:
                deadline = None

            try:
                run_cmd_once(cmd, details['cmd'], environment, deadline)
                break
            except exceptions.CommandFailedException:
                if details.get('retry-if-fails', False):
                    wait()
                    continue
                raise
            except exceptions.CommandTimedOutException:
                if details.get('retry-if-fails', False):
                    if time.time() + retry_delay < deadline:
                        wait()
                        continue
                raise

    def shell_step_cmd(self, details, env_prefix=''):
        if details.get('type', None) == 'remote':
            for fip_addr in self.nodes[details['node']].fip_addresses:
                node = fip_addr
                break
            return 'ssh -o StrictHostKeyChecking=no ubuntu@%s "%s bash"' % (node, env_prefix)
        else:
             return '%s bash' % (env_prefix,)

    def add_suffix(self, s):
        if self.suffix:
            return '%s_%s' % (s, self.suffix)
        else:
            return s

    def provision_step(self, details):
        stack = load_yaml(details['stack'])

        if self.key:
            keypair_name = self.add_suffix('pubkey')
            self.create_keypair(keypair_name, self.key)
            self.record_resource('keypair', keypair_name)
        else:
            keypair_name = None

        if 'userdata' in details:
            with open(details['userdata'], 'r') as fp:
                userdata = fp.read()
        else:
            userdata = None

        pending_nodes = set()

        if details.get('retry-delay', False):
            retry_delay = utils.parse_time(details['retry-delay'])
        else:
            retry_delay = 5

        if details.get('timeout', False):
            provision_timeout = utils.parse_time(details['timeout'])
            deadline =  time.time() + provision_timeout
        else:
            provision_timeout = None
            deadline = None

        def wait():
            time.sleep(retry_delay)

        for base_network_name, network_info in stack['networks'].items():
            if base_network_name in self.networks:
                continue
            network_name = self.add_suffix(base_network_name)
            self.networks[base_network_name] = self.create_network(network_name,
                                                                   network_info)

        for base_secgroup_name, secgroup_info in stack['securitygroups'].items():
            if base_secgroup_name in self.secgroups:
                continue
            secgroup_name = self.add_suffix(base_secgroup_name)
            self.secgroups[base_secgroup_name] = self.create_security_group(secgroup_name,
                                                                            secgroup_info)

        for base_node_name, node_info in stack['nodes'].items():
            if base_node_name in self.nodes:
                continue
            def _create_node(base_name):
                node_name = self.add_suffix(base_name)
                self.nodes[base_name] = Node(node_name, node_info,
                                                        keypair=keypair_name,
                                                        userdata=userdata,
                                                        runner=self)
                self.nodes[base_name].build()

            if 'number' in node_info:
                count = node_info.pop('number')
                for idx in range(1, count+1):
                    node_name = '%s%d' % (base_node_name, idx)
                    _create_node(node_name)
                    pending_nodes.add(node_name)
            else:
                _create_node(base_node_name)
                pending_nodes.add(base_node_name)

        while True:
            done = set()
            for name in pending_nodes:
                if self.nodes[name].poll() == 'ACTIVE':
                    done.add(name)
                if self.nodes[name].poll() == 'ERROR':
                    if details.get('retry-if-fails', False):
                        wait()
                        if deadline and time.time() > deadline:
                            raise exceptions.ProvisionTimedOutException()
                        self.nodes[name].clean()
                        self.nodes[name].build()
                    raise exceptions.ProvisionFailedException()
            pending_nodes = pending_nodes.difference(done)
            if not pending_nodes:
                break
            time.sleep(5)

    def deploy(self, name):
        for step in self.cfg[name]:
            step_type = step.keys()[0]
            details = step[step_type]
            func = getattr(self, '%s_step' % step_type)
            func(details)


def main(argv=sys.argv[1:], stdout=sys.stdout):
    def deploy(args):
        cfg = load_yaml(args.cfg)

        if args.key:
            with open(args.key, 'r') as fp:
                key = fp.read()


        dr = DeploymentRunner(config=cfg,
                              suffix=args.suffix,
                              mappings=load_mappings(args.mappings),
                              key=key)

        if args.cont:
            dr.detect_existing_resources()

        if args.cleanup:
            with open(args.cleanup, 'a+') as cleanup:
                def record_resource(type_, id):
                    cleanup.write('%s: %s\n' % (type_, id))
                dr.record_resource = record_resource

                dr.deploy(args.name)
        else:
            dr.deploy(args.name)

    def cleanup(args):
        dr = DeploymentRunner()

        with open(args.log, 'r') as fp:
            lines = [l.strip() for l in fp]

        lines.reverse()
        for l in lines:
            resource_type, uuid = l.split(': ')
            func = getattr(dr, 'delete_%s' % resource_type)
            try:
                func(uuid)
            except Exception, e:
                print e

    parser = argparse.ArgumentParser(description='Run deployment')

    subparsers = parser.add_subparsers(help='Subcommand help')
    list_refs_parser = subparsers.add_parser('list-refs',
                                             help='List symbolic resources')
    list_refs_parser.set_defaults(func=list_refs)
    list_refs_parser.add_argument('--tmpl', action='store_true',
                                  help='Output template ini file')
    list_refs_parser.add_argument('stack', help='YAML file describing stack')

    deploy_parser = subparsers.add_parser('deploy', help='Perform deployment')
    deploy_parser.set_defaults(func=deploy)
    deploy_parser.add_argument('--cfg', default='.overcast.yaml',
                               help='Deployment config file')
    deploy_parser.add_argument('--suffix', help='Resource name suffix')
    deploy_parser.add_argument('--mappings', help='Resource map file')
    deploy_parser.add_argument('--key', help='Public key file')
    deploy_parser.add_argument('--cleanup', help='Cleanup file')
    deploy_parser.add_argument('--incremental', dest='cont', action='store_true',
                               help="Don't create resources if identically named ones already exist")
    deploy_parser.add_argument('name', help='Deployment to perform')

    cleanup_parser = subparsers.add_parser('cleanup', help='Clean up')
    cleanup_parser.set_defaults(func=cleanup)
    cleanup_parser.add_argument('log', help='Clean up log (generated by deploy)')

    args = parser.parse_args(argv)

    if args.func:
        args.func(args)

if __name__ == '__main__':
    main()
