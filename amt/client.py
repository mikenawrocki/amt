# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# This file is heavily derived from the Ironic AMT driver at
# https://github.com/openstack/ironic/tree/master/ironic/drivers/modules/amt
#
# Thanks much for the hard work of people in that project to produce
# Open Source software that acts as one of the few bits of example
# code for this interface.

import errno
import socket
import time
from datetime import datetime, timedelta
import xml.dom.minidom
from xml.etree import ElementTree

import requests
from requests.auth import HTTPDigestAuth

import wsman


"""CIM schema urls

Conceptually you can query a Service, everything else is for update
only or modeling only. And, yes this is as redundant as it looks.
"""

SCHEMA_BASE = 'http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/'

CIM_AssociatedPowerManagementService = (SCHEMA_BASE +
                                        'CIM_AssociatedPowerManagementService')
CIM_PowerManagementService = (SCHEMA_BASE +
                              'CIM_PowerManagementService')
CIM_BootService = SCHEMA_BASE + 'CIM_BootService'

CIM_ComputerSystem = SCHEMA_BASE + 'CIM_ComputerSystem'
CIM_BootConfigSetting = SCHEMA_BASE + 'CIM_BootConfigSetting'
CIM_BootSourceSetting = SCHEMA_BASE + 'CIM_BootSourceSetting'

# Additional useful constants
_SOAP_ENVELOPE = 'http://www.w3.org/2003/05/soap-envelope'
_ADDRESS = 'http://schemas.xmlsoap.org/ws/2004/08/addressing'
_ANONYMOUS = 'http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous'
_WSMAN = 'http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd'


# magic ports to connect to
AMT_PROTOCOL_PORT_MAP = {
    'http': 16992,
    'https': 16993,
}


def pp_xml(body):
    """Pretty print format some XML so it's readable."""
    pretty = xml.dom.minidom.parseString(body)
    return pretty.toprettyxml(indent="  ")

class AMTTimeout(Exception):
    pass


class Client(object):
    """AMT client.

    Manage interactions with AMT host.
    """
    def __init__(self, address, password, username='admin', protocol='http'):
        port = AMT_PROTOCOL_PORT_MAP[protocol]
        path = '/wsman'
        self.uri = "%(protocol)s://%(address)s:%(port)s%(path)s" % {
            'address': address,
            'protocol': protocol,
            'port': port,
            'path': path}
        self.username = username
        self.password = password

    def _awaken_amt(self, timeout=16):
        """ `Awaken` the AMT processor.

        When a device is powered off, AMT seems to enter some sort of power
        saving mode in which it replies to SYN packets with RST packets until
        it is ready to accept the connection, which it then will accept. This
        method simply attempts to connect until a specified amount of time
        passes, or the connection is accepted.
        """

        start_time = datetime.now()
        end_time = start_time + timedelta(seconds=timeout)

        while datetime.now() < end_time:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    socket.create_connection((self.address, self.port),
                                             timeout=timeout)
                    return
            except socket.error as err:
                if err.errno == errno.ECONNREFUSED:
                    time.sleep(0.5)
                else:
                    raise

        raise AMTTimeout

    def post(self, payload, ns=None):
        self._awaken_amt()
        resp = requests.post(self.uri,
                             headers={'content-type':
                                      'application/soap+xml;charset=UTF-8'},
                             auth=HTTPDigestAuth(self.username, self.password),
                             data=payload)
        if resp.status_code == 200:
            if ns:
                rv = _return_value(resp.content, ns)
                if rv == 0:
                    return 0
                print(pp_xml(resp.content))
            else:
                return 0
        else:
            print("Status: %s" % resp.status_code)
            print(pp_xml(resp.content))
            return 1

    def power_on(self):
        """Power on the box."""
        payload = wsman.power_state_request(self.uri, "on")
        return self.post(payload, CIM_PowerManagementService)

    def power_off(self):
        """Power off the box."""
        payload = wsman.power_state_request(self.uri, "off")
        return self.post(payload, CIM_PowerManagementService)

    def power_cycle(self):
        """Power cycle the box."""
        payload = wsman.power_state_request(self.uri, "reboot")
        return self.post(payload, CIM_PowerManagementService)

    def pxe_next_boot(self):
        """Sets the machine to PXE boot on its next reboot

        Will default back to normal boot list on the reboot that follows.
        """
        self.set_next_boot(boot_device='pxe')

    def set_next_boot(self, boot_device):
        """Sets the machine to boot to boot_device on its next reboot

        Will default back to normal boot list on the reboot that follows.
        """
        payload = wsman.change_boot_order_request(self.uri, boot_device)
        self.post(payload)

        payload = wsman.enable_boot_config_request(self.uri)
        self.post(payload)

    def power_status(self):
        payload = wsman.get_request(
            self.uri,
            CIM_AssociatedPowerManagementService)
        self._awaken_amt()
        resp = requests.post(self.uri,
                             auth=HTTPDigestAuth(self.username, self.password),
                             data=payload)
        value = _find_value(
            resp.content,
            CIM_AssociatedPowerManagementService,
            "PowerState")
        return value

    def enable_vnc(self, password=None):
        """ Enable VNC connections via port 5900 on the managed host.

        RFB passwords must be *exactly* 8 characters long, as noted in the
        Intel KVM documentation here: https://goo.gl/eT0TAA

        Allow the user to specify an alternate RFB password via kwargs, or
        simply truncate the provided AMT password to that length, in the
        event it is longer.
        """

        if not password or len(password) != 8:
            password = self.password[:8]

        payload = wsman.enable_remote_kvm(self.uri, password)
        self.post(payload)
        payload = wsman.kvm_redirect(self.uri)
        self.post(payload)

    def vnc_status(self):
        payload = wsman.get_request(
            self.uri,
            ('http://intel.com/wbem/wscim/1/ips-schema/1/'
             'IPS_KVMRedirectionSettingData'))
        self._awaken_amt()
        resp = requests.post(self.uri,
                             auth=HTTPDigestAuth(self.username, self.password),
                             data=payload)
        return pp_xml(resp.content)


def _find_value(content, ns, key):
    """Find the return value in a CIM response.

    The xmlns is needed because everything in CIM is a million levels
    of namespace indirection.
    """
    doc = ElementTree.fromstring(content)
    query = './/{%(ns)s}%(item)s' % {'ns': ns, 'item': key}
    rv = doc.find(query)
    return rv.text


def _return_value(content, ns):
    """Find the return value in a CIM response.

    The xmlns is needed because everything in CIM is a million levels
    of namespace indirection.
    """
    doc = ElementTree.fromstring(content)
    query = './/{%(ns)s}%(item)s' % {'ns': ns, 'item': 'ReturnValue'}
    rv = doc.find(query)
    return int(rv.text)
