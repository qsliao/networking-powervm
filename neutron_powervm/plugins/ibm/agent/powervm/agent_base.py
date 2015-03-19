# Copyright 2015 IBM Corp.
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy

import eventlet
eventlet.monkey_patch()

from oslo_config import cfg
from oslo_log import log as logging

from neutron.agent.common import config as a_config
from neutron.agent import rpc as agent_rpc
from neutron.common import constants as q_const
from neutron.common import topics
from neutron import context as ctx
from neutron.i18n import _LW, _LE
from neutron.openstack.common import loopingcall

from neutron_powervm.plugins.ibm.agent.powervm import utils

import time


LOG = logging.getLogger(__name__)


agent_opts = [
    cfg.IntOpt('polling_interval', default=2,
               help=_("The number of seconds the agent will wait between "
                      "polling for local device changes.")),
    cfg.IntOpt('heal_and_optimize_interval', default=300,
               help=_('The number of seconds the agent should wait between '
                      'heal/optimize intervals.  Should be higher than the '
                      'polling_interval as it runs in the nearest polling '
                      'loop.')),
    # TODO(thorst) Reevaluate as the API auth model evolves
    cfg.StrOpt('pvm_host_mtms',
               default='',
               help='The Model Type/Serial Number of the host server to '
                    'manage.  Format is MODEL-TYPE*SERIALNUM.  Example is '
                    '8286-42A*1234ABC.'),
    cfg.StrOpt('pvm_server_ip',
               default='localhost',
               help='The IP Address hosting the PowerVM REST API'),
    cfg.StrOpt('pvm_user_id',
               default='',
               help='The user id for authentication into the API.'),
    cfg.StrOpt('pvm_pass',
               default='',
               help='The password for authentication into the API.')
]

cfg.CONF.register_opts(agent_opts, "AGENT")
a_config.register_agent_state_opts_helper(cfg.CONF)
a_config.register_root_helper(cfg.CONF)

ACONF = cfg.CONF.AGENT


class PVMPluginApi(agent_rpc.PluginApi):
    pass


class PVMRpcCallbacks(object):
    '''
    Provides call backs (as defined in the setup_rpc method within the
    appropriate Neutron Agent class) that will be invoked upon certain
    actions from the controller.
    '''

    # This agent supports RPC Version 1.0.  Though agents don't boot unless
    # 1.1 or higher is specified now.
    # For reference:
    #  1.0 Initial version
    #  1.1 Support Security Group RPC
    #  1.2 Support DVR (Distributed Virtual Router) RPC
    RPC_API_VERSION = '1.1'

    def __init__(self, agent):
        '''
        Creates the call back.  Most of the call back methods will be
        delegated to the agent.

        :param agent: The owning agent to delegate the callbacks to.
        '''
        super(PVMRpcCallbacks, self).__init__()
        self.agent = agent

    def port_update(self, context, **kwargs):
        port = kwargs['port']
        self.agent._update_port(port)
        LOG.debug(_("port_update RPC received for port: %s"), port['id'])

    def network_delete(self, context, **kwargs):
        network_id = kwargs.get('network_id')

        # TODO(thorst) Need to perform the call back
        LOG.debug(_("network_delete RPC received for network: %s"), network_id)


