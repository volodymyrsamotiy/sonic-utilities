#!/usr/bin/env python

import os
import sys
import json
import syslog
from swsssdk import ConfigDBConnector
from swsssdk import SonicV2Connector


''' vnet_route_check.py: tool that verifies VNET routes consistancy between SONiC and vendor SDK DBs.

Logically VNET route verification logic consists of 3 parts:
1. Get VNET routes entries that are missed in ASIC_DB but present in APP_DB.
2. Get VNET routes entries that are missed in APP_DB but present in ASIC_DB.
3. Get VNET routes entries that are missed in SDK but present in ASIC_DB.

Returns 0 if there is no inconsistancy found and all VNET routes are aligned in all DBs.
Returns -1 if there is incosistancy found and prints differences between DBs in JSON format to standart output.

Format of differences output:
{
    "results": {
        "missed_in_asic_db_routes": {
            "<vnet_name>": {
                "routes": [
                    "<pfx>/<pfx_len>"
                ]
            }
        },
        "missed_in_app_db_routes": {
            "<vnet_name>": {
                "routes": [
                    "<pfx>/<pfx_len>"
                ]
            }
        },
        "missed_in_sdk_routes": {
            "<vnet_name>": {
                "routes": [
                    "<pfx>/<pfx_len>"
                ]
            }
        }
    }
}
'''


RC_OK = 0
RC_ERR = -1


report_level = syslog.LOG_ERR
write_to_syslog = True


def set_level(lvl, log_to_syslog):
    global report_level
    global write_to_syslog

    write_to_syslog = log_to_syslog
    report_level = lvl


def print_message(lvl, *args):
    if (lvl <= report_level):
        msg = ""
        for arg in args:
            msg += " " + str(arg)
        print(msg)
        if write_to_syslog:
            syslog.syslog(lvl, msg)


def get_vnet_intfs():
    ''' Returns dictionary of VNETs and related VNET interfaces.
    Format: { <vnet_name>: [ <vnet_rif_name> ] }
    '''
    config_db = ConfigDBConnector()
    config_db.connect('CONFIG_DB')

    intfs_data = config_db.get_table('INTERFACE')
    vlan_intfs_data = config_db.get_table('VLAN_INTERFACE')

    vnet_intfs = {}

    for intf_name, intf_attrs in intfs_data.items():
        if 'vnet_name' in intf_attrs:
            vnet_name = intf_attrs['vnet_name']
            if vnet_name in vnet_intfs:
                vnet_intfs[vnet_name].append(intf_name)
            else:
                vnet_intfs[vnet_name] = [intf_name]

    for intf_name, intf_attrs in vlan_intfs_data.items():
        if 'vnet_name' in intf_attrs:
            vnet_name = intf_attrs['vnet_name']
            if vnet_name in vnet_intfs:
                vnet_intfs[vnet_name].append(intf_name)
            else:
                vnet_intfs[vnet_name] = [intf_name]

    return vnet_intfs


def get_all_rifs_oids():
    ''' Returns dictionary of all router interfaces and their OIDs.
    Format: { <rif_name>: <rif_oid> }
    '''
    db = SonicV2Connector(host='127.0.0.1')
    db.connect(db.COUNTERS_DB)

    rif_name_oid_map = db.get_all(db.COUNTERS_DB, 'COUNTERS_RIF_NAME_MAP')

    return rif_name_oid_map


def get_vnet_rifs_oids():
    ''' Returns dictionary of VNET interfaces and their OIDs.
    Format: { <vnet_rif_name>: <vnet_rif_oid> }
    '''
    vnet_intfs = get_vnet_intfs()
    intfs_oids = get_all_rifs_oids()

    vnet_intfs = [vnet_intfs[k] for k in vnet_intfs]
    vnet_intfs = [val for sublist in vnet_intfs for val in sublist]

    vnet_rifs_oids_map = {}

    for intf_name in intfs_oids or {}:
        if intf_name in vnet_intfs:
            vnet_rifs_oids_map[intf_name] = intfs_oids[intf_name]

    return vnet_rifs_oids_map


