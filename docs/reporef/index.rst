Repository Reference
====================

Templates
---------

Templates for switch configurations.

In the base of this repository there should be one directory for each network operating system
platform, like "eos", "junos" or "iosxr".

In each of these directories there needs to be a file called "mapping.yml", this file defines
what template files should be used for each device type. For example, in mapping.yml there
might be a definition of templates for an access switch specified like this:

::

    ACCESS:
        entrypoint: access.j2
        dependencies:
            - managed-full.j2


This indicates that the starting point for the template of access switches for this platform
is deffined in the Jinja2 template file called "access.j2". Additionally, this template file
will depend on things defined in a file called "managed-full.j2".

The template files themselves are written using the Jinja2 templating language. Variables
that are exposed from CNaaS includes:

- mgmt_ip: IPv4 management address (ex 192.168.0.10)

- mgmt_ipif: IPv4 management address including prefix length (ex 192.168.0.10/24)

- mgmt_prefixlen: Just the prefix length (ex 24)

- mgmt_vlan_id: VLAN id for management (ex 10)

- mgmt_gw: IPv4 address for the default gateway in the management network

- uplinks: A list of uplink interfaces, each interface is a dictionary with these keys:

  * ifname: Name of the physical interface (ex Ethernet1)

- access_auto: A list of access_auto interfacs. Using same keys as uplinks.

Additional variables available for distribution switches:

- infra_ip: IPv4 infrastructure VRF address (ex 10.199.0.0)

- infra_ipif: IPv4 infrastructure VRF address inc prefix (ex 10.199.0.0/32)

- vrfs: A list of dictionaries with two keys: "name" and "rd" (rd as in Route Distinguisher).
  Populated from settings defined in routing.yml.

- bgp_ipv4_peers: A list of dictionaries with the keys: "peer_hostname", "peer_infra_lo", "peer_ip" and "peer_asn".
  Contains one entry per directly connected dist/core device, used to build an eBGP underlay for the fabric.
  Populated from the links database table.

- bgp_evpn_peers: A list of dictionaries with the keys: "peer_hostname", "peer_infra_lo", "peer_asn".
  Contains one entry per hostname specified in settings->evpn_spines. Used to build
  eBGP peering for EVPN between loopbacks.

- mgmtdomains: A list of dictionaries with the keys: "ipv4_gw", "vlan", "description", "esi_mac".
  Populated from the mgmtdomains database table.

- asn: A private unique Autonomous System number generated from the last two octets
  of the infra_lo IP address on the device.
 
All settings configured in the settings repository are also exposed to the templates.

settings
--------

Settings are defined at different levels and inherited (possibly overridden) in several steps.
For example, NTP servers might be defined in the "global" settings to impact the entire
managed network, but then overridden for a specific device type that needs custom NTP servers.
The inheritence is defined in these steps: Global -> Core/Dist/Access -> Device specific.
The directory structure looks like this:

- global

  * groups.yml: Definition of custom device groups
  * vxlans.yml: Definition of VXLAN/VLANs
  * routing.yml: Definition of global routing settings like fabric underlay and VRFs
  * base_system.yml: Base system settings

- core

  * base_system.yml: Base system settings

- dist

  * base_system.yml: Base system settings

- access:

  * base_system.yml: Base system settings

- devices:

  * <hostname>

    + base_system.yml
    + interfaces.yml

routing.yml:

- underlay:

  * infra_link_net: A /16 of IPv4 addresses that CNaaS-NMS can use to automatically assign
    addresses for infrastructure links from (ex /31 between dist-core).
  * infra_lo_net: A /16 of IPv4 addresses that CNaaS-NMS can use to automatically assign
    addresses for infrastructure loopback interfaces from.

- evpn_spines:

  * hostname: A hostname of a CORE (or DIST) device from the device database.
    The other DIST switches participating in the VXLAN/EVPN fabric will establish
    eBGP connections to these devices.

- vrfs:

  * name: The name of the VRF. Should be one word (no spaces).
  * vrf_id: An integer between 1-65535. This ID can be used to generate unique VNI, RD and RT
    values for this VRF.
  * groups: A list of groups this VRF should be provisioned on.



etc
---

Configuration files for system daemons

Directory structure:

- dhcpd/

  * dhcpd.conf: Used for ZTP DHCPd