class BasePVMNeutronAgent(object):
    """Baseline PowerVM Neutron Agent class for extension.

    The ML2 agents have a common RPC polling framework and API callback
    mechanism.  This class provides the baseline so that other children
    classes can extend and focus on their specific functions rather than
    integration with the RPC server.
    """

    def __init__(self, binary_name, agent_type):
        self.agent_state = {'binary': binary_name, 'host': cfg.CONF.host,
                            'topic': q_const.L2_AGENT_TOPIC,
                            'configurations': {}, 'agent_type': agent_type,
                            'start_flag': True}
        self.setup_rpc()

        # A list of ports that maintains the list of current 'modified' ports
        self.updated_ports = set()

        # Create the utility class that enables work against the Hypervisors
        # Shared Ethernet NetworkBridge.
        password = ACONF.pvm_pass.decode('base64', 'strict')
        self.api_utils = utils.PVMUtils(ACONF.pvm_server_ip, ACONF.pvm_user_id,
                                        password, ACONF.pvm_host_mtms)

    def setup_rpc(self):
        """Registers the RPC consumers for the plugin."""
        self.agent_id = 'sea-agent-%s' % cfg.CONF.host
        self.topic = topics.AGENT
        self.plugin_rpc = PVMPluginApi(topics.PLUGIN)
        self.state_rpc = agent_rpc.PluginReportStateAPI(topics.PLUGIN)

        self.context = ctx.get_admin_context_without_session()

        # Defines what will be listening for incoming events from the
        # controller.
        self.endpoints = [PVMRpcCallbacks(self)]

        # Define the listening consumers for the agent.  ML2 only supports
        # these two update types.
        consumers = [[topics.PORT, topics.UPDATE],
                     [topics.NETWORK, topics.DELETE]]

        self.connection = agent_rpc.create_consumers(self.endpoints,
                                                     self.topic,
                                                     consumers)

        # Report interval is for the agent health check.
        report_interval = cfg.CONF.AGENT.report_interval
        if report_interval:
            hb = loopingcall.FixedIntervalLoopingCall(self._report_state)
            hb.start(interval=report_interval)

    def _report_state(self):
        '''
        Reports the state of the agent back to the controller.  Controller
        knows that if a response isn't provided in a certain period of time
        then the agent is dead.  This call simply tells the controller that
        the agent is alive.
        '''
        # TODO(thorst) provide some level of devices connected to this agent.
        try:
            device_count = 0
            self.agent_state.get('configurations')['devices'] = device_count
            self.state_rpc.report_state(self.context,
                                        self.agent_state)
            self.agent_state.pop('start_flag', None)
        except Exception:
            LOG.exception(_("Failed reporting state!"))

    def _update_port(self, port):
        '''
        Invoked to indicate that a port has been updated within Neutron.
        '''
        self.updated_ports.append(port)

    def _list_updated_ports(self):
        '''
        Will return (and then reset) the list of updated ports received
        from the system.
        '''
        ports = copy.copy(self.updated_ports)
        self.updated_ports = []
        return ports

    def heal_and_optimize(self, is_boot):
        """Ensures that the bridging supports all the needed ports.

        This method is invoked periodically (not on every RPC loop).  Its
        purpose is to ensure that the bridging supports every client VM
        properly.  If possible, it should also optimize the connections.

        :param is_boot: Indicates if this is the first call on boot up of the
                        agent.
        """
        raise NotImplementedError()

    def provision_ports(self, ports):
        """Invoked when a set of new Neutron ports has been detected.

        This method should provision the bridging for the new ports.  This
        does not involve setting the client side adapters (that is done
        via nova VIF plugging) but instead make sure that the adapter is
        bridged out to the physical network.

        Must be implemented by a subclass.

        :param ports: The new ports that are to be provisioned.  Is a set
                      of neutron ports.
        """
        raise NotImplementedError()

    def rpc_loop(self):
        '''
        Runs a check periodically to determine if new ports were added or
        removed.  Will call down to appropriate methods to determine correct
        course of action.
        '''

        loop_timer = float(0)
        loop_interval = float(ACONF.heal_and_optimize_interval)
        first_loop = True
        succesive_exceptions = 0

        while True:
            try:
                # If the loop interval has passed, heal and optimize
                if time.time() - loop_timer > loop_interval:
                    LOG.debug("Performing heal and optimization of system.")
                    self.heal_and_optimize(first_loop)
                    first_loop = False
                    loop_timer = time.time()

                # Determine if there are new ports
                u_ports = self._list_updated_ports()

                # If there are no updated ports, just sleep and re-loop
                if not u_ports:
                    LOG.debug("No changes, sleeping %d seconds." %
                              ACONF.polling_interval)
                    time.sleep(ACONF.polling_interval)
                    continue

                # Provision the ports on the Network Bridge.
                self.provision_ports(u_ports)
                succesive_exceptions = 0
            except Exception as e:
                # The agent should retry a few times, in case something
                # bubbled up.  A successful provision loop will reset the
                # timer.
                #
                # Note that the exception timer is not reset if there are no
                # provisions.  That is because 99% of the errors will occur
                # in the provision path.  So we only reset the error when
                # a successful provision has occurred (otherwise we'd never
                # hit the exception limit).
                succesive_exceptions += 1
                LOG.exception(e)
                if succesive_exceptions == 3:
                    LOG.error(_LE("Multiple exceptions have been "
                                  "encountered.  The agent is unable to "
                                  "proceed.  Exiting."))
                    raise
                else:
                    LOG.warn(_LW("Error has been encountered and logged.  The "
                             "agent will retry again."))