def get_vrf_entries():
    ''' Returns dictionary of VNET interfaces and corresponding VRF OIDs.
    Format: { <vnet_rif_name>: <vrf_oid> }
    '''
    db = ConfigDBConnector()
    db.db_connect('ASIC_DB')

    vnet_rifs_oids = get_vnet_rifs_oids()

    rif_vrf_map = {}
    for vnet_rif_name in vnet_rifs_oids:
        rif_attrs = db.get_all(db.ASIC_DB, 'ASIC_STATE:SAI_OBJECT_TYPE_ROUTER_INTERFACE:{}'.format(vnet_rifs_oids[vnet_rif_name]))
        rif_vrf_map[vnet_rif_name] = rif_attrs['SAI_ROUTER_INTERFACE_ATTR_VIRTUAL_ROUTER_ID']

    return rif_vrf_map


def filter_out_vnet_ip2me_routes(vnet_routes):
    ''' Filters out IP2ME routes from the provided dictionary with VNET routes
    Format: { <vnet_name>: { 'routes': [ <pfx/pfx_len> ], 'vrf_oid': <oid> } }
    '''
    db = ConfigDBConnector()
    db.db_connect('APPL_DB')

    vnet_intfs = get_vnet_intfs()
    all_rifs_db_keys = db.get_keys('INTF_TABLE')

    vnet_intfs = [vnet_intfs[k] for k in vnet_intfs]
    vnet_intfs = [val for sublist in vnet_intfs for val in sublist]

    vnet_ip2me_routes = []
    for rif in all_rifs_db_keys:
        rif_attrs = rif.split(':')
        # Skip RIF entries without IP prefix and prefix length (they have only one attribute - RIF name)
        if len(rif_attrs) == 1:
            continue

        # rif_attrs[0] - RIF name
        # rif_attrs[1] - IP prefix and prefix legth
        # IP2ME routes have '/32' prefix length so replace it and add to the list
        if rif_attrs[0] in vnet_intfs:
            vnet_ip2me_routes.append(rif_attrs[1].replace('/24', '/32'))

    for vnet, vnet_attrs in vnet_routes.items():
        for route in vnet_attrs['routes']:
            if route in vnet_ip2me_routes:
                vnet_attrs['routes'].remove(route)

        if not vnet_attrs['routes']:
            vnet_routes.pop(vnet)


def get_vnet_routes_from_app_db():
    ''' Returns dictionary of VNET routes configured per each VNET in APP_DB.
    Format: { <vnet_name>: { 'routes': [ <pfx/pfx_len> ], 'vrf_oid': <oid> } }
    '''
    db = ConfigDBConnector()
    db.db_connect('APPL_DB')

    vnet_intfs = get_vnet_intfs()
    vnet_vrfs = get_vrf_entries()

    vnet_routes_db_keys = db.get_keys('VNET_ROUTE_TABLE') + db.get_keys('VNET_ROUTE_TUNNEL_TABLE')

    vnet_routes = {}

    for vnet_route_db_key in vnet_routes_db_keys:
        vnet_route_list = vnet_route_db_key.split(':')
        vnet_name = vnet_route_list[0]
        vnet_route = vnet_route_list[1]

        if vnet_name not in vnet_routes:
            vnet_routes[vnet_name] = {}
            vnet_routes[vnet_name]['routes'] = []

            intf = vnet_intfs[vnet_name][0]
            vnet_routes[vnet_name]['vrf_oid'] = vnet_vrfs.get(intf, 'None')

        vnet_routes[vnet_name]['routes'].append(vnet_route)

    return vnet_routes


def get_vnet_routes_from_asic_db():
    ''' Returns dictionary of VNET routes configured per each VNET in ASIC_DB.
    Format: { <vnet_name>: { 'routes': [ <pfx/pfx_len> ], 'vrf_oid': <oid> } }
    '''
    db = ConfigDBConnector()
    db.db_connect('ASIC_DB')

    vnet_vrfs = get_vrf_entries()
    vnet_vrfs_oids = [vnet_vrfs[k] for k in vnet_vrfs]
    
    vnet_intfs = get_vnet_intfs()

    vrf_oid_to_vnet_map = {}

    for vnet_name, vnet_rifs in vnet_intfs.items():
        for vnet_rif, vrf_oid in vnet_vrfs.items():
            if vnet_rif in vnet_rifs:
                vrf_oid_to_vnet_map[vrf_oid] = vnet_name

    routes_db_keys = db.get_keys('ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY', False)   
    vnet_routes = {}

    for route_db_key in routes_db_keys:
        route_attrs = route_db_key.lower().split('\"', -1)
        # route_attrs[11] - VRF OID for the VNET route
        # route_attrs[3] - VNET route IP subnet
        vrf_oid = route_attrs[11]
        ip_addr = route_attrs[3]

        if vrf_oid in vnet_vrfs_oids:
            if vrf_oid_to_vnet_map[vrf_oid] not in vnet_routes:
                vnet_name = vrf_oid_to_vnet_map[vrf_oid]

                vnet_routes[vnet_name] = {}
                vnet_routes[vnet_name]['routes'] = []
                vnet_routes[vnet_name]['vrf_oid'] = vrf_oid

            vnet_routes[vnet_name]['routes'].append(ip_addr)

    filter_out_vnet_ip2me_routes(vnet_routes)

    return vnet_routes


def get_vnet_routes_diff(routes_1, routes_2):
    ''' Returns all routes present in routes_2 dictionary but missed in routes_1
    Format: { <vnet_name>: { 'routes': [ <pfx/pfx_len> ] } }
    '''

    routes = {}

    for vnet_name, vnet_attrs in routes_2.items():
        if vnet_name not in routes_1:
            routes[vnet_name] = routes
        else:
            for vnet_route in vnet_attrs['routes']:
                if vnet_route not in routes_1[vnet_name]['routes']:
                    if vnet_name not in routes:
                        routes[vnet_name] = {}
                        routes[vnet_name]['routes'] = []
                    routes[vnet_name]['routes'].append(vnet_route)

    return routes


def get_sdk_vnet_routes_diff(routes):
    ''' Returns all routes present in routes dictionary but missed in SAI/SDK
    Format: { <vnet_name>: { 'routes': [ <pfx/pfx_len> ], 'vrf_oid': <oid> } }
    '''
    routes_diff = {}

    res = os.system('docker exec syncd test -f /usr/bin/vnet_route_check.py')
    if res != 0:
        return routes_diff

    for vnet_name, vnet_routes in routes.items():
        vnet_routes = routes[vnet_name]["routes"]
        vnet_vrf_oid = routes[vnet_name]["vrf_oid"]

        res = os.system('docker exec syncd "/usr/bin/vnet_route_check.py {} {}"'.format(vnet_vrf_oid, vnet_routes))
        if res:
            routes_diff[vnet_name] = {}
            routes_diff[vnet_name]['routes'] = res

    return routes_diff


def main():

    app_db_vnet_routes = get_vnet_routes_from_app_db()
    asic_db_vnet_routes = get_vnet_routes_from_asic_db()

    missed_in_asic_db_routes = get_vnet_routes_diff(asic_db_vnet_routes, app_db_vnet_routes)
    missed_in_app_db_routes = get_vnet_routes_diff(app_db_vnet_routes, asic_db_vnet_routes)
    missed_in_sdk_routes = get_sdk_vnet_routes_diff(asic_db_vnet_routes)

    res = {}
    res['results'] = {}
    rc = RC_OK

    if missed_in_asic_db_routes:
        res['results']['missed_in_asic_db_routes'] = missed_in_asic_db_routes

    if missed_in_app_db_routes:
        res['results']['missed_in_app_db_routes'] = missed_in_app_db_routes

    if missed_in_sdk_routes:
        res['results']['missed_in_sdk_routes'] = missed_in_sdk_routes

    if res['results']:
        rc = RC_ERR
        print_message(syslog.LOG_ERR, json.dumps(res, indent=4))
        print_message(syslog.LOG_ERR, 'Vnet Route Mismatch reported')

    sys.exit(rc)


if __name__ == "__main__":
    main()