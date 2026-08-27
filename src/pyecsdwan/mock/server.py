"""Fake Orchestrator: FastAPI app factory, in-memory state, and a thread runner.

Behavior mirrors the real Orchestrator 9.3+ endpoints as documented in the
vendored pyedgeconnect SDK docstrings. Every handler is tolerant: unknown
body fields pass through into state untouched, and responses come straight
from state (never remodeled), matching the pass-through philosophy of
``pyecsdwan.client``.

All routes are served under ``/gms/rest``. Authentication accepts either an
``X-Auth-Token`` header with any non-empty value, or a session cookie issued
by ``POST /gms/rest/authentication/login``. Set ``MockState.require_auth``
to ``False`` to disable the check entirely (useful in tests).
"""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from typing import Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

_SESSION_COOKIE = "pyecsdwanMockSession"


# -- seed data ---------------------------------------------------------------


def _seed_appliances() -> list[dict[str, Any]]:
    return [
        {
            "nePk": "1.NE",
            "id": "1.NE",
            "hostName": "HUB1-EC",
            "site": "HQ",
            "model": "EC-S",
            "state": 1,
            "reachabilityChannel": 2,
            "hasUnsavedChanges": False,
            "rebootRequired": False,
        },
        {
            "nePk": "3.NE",
            "id": "3.NE",
            "hostName": "BR1-EC",
            "site": "Branch-1",
            "model": "EC-S",
            "state": 1,
            "reachabilityChannel": 2,
            "hasUnsavedChanges": False,
            "rebootRequired": False,
        },
        {
            "nePk": "5.NE",
            "id": "5.NE",
            "hostName": "BR2-EC",
            "site": "Branch-2",
            "model": "EC-S",
            "state": 1,
            "reachabilityChannel": 2,
            "hasUnsavedChanges": False,
            "rebootRequired": False,
        },
    ]


def _seed_interface_labels() -> dict[str, Any]:
    return {
        "wan": {
            "1": {"name": "MPLS1", "active": True, "topology": 0},
            "2": {"name": "INET1", "active": True, "topology": 0},
        },
        "lan": {
            "4": {"name": "Voice", "active": True, "topology": 0},
            "5": {"name": "Data", "active": True, "topology": 0},
        },
    }


def _seed_overlays() -> dict[str, dict[str, Any]]:
    return {"1": {"id": 1, "name": "CorpFabric", "modifiedTime": 0}}


def _seed_overlay_association() -> dict[str, list[str]]:
    return {"1": ["1.NE"]}


def _seed_security_maps() -> dict[str, Any]:
    return {"mapsByVrf": {"0": {"zoneMap": {}}}}


def _seed_zones() -> dict[str, Any]:
    return {"0": {"name": "Default"}}


def _seed_vrf_zones_map() -> dict[str, Any]:
    return {"0": {"0": {"id": 0, "name": "Default"}}}


def _seed_zone_list_meta() -> dict[str, Any]:
    return {
        "1.NE": {"zones": ["Default"]},
        "3.NE": {"zones": ["Default"]},
        "5.NE": {"zones": ["Default"]},
    }


# -- bgp (#16, Stage 2) --------------------------------------------------------
#
# Rides the generic /appliance/rest proxy (appliance_ecos store) like vrrp,
# routes, zones, and security-maps — Stage 2 switched bgp.py's fetch()/
# apply() off the dedicated orchestrator-level routes this block used to
# back, onto the same proxy channel every other appliance-scope resource
# reads and writes through. Shapes are the two real payloads captured live
# this session (see resources/bgp.py module docstring): most appliances
# disabled/unconfigured, one enabled with a real neighbor.


def _seed_bgp_system() -> dict[str, dict[str, Any]]:
    disabled = {
        "stale_path_time": 150,
        "enable_gms_marked": False,
        "enable": False,
        "remote_as_path_advertise": False,
        "log_nbr_msgs": True,
        "rtr_id": "0.0.0.0",
        "asn": 65534,
        "max_restart_time": 120,
        "graceful_restart_en": False,
    }
    enabled = {
        "stale_path_time": 150,
        "enable_gms_marked": False,
        "enable": True,
        "remote_as_path_advertise": True,
        "log_nbr_msgs": True,
        "route_target": {"0": {"self": 0, "export": "0:0", "import": "0:0"}},
        "rtr_id": "192.168.255.13",
        "asn": 65534,
        "max_restart_time": 120,
        "graceful_restart_en": False,
    }
    return {"1.NE": dict(disabled), "3.NE": dict(enabled), "5.NE": dict(disabled)}


def _seed_bgp_neighbor() -> dict[str, dict[str, Any]]:
    populated = {
        "10.127.1.1": {
            "as_override": False,
            "bfd_desired": False,
            "directly_connected": False,
            "enable": True,
            "evpn": False,
            "gms_marked": False,
            "hold": 9,
            "ka": 6,
            "lcl_interface": "any",
            "next_hop_self": True,
            "password": "",
            "remote_as": 65001,
            "rtmap_inbound": "default_rtmap_bgp_inbound_br",
            "rtmap_outbound": "default_rtmap_bgp_outbound_br",
            "store_received_routes": True,
            "type": "Branch",
        }
    }
    return {"1.NE": {}, "3.NE": dict(populated), "5.NE": {}}


# -- end bgp (#16) seed data -----------------------------------------------------


# -- ospf (#17, Stage 2) --------------------------------------------------------
#
# Same Stage 2 migration as bgp above — rides the generic appliance_ecos
# proxy store now, not a dedicated orchestrator-level route.


def _seed_ospf_system() -> dict[str, dict[str, Any]]:
    disabled = {
        "redistMapToOSPF": "default_rtmap_to_ospf",
        "enable": False,
        "routerId": "0.0.0.0",
        "opaque_enable": True,
    }
    disabled_with_router_id = {
        "redistMapToOSPF": "default_rtmap_to_ospf",
        "enable": False,
        "routerId": "192.168.255.13",
        "opaque_enable": True,
    }
    return {
        "1.NE": dict(disabled),
        "3.NE": dict(disabled_with_router_id),
        "5.NE": dict(disabled),
    }


def _seed_ospf_interfaces() -> dict[str, dict[str, Any]]:
    populated = {
        "lan0": {
            "cost": 1,
            "area": "0.0.0.0",
            "authKey": "",
            "md5Password": "",
            "authType": "None",
            "comment": "",
            "priority": 1,
            "transmitDelay": 1,
            "retransmitInterval": 4,
            "helloInterval": 10,
            "deadInterval": 40,
            "md5Key": 0,
            "adminStatus": True,
            "bfdDesired": False,
        },
        "lan1": {
            "cost": 1,
            "area": "1.0.0.0",
            "authKey": "",
            "md5Password": "",
            "authType": "None",
            "comment": "",
            "priority": 1,
            "transmitDelay": 1,
            "retransmitInterval": 4,
            "helloInterval": 10,
            "deadInterval": 40,
            "md5Key": 0,
            "adminStatus": True,
            "bfdDesired": False,
        },
    }
    return {"1.NE": {}, "3.NE": dict(populated), "5.NE": {}}


# -- end ospf (#17) seed data -----------------------------------------------------


# -- deployment (#12) ---------------------------------------------------------
#
# Genericized capture of a real GET /appliance/rest?...&url=deployment
# response against a lab Orchestrator this session (docs/research/
# appliance-config.md + resources/deployment.py docstring). Six interfaces,
# one running a DHCP server, matching the sampled appliance's shape.


def _seed_deployment() -> dict[str, Any]:
    def _ip(
        devnum_role: str,
        ip: str,
        mask: str,
        *,
        label: str,
        lan_side: bool,
        wan_side: bool,
        wan_nexthop: str = "",
        zone: int = 0,
        dhcpd: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "ip": ip,
            "mask": mask,
            "wanNexthop": wan_nexthop,
            "dhcp": False,
            "lanSide": lan_side,
            "wanSide": wan_side,
            "label": label,
            "harden": 0,
            "behindNAT": False,
            "maxBW": {"inbound": 0, "outbound": 0},
            "zone": zone,
            "comment": "",
            "vrf": 0,
            "role": devnum_role,
            "proxy_arp": False,
            "dhcpd": dhcpd
            if dhcpd is not None
            else {"type": "none", "server": {}, "relay": {}},
        }

    return {
        # Read-only hardware/license limits — Deployment.normalize() strips
        # this key entirely; kept here only so the mock's GET response
        # matches the real appliance shape byte-for-byte.
        "scalars": {
            "maxWanBandwidth": 1000000,
            "defaultMaxWanBandwidth": 1000000,
            "maxRxTargetBandwidth": 1000000,
            "maxTunnels": 500,
            "maxIKETunnels": 500,
            "minMtu": 576,
            "maxMtu": 9216,
            "maxRouteMapEntries": 512,
            "maxOptMapEntries": 256,
            "maxQoSMapEntries": 256,
            "maxNatMapEntries": 512,
            "isPortalLicensed": True,
            "portalLicenseType": "boost",
            "supportServerMode": True,
            "isLicenseRequired": False,
            "isDynamicLimits": False,
            "isDynamicInterface": True,
            "isModel4Port": False,
            "isModel10G": False,
            "isModelSingleDisk": True,
            "isModelPowerCycle": False,
            "num1GigPorts": 8,
            "num1GigFiberPorts": 0,
            "numMgmtPorts": 1,
            "num10GigPorts": 0,
        },
        "sysConfig": {
            "mode": "router",
            "useMgmt0": True,
            "tenG": False,
            "bonding": False,
            "maxBW": 100000,
            "propagateLinkDown": False,
            "singleBridge": False,
            "inline": False,
            "vrfEnable": False,
            "serverPerSegment": False,
            "ifLabels": {"lan": ["Data", "Voice"], "wan": ["MPLS1", "INET1"]},
            "haIf": "",
            "zones": [],
            "vrfs": [],
            "roles": [],
            "vrfZonesMap": {},
            "maxInBW": 100000,
            "maxInSysBW": 100000,
            "maxObSysBW": 100000,
            "license": "boost",
        },
        "mgmtIfData": {
            "mgmt0": {
                "dhcp": False,
                "ip": "10.0.0.10",
                "mask": "255.255.255.0",
                "nexthop": "10.0.0.1",
            },
        },
        "modeIfs": [
            {
                "devNum": "rtr1",
                "ifName": "lan0",
                "applianceIPs": [
                    _ip(
                        "lan",
                        "10.1.1.1",
                        "255.255.255.0",
                        label="Data",
                        lan_side=True,
                        wan_side=False,
                        zone=0,
                    )
                ],
            },
            {
                "devNum": "rtr1",
                "ifName": "lan1",
                "applianceIPs": [
                    _ip(
                        "lan",
                        "10.1.2.1",
                        "255.255.255.0",
                        label="Voice",
                        lan_side=True,
                        wan_side=False,
                        zone=0,
                        dhcpd={
                            "type": "server",
                            "server": {
                                "prefix": "10.1.2.0/24",
                                "ipStart": "10.1.2.100",
                                "ipEnd": "10.1.2.200",
                                "gw": ["10.1.2.1"],
                                "dns": ["10.1.2.1"],
                                "ntpd": [],
                                "netbios": [],
                                "netbiosNodeType": 0,
                                "maxLease": 86400,
                                "defaultLease": 43200,
                                "ip_range": {},
                                "options": {},
                                "host": {},
                                "failover": "",
                            },
                            "relay": {},
                        },
                    )
                ],
            },
            {
                "devNum": "rtr1",
                "ifName": "wan0",
                "applianceIPs": [
                    _ip(
                        "wan",
                        "203.0.113.10",
                        "255.255.255.0",
                        label="MPLS1",
                        lan_side=False,
                        wan_side=True,
                        wan_nexthop="203.0.113.1",
                        zone=0,
                    )
                ],
            },
            {
                "devNum": "rtr1",
                "ifName": "wan1",
                "applianceIPs": [
                    _ip(
                        "wan",
                        "198.51.100.10",
                        "255.255.255.0",
                        label="INET1",
                        lan_side=False,
                        wan_side=True,
                        wan_nexthop="198.51.100.1",
                        zone=0,
                    )
                ],
            },
            {"devNum": "rtr1", "ifName": "wan2", "applianceIPs": []},
            {"devNum": "rtr1", "ifName": "lan2", "applianceIPs": []},
        ],
        "dpRoutes": [],
        # Real top-level key (issue text didn't mention it) — pass-through
        # unknown config, never dropped by Deployment.normalize().
        "vifs": {"pppoe": [], "bondedIfs": []},
        "dhcpFailover": {},
    }


def _validate_deployment(body: Any, force_fail: bool) -> dict[str, Any]:
    """Minimal mock of ``deployment/validate``: ``{err, rebootRequired}``.

    ``force_fail`` lets a test arm one deterministic failure (mirrors
    ``fail_next_action``); otherwise a body missing a mask for a configured
    IP is the one condition rejected, so the happy path is always empty.
    """
    if force_fail:
        return {
            "err": "mock validation failure: conflicting IP allocation",
            "rebootRequired": False,
        }
    if isinstance(body, dict):
        for iface in body.get("modeIfs") or []:
            if not isinstance(iface, dict):
                continue
            for entry in iface.get("applianceIPs") or []:
                if isinstance(entry, dict) and entry.get("ip") and not entry.get("mask"):
                    return {
                        "err": (
                            f"{iface.get('devNum')}.{iface.get('ifName')}: "
                            f"ip {entry.get('ip')} missing mask"
                        ),
                        "rebootRequired": False,
                    }
    return {"err": "", "rebootRequired": False}


# -- loopback (#18) ------------------------------------------------------------
#
# 1. Per-appliance loopback interfaces: served through the generic
#    /appliance/rest proxy's opaque KV store (like vrrp, #14) — seeded via
#    _seed_appliance_ecos() below, key "virtualif/loopback". Real shape
#    captured live this session (resources/loopback.py module docstring).
# 2. Loopback orchestration: its own top-level state (fabric-wide, not
#    per-appliance), with real add/reclaim semantics for the pool endpoints
#    so a resource's GET-then-POST full-structure-replace round-trips.

_LOOPBACK_ORCH_PATH = "/loopbackOrch"
_LOOPBACK_ORCH_POOL_PATH = "/loopbackOrch/pool"
_LOOPBACK_ORCH_RECLAIM_PATH = "/loopbackOrch/pool/reclaim"


def _seed_loopback_interfaces() -> dict[str, dict[str, Any]]:
    """Per-appliance ``virtualif/loopback`` fixture — real shape captured
    live this session against a lab Orchestrator (see resources/loopback.py).
    Only HUB1-EC (1.NE) has a loopback configured; the others come back
    empty, same "not every appliance has one" spread as vrrp/bgp-neighbor."""
    return {
        "1.NE": {
            "lo0": {
                "admin": True,
                "gms_marked": False,
                "ipaddr": "192.168.255.12",
                "label": "",
                "nmask": 32,
                "role_id": 0,
                "vrf_id": 0,
                "zone": 0,
            }
        },
        "3.NE": {},
        "5.NE": {},
    }


def _seed_loopback_orch() -> dict[str, Any]:
    """``loopbackOrch`` fixture. Not live-confirmed (the lab fabric's
    ``loopbackOrch`` came back empty this session) — shaped from the issue
    text + the vendored SDK's ``get_loopback_orchestration`` docstring:
    ``{segmentId: {loopbackPool, interfaces: {interfaceId: {mgmtIP, label,
    zone}}}}``. Deliberately seeded with the real GET casing (``mgmtIP``) so
    the mgmtIp/mgmtIP bug is exercised by tests that *submit* the alias, not
    silently masked by the fixture itself using the buggy casing too."""
    return {
        "0": {
            "loopbackPool": "10.41.0.0/16",
            "interfaces": {
                "20000": {"mgmtIP": True, "label": "149", "zone": 27},
            },
        }
    }


def _seed_loopback_orch_pool() -> dict[str, Any]:
    return {
        "0": {
            "segment": 0,
            "subnet": "10.41.0.0/16",
            "totalAddr": 65534,
            "addrAllocated": 1,
            "addrDeleted": 0,
        }
    }


# -- end loopback (#18) seed data -----------------------------------------------


# -- static routes (#15) ------------------------------------------------------
#
# Per-appliance ECOS state for the subnets3/* static-route endpoints, proxied
# through /appliance/rest (see _subnets3_dispatch below). Shape matches the
# real payload captured live against subnets3/configured (issue #15).

_SUBNETS_ALL_PATH = "subnets3/all"
_SUBNETS_CONFIGURED_PATH = "subnets3/configured"
_SUBNETS_ADD_PATH = "subnets3/configured/addMultiple"
_SUBNETS_DELETE_PATH = "subnets3/configured/deleteMultiple"


def _seed_static_routes() -> dict[str, dict[str, Any]]:
    default_route = {
        "prefix": {
            "0.0.0.0/0": {
                "self": "0.0.0.0/0",
                "advert": True,
                "advert_bgp": False,
                "advert_ospf": False,
                "local": True,
                "nhop": {
                    "0.0.0.0": {
                        "self": "0.0.0.0",
                        "interface": {
                            "default": {
                                "self": "default",
                                "comment": "Default route",
                                "dest_mac": "00:00:00:00:00:00",
                                "dir": "ANY",
                                "gms_marked": False,
                                "label": 1,
                                "metric": 50,
                                "no_subshared": False,
                                "vni": 16777216,
                                "vxlan": False,
                                "zone_id": 65534,
                            }
                        },
                    }
                },
            }
        }
    }
    return {ne_pk: copy.deepcopy(default_route) for ne_pk in ("1.NE", "3.NE", "5.NE")}


def _seed_static_routes_learned() -> dict[str, dict[str, Any]]:
    """Extra prefixes visible only via subnets3/all — simulated OSPF-learned
    routes, never mutated by addMultiple/deleteMultiple."""
    learned = {
        "prefix": {
            "10.99.0.0/24": {
                "self": "10.99.0.0/24",
                "advert": False,
                "advert_bgp": False,
                "advert_ospf": True,
                "local": False,
                "nhop": {
                    "10.1.1.1": {
                        "self": "10.1.1.1",
                        "interface": {
                            "wan0": {
                                "self": "wan0",
                                "comment": "",
                                "dest_mac": "aa:bb:cc:00:11:22",
                                "dir": "ANY",
                                "gms_marked": False,
                                "label": 2,
                                "metric": 110,
                                "no_subshared": False,
                                "vni": 16777216,
                                "vxlan": False,
                                "zone_id": 65534,
                            }
                        },
                    }
                },
            }
        }
    }
    return {ne_pk: copy.deepcopy(learned) for ne_pk in ("1.NE", "3.NE", "5.NE")}


# -- vrrp (#14) ----------------------------------------------------------


def _seed_vrrp() -> dict[str, list[dict[str, Any]]]:
    """Realistic ``vrrp`` ECOS fixture: a two-appliance HA pair peered over
    wan0 (1.NE master, 3.NE backup, groupId 1), with each entry's server-
    reported fields (mode/master_transitions/uptime/vmac/...) present too so
    a resource's normalize() is exercised stripping them.

    5.NE is left with no entries: a live-appliance probe this dev cycle
    found the ``vrrp`` endpoint reachable but returned no VRRP config on any
    appliance it was run against, so "nothing configured" is the one shape
    actually confirmed live — worth keeping represented here.
    """
    common = {
        "pkt_trace": False,
        "adv_timer": 1,
        "preempt": True,
        "holddown": 10,
        "auth": "",
        "desc": "HQ-Branch1 HA",
        "interface": "wan0",
        "vipaddr": "10.0.0.1",
        "groupId": 1,
    }
    master = {
        **common,
        "enable": "Up",
        "priority": 200,
        "mode": "Master",
        "master_transitions": 3,
        "uptime": "12 days 4 hrs 0 mins 0 secs",
        "vmac": "00-00-5E-00-01-01",
    }
    backup = {
        **common,
        "enable": "Up",
        "priority": 100,
        "mode": "Backup",
        "master_transitions": 3,
        "uptime": "12 days 3 hrs 58 mins 12 secs",
        "vmac": "00-00-5E-00-01-01",
    }
    return {"1.NE": [master], "3.NE": [backup], "5.NE": []}


# -- appliance zones/security-maps (#19) -------------------------------------
#
# Both ECOS paths ("zones", "securityMaps") are served through the generic
# appliance-proxy handler below, same as vrrp (#14) — no dedicated route is
# needed, only realistic seed data in the per-appliance ECOS store.


def _seed_appliance_zones() -> dict[str, dict[str, Any]]:
    """Appliance-scope zone table, ECOS path 'zones'. Real captured shape
    (#19, read-only against a live lab Orchestrator): {"1": {"name":
    "Untrust"}} — note no id-0 Default row, unlike the orchestrator-scope
    zones table (mock.zones above); that server-managed row is a genuine
    difference at this scope, not an omission here."""
    return {
        "1.NE": {"1": {"name": "Untrust"}, "2": {"name": "DMZ"}},
        "3.NE": {"1": {"name": "Untrust"}},
        "5.NE": {},
    }


def _seed_appliance_security_maps() -> dict[str, dict[str, Any]]:
    """Per-appliance security map table, ECOS path 'securityMaps'. Real
    captured shape (#19, read-only against a live lab Orchestrator):
    map -> "<fromZoneId>_<toZoneId>" -> "prio" -> priority -> rule
    {comment, gms_marked, match, set, misc}."""
    rules = {
        "map1": {
            "0_1": {
                "prio": {
                    "20000": {
                        "comment": "",
                        "gms_marked": True,
                        "match": {"acl": "", "webcc_cat": "11|27|62|31|59|55|67|86"},
                        "misc": {
                            "tag": "tke",
                            "rule": "enable",
                            "logging_priority": "2",
                            "logging": "enable",
                        },
                        "set": {"action": "deny"},
                    },
                    "24999": {
                        "comment": "",
                        "gms_marked": True,
                        "match": {"acl": "", "internet": "1"},
                        "misc": {
                            "rule": "enable",
                            "logging_priority": "2",
                            "logging": "enable",
                        },
                        "set": {"action": "inspect"},
                    },
                    "65535": {
                        "comment": "",
                        "gms_marked": True,
                        "match": {"acl": ""},
                        "misc": {
                            "rule": "enable",
                            "logging_priority": "2",
                            "logging": "enable",
                        },
                        "set": {"action": "deny"},
                    },
                }
            }
        }
    }
    return {"1.NE": copy.deepcopy(rules), "3.NE": {}, "5.NE": {}}


# -- end appliance zones/security-maps (#19) seed data ------------------------


# -- policy maps / shapers (#33) ----------------------------------------------
#
# Five ECOS paths ("qosMaps", "optimizationMaps", "routeMaps", "shapers",
# "inboundShapers") served through the generic appliance-proxy handler below,
# same as vrrp (#14) and zones/securityMaps (#19) — no dedicated routes are
# needed, only realistic seed data in the per-appliance ECOS store.
#
# Two distinct shapes live in this block (see resources/policy_maps.py and
# resources/shapers.py): the three *maps* share the data/options envelope
# (and its activeMap directive), while the two *shapers* are bare
# interface-keyed tables with no envelope at all. The live traffic-class
# subtree is ~34KB; it is trimmed here to classes 1-3 on a few interfaces,
# which is representative of the shape without the bulk.


def _seed_policy_map_envelope(active_map: str, maps: dict[str, Any]) -> dict[str, Any]:
    """The data/options envelope all three policy-map endpoints return.

    Real captured shape (#33, read-only against a live lab Orchestrator):
    {"options": {"merge": ..., "activeMap": ..., "templateApply": ...},
     "data": {mapName: {"prio": {priority: rule}}}}.
    """
    return {
        "options": {"merge": True, "activeMap": active_map, "templateApply": False},
        "data": copy.deepcopy(maps),
    }


def _seed_qos_maps() -> dict[str, dict[str, Any]]:
    maps = {
        "map1": {
            "self": "map1",
            "prio": {
                "1000": {
                    "self": 1000,
                    "comment": "voice",
                    "match": {"acl": "", "app": "sip"},
                    "set": {"traffic_class": 1, "lan_qos": "trust-lan", "wan_qos": "trust-lan"},
                },
                "65535": {
                    "self": 65535,
                    "comment": "",
                    "match": {"acl": ""},
                    "set": {"traffic_class": 2, "lan_qos": "trust-lan"},
                },
            },
        }
    }
    return {
        "1.NE": _seed_policy_map_envelope("map1", maps),
        # 3.NE carries a second, non-active map so activeMap has something to
        # actually select between.
        "3.NE": _seed_policy_map_envelope(
            "map1",
            {
                **copy.deepcopy(maps),
                "afterhours": {
                    "self": "afterhours",
                    "prio": {
                        "65535": {
                            "self": 65535,
                            "comment": "",
                            "match": {"acl": ""},
                            "set": {"traffic_class": 4},
                        }
                    },
                },
            },
        ),
        "5.NE": {},
    }


def _seed_optimization_maps() -> dict[str, dict[str, Any]]:
    maps = {
        "map1": {
            "self": "map1",
            "prio": {
                "65535": {
                    "self": 65535,
                    "comment": "",
                    "match": {"acl": ""},
                    "set": {"boost": "disable", "tcp_accel": "enable", "net_mem": "enable"},
                }
            },
        }
    }
    return {"1.NE": _seed_policy_map_envelope("map1", maps), "3.NE": {}, "5.NE": {}}


def _seed_route_maps() -> dict[str, dict[str, Any]]:
    maps = {
        "map1": {
            "self": "map1",
            "prio": {
                "65535": {
                    "self": 65535,
                    "comment": "",
                    "gms_marked": True,
                    "match": {"acl": ""},
                    "set": {"action": "auto_optimized", "fallback": "next_hop"},
                }
            },
        }
    }
    return {"1.NE": _seed_policy_map_envelope("map1", maps), "3.NE": {}, "5.NE": {}}


def _shaper_traffic_classes(names: tuple[str, str, str]) -> dict[str, Any]:
    """Trimmed traffic-class subtree (live has 10 classes; 3 is enough to
    exercise numeric key ordering and per-class passthrough)."""
    return {
        "1": {"name": names[0], "priority": 1, "min_bw": 10, "max_bw": 100,
              "excess": 100, "max_wait": 100},
        "2": {"name": names[1], "priority": 2, "min_bw": 5, "max_bw": 100,
              "excess": 100, "max_wait": 200},
        "10": {"name": names[2], "priority": 10, "min_bw": 0, "max_bw": 100,
               "excess": 1, "max_wait": 1000},
    }


def _seed_shapers() -> dict[str, dict[str, Any]]:
    """Outbound shapers, ECOS path 'shapers'. Real captured shape (#33): a
    bare interface-keyed table, no data/options envelope."""
    table = {
        "default": {
            "accuracy": 1000,
            "dyn_bw_enable": False,
            "enable": True,
            "max_bw": 1000000,
            "traffic-class": _shaper_traffic_classes(("realtime", "interactive", "default")),
        },
        "wan": {
            "accuracy": 1000,
            "dyn_bw_enable": True,
            "enable": True,
            "max_bw": 500000,
            "traffic-class": _shaper_traffic_classes(("realtime", "interactive", "default")),
        },
        "lan0": {
            "accuracy": 1000,
            "dyn_bw_enable": False,
            "enable": False,
            "max_bw": 1000000,
            "traffic-class": _shaper_traffic_classes(("realtime", "interactive", "default")),
        },
    }
    return {
        "1.NE": copy.deepcopy(table),
        "3.NE": {"wan": copy.deepcopy(table["wan"])},
        "5.NE": {},
    }


def _seed_inbound_shapers() -> dict[str, dict[str, Any]]:
    """Inbound shapers, ECOS path 'inboundShapers'. Same interface-keyed
    shape as 'shapers' plus a per-interface if_shaping_enable (#33)."""
    table = {
        "wan": {
            "accuracy": 5000,
            "dyn_bw_enable": False,
            "enable": True,
            "if_shaping_enable": True,
            "max_bw": 200000,
            "traffic-class": _shaper_traffic_classes(("realtime", "interactive", "default")),
        },
        "wan0": {
            "accuracy": 5000,
            "dyn_bw_enable": False,
            "enable": False,
            "if_shaping_enable": False,
            "max_bw": 200000,
            "traffic-class": _shaper_traffic_classes(("realtime", "interactive", "default")),
        },
    }
    return {"1.NE": copy.deepcopy(table), "3.NE": {}, "5.NE": {}}


# -- end policy maps / shapers (#33) seed data --------------------------------


# -- internal subnets (#37) ---------------------------------------------------
#
# Orchestrator-scope singleton served by GET/POST /gms/internalSubnets2 (see
# the routes block of the same label below). Seeded from the real captured
# payload, including the live-only `segmentedIpv6Enabled` key that is absent
# from the vendored spec — it exercises the resource's unknown-key passthrough.


def _seed_internal_subnets() -> dict[str, Any]:
    return {
        "ipv4": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "224.0.0.0/4"],
        "ipv6": ["fe80::/10", "ff00::/8", "fc00::/7"],
        "segmentIpv4": [],
        "segmentIpv6": [],
        "segmentedIpv6Enabled": True,
        "nonDefaultRoutes": False,
    }


# -- end internal subnets (#37) seed data -------------------------------------


# -- nat (#32) ----------------------------------------------------------------
#
# Two appliance-scope ECOS paths ("natMaps", "nat/natPools") ride the generic
# /appliance/rest proxy, same as vrrp/zones/securityMaps — no dedicated route
# is needed, only seed data merged in by _seed_appliance_ecos() below. The one
# writable NAT endpoint on the Orchestrator API (/vrf/config/snatMaps) does
# get a route block, down next to the other orchestrator routes.


def _seed_nat_maps() -> dict[str, dict[str, Any]]:
    """NAT policy-map table, ECOS path 'natMaps'.

    Real captured envelope (#32, read-only against a live lab Orchestrator):
    ``{"data": {"map1": {"self": "map1", "prio": {...}}}, "options": {...}}``.
    The envelope, the `self` echo and the map->prio nesting are live-
    confirmed; the individual rule fields below follow the appliance spec's
    NatPolicyRule/BrNatPriority schemas. `options` is seeded with `activeMap`
    only — that key is what resources/nat.py lifts into canonical state;
    `merge`/`templateApply` are write-only directives and are covered by a
    unit test rather than invented into a GET response here.
    """
    return {
        "1.NE": {
            "data": {
                "map1": {
                    "self": "map1",
                    "prio": {
                        "10": {
                            "self": 10,
                            "comment": "",
                            "gms_marked": True,
                            "match": {"acl": ""},
                            "set": {"nat_dir": "none"},
                        },
                        "65535": {
                            "self": 65535,
                            "comment": "",
                            "gms_marked": True,
                            "match": {"acl": ""},
                            "set": {"nat_dir": "none"},
                        },
                    },
                }
            },
            "options": {"activeMap": "map1"},
        },
        "3.NE": {
            "data": {
                "map2": {
                    "self": "map2",
                    "prio": {
                        "100": {
                            "self": 100,
                            "comment": "branch outbound",
                            "gms_marked": False,
                            "match": {"acl": ""},
                            "set": {"nat_dir": "outbound"},
                        }
                    },
                }
            },
            "options": {"activeMap": "map2"},
        },
        "5.NE": {},
    }


def _seed_nat_pools() -> dict[str, dict[str, Any]]:
    """NAT pool table, ECOS path 'nat/natPools'.

    SPEC-DERIVED (#32): the live lab returned ``{}`` here, so this follows
    the appliance spec's NATPools/NATPool schema ({poolId: {name, subnet,
    dir, pat, comment}}) rather than a captured payload.
    """
    return {
        "1.NE": {
            "1": {
                "name": "pool1",
                "subnet": "192.0.2.0/24",
                "dir": "outbound",
                "pat": 1,
                "comment": "",
            }
        },
        "3.NE": {},
        "5.NE": {},
    }


def _seed_snat_maps() -> dict[str, Any]:
    """Orchestrator-scope inter-segment S-NAT rules (/vrf/config/snatMaps).

    SPEC-DERIVED (#32): the live lab returned ``{}``. Shape follows the
    SnatMaps schema plus the vendored SDK's documented read-side extras
    (gms_marked, comment). Only *disabled* pairs are listed — S-NAT between
    segments is on by default.
    """
    return {"0_1": {"enable": False, "gms_marked": False, "comment": "no snat to guest"}}


# -- end nat (#32) seed data ---------------------------------------------------
# -- common settings (#38) ----------------------------------------------------
#
# SNMP, logging, management services and banners are appliance-scope singleton
# settings documents: they ride the generic /appliance/rest proxy (the
# appliance_ecos store) exactly like vrrp/zones/securityMaps, so no new
# appliance route is needed here — only seed data, merged in by
# _seed_appliance_ecos() below. Shapes are the ones captured read-only against
# a live lab Orchestrator this session (see resources/common_settings.py).
#
# The Orchestrator schedule timezone is the one member of the group with a real
# Orchestrator write path, so it does need its own route (down in create_app)
# and its own MockState field.
#
# 1.NE / 3.NE are seeded populated; 5.NE is deliberately left out of every seed
# below so tests have one appliance whose settings come back as the proxy's
# empty-{} default and must normalize to documented defaults.


def _seed_snmp() -> dict[str, dict[str, Any]]:
    """ECOS 'snmp'. Live top-level key set, including the two keys the vendored
    SNMP schema does not declare (hash_algs / priv_algs) — kept so the
    unknown-key passthrough is actually exercised."""
    return {
        "1.NE": {
            "access": {"rocommunity": "public"},
            "auto_launch": True,
            "listen": {"enable": True},
            "syscontact": "netops@example.com",
            "sysdescr": "HUB1 EdgeConnect",
            "syslocation": "HQ",
            "traps": {"enable": True, "trap_community": "public"},
            "trapsink": {"sink": {"10.0.0.9": {"self": "10.0.0.9", "version": "v2c"}}},
            "v3": {"users": {}},
            "hash_algs": ["MD5", "SHA"],
            "priv_algs": ["DES", "AES-128"],
        },
        "3.NE": {
            "access": {"rocommunity": ""},
            "auto_launch": False,
            "listen": {"enable": False},
            "syscontact": "",
            "sysdescr": "BR1 EdgeConnect",
            "syslocation": "Branch-1",
            "traps": {"enable": False, "trap_community": "public"},
            "trapsink": {"sink": {}},
            "v3": {"users": {}},
            "hash_algs": ["MD5", "SHA"],
            "priv_algs": ["DES", "AES-128"],
        },
    }


def _seed_logging_config() -> dict[str, dict[str, Any]]:
    """ECOS 'logging/config'. 'ids' is live-present but spec-absent — seeded so
    the passthrough is covered."""
    return {
        "1.NE": {
            "min_priority": "Notice",
            "threshold_size": 50,
            "keep_number": 30,
            "auditlog": "local0",
            "flow": "local1",
            "system": "local2",
            "ids": "local3",
            "logStatefulWanDrops": False,
            "mask_enable": False,
            "mask_ipv4": 24,
            "format_log_enable": False,
        },
        "3.NE": {
            "min_priority": "Error",
            "threshold_size": 50,
            "keep_number": 30,
            "auditlog": "local0",
            "flow": "local1",
            "system": "local2",
            "ids": "local3",
            "logStatefulWanDrops": True,
            "mask_enable": False,
            "mask_ipv4": 24,
            "format_log_enable": False,
        },
    }


def _seed_mgmt_services() -> dict[str, dict[str, Any]]:
    """ECOS 'mgmtServices'. Every service record carries the 'self' id echo the
    live capture shows — resources/common_settings.py strips it on read (via
    security_policy._strip_meta) and does not re-send it."""

    def service(service_id: str, displayname: str, srcinf: str) -> dict[str, Any]:
        return {"self": service_id, "displayname": displayname, "srcinf": srcinf}

    services = {
        "aaa": ("AAA", "mgmt0"),
        "dhcrelay": ("DHCP Relay", ""),
        "netflowd": ("NetFlow", "mgmt0"),
        "node": ("Node", ""),
        "ntpd": ("NTP", "mgmt0"),
        "other": ("Other", ""),
        "snmpd": ("SNMP", "mgmt0"),
        "sshd": ("SSH", "mgmt0"),
    }
    table = {sid: service(sid, name, srcinf) for sid, (name, srcinf) in services.items()}
    return {"1.NE": copy.deepcopy(table), "3.NE": copy.deepcopy(table)}


def _seed_banners() -> dict[str, dict[str, Any]]:
    """ECOS 'banners'. Live shape is exactly {"motd": ..., "issue": ...}."""
    return {
        "1.NE": {"motd": "Welcome to HUB1", "issue": "Authorized access only."},
        "3.NE": {"motd": "", "issue": ""},
    }


def _seed_schedule_timezone() -> dict[str, Any]:
    """GET/POST /gms/scheduleTimezone — Orchestrator scope, live shape."""
    return {"defaultTimezone": "UTC"}


# -- end common settings (#38) seed data --------------------------------------


def _seed_appliance_ecos() -> dict[str, dict[str, Any]]:
    """Seed for the generic per-appliance ECOS store (``appliance_ecos``
    below). Extend with more ``{ecosPath: payload}`` entries as further
    appliance-scope resources land (Phase 2, #3) — one ``dict.setdefault``
    block per resource keeps additions merge-friendly."""
    out: dict[str, dict[str, Any]] = {}
    for ne_pk, entries in _seed_vrrp().items():
        out.setdefault(ne_pk, {})["vrrp"] = entries
    for ne_pk, interfaces in _seed_loopback_interfaces().items():
        out.setdefault(ne_pk, {})["virtualif/loopback"] = interfaces
    for ne_pk, zone_table in _seed_appliance_zones().items():
        out.setdefault(ne_pk, {})["zones"] = zone_table
    for ne_pk, sec_map_table in _seed_appliance_security_maps().items():
        out.setdefault(ne_pk, {})["securityMaps"] = sec_map_table
    for ne_pk, system in _seed_bgp_system().items():
        out.setdefault(ne_pk, {})["bgp/config/system"] = system
    for ne_pk, neighbors in _seed_bgp_neighbor().items():
        out.setdefault(ne_pk, {})["bgp/config/neighbor"] = neighbors
    for ne_pk, system in _seed_ospf_system().items():
        out.setdefault(ne_pk, {})["ospf/config/system"] = system
    for ne_pk, interfaces in _seed_ospf_interfaces().items():
        out.setdefault(ne_pk, {})["ospf/config/interfaces"] = interfaces
    # -- nat (#32) --
    for ne_pk, nat_maps in _seed_nat_maps().items():
        out.setdefault(ne_pk, {})["natMaps"] = nat_maps
    for ne_pk, nat_pools in _seed_nat_pools().items():
        out.setdefault(ne_pk, {})["nat/natPools"] = nat_pools
    # -- policy maps / shapers (#33) --
    for ne_pk, qos in _seed_qos_maps().items():
        out.setdefault(ne_pk, {})["qosMaps"] = qos
    for ne_pk, opt in _seed_optimization_maps().items():
        out.setdefault(ne_pk, {})["optimizationMaps"] = opt
    for ne_pk, route_maps in _seed_route_maps().items():
        out.setdefault(ne_pk, {})["routeMaps"] = route_maps
    for ne_pk, shaper in _seed_shapers().items():
        out.setdefault(ne_pk, {})["shapers"] = shaper
    for ne_pk, inbound in _seed_inbound_shapers().items():
        out.setdefault(ne_pk, {})["inboundShapers"] = inbound
    # -- common settings (#38) --
    for ne_pk, snmp in _seed_snmp().items():
        out.setdefault(ne_pk, {})["snmp"] = snmp
    for ne_pk, logging_config in _seed_logging_config().items():
        out.setdefault(ne_pk, {})["logging/config"] = logging_config
    for ne_pk, mgmt_services in _seed_mgmt_services().items():
        out.setdefault(ne_pk, {})["mgmtServices"] = mgmt_services
    for ne_pk, banners in _seed_banners().items():
        out.setdefault(ne_pk, {})["banners"] = banners
    return out


# -- priorities (#36) ---------------------------------------------------------
#
# Two orchestrator-scope, full-overwrite priority structures with genuinely
# different shapes (see resources/priorities.py): a keyed map and an ordered
# list. Routes live down in create_app next to the other orchestrator routes.


def _seed_overlay_priority() -> dict[str, Any]:
    """``/gms/overlays/priority``: ``{priority: overlayId}``.

    Real captured shape is ``{"1": 1, "2": 2, "3": 3, "4": 4}`` on a
    four-overlay fabric; this mock seeds exactly one overlay (id 1, see
    ``_seed_overlays``), so the seeded map is its single-overlay counterpart.
    Note the key is the *priority* and the value the *overlay id* — the
    direction the OpenAPI spec, the vendored SDK and docs/research all state.
    """
    return {"1": 1}


def _seed_template_group_priorities() -> list[str]:
    """``/template/templateGroupsPriorities``: ordered group-name list.

    Real captured shape, verbatim: ``{"priorities": ["Default Template
    Group"]}``. The names are deliberately NOT cross-checked against
    ``mock.template_groups`` (which starts empty) — the handler is
    pass-through like every other one here, and the resource plugin does not
    check existence either (a group can be created earlier in the same
    changeset).
    """
    return ["Default Template Group"]


# -- end priorities (#36) seed data ---------------------------------------------
# -- regions (#35) ------------------------------------------------------------
#
# Shapes seeded from the real payloads captured live (read-only) this session:
#   GET /regions                     -> [{"regionId": 0, "regionName": "Default"}]
#   GET /gms/overlays/config/regions -> {overlayId: {regionId: <overlay config>}}
# The lab fabric only had the global region 0; a second region ("EMEA", id 1)
# is seeded here so the region-scoped write can be *tested* for not clobbering
# its neighbours, and so regionId 0 is distinguishable from a real region.
#
# match.overlayAcl is deliberately seeded as a JSON-encoded STRING with its
# keys NOT in sorted order — that is what the real Orchestrator returns, and
# it is the phantom-drift hazard resources/regions.py normalizes away.
#
# POST /gms/overlays/config/regions (the exhaustive array-of-map replace) is
# deliberately NOT modeled: resources/regions.py never calls it, and modeling
# it would mean inventing the array's overlay-identity convention, which the
# vendored spec does not pin down.


def _seed_regions() -> list[dict[str, Any]]:
    return [
        {"regionId": 0, "regionName": "Default"},
        {"regionId": 1, "regionName": "EMEA"},
    ]


def _seed_region_appliances() -> dict[str, int]:
    """{nePk: regionId} — every appliance starts in the global region 0."""
    return {"1.NE": 0, "3.NE": 0, "5.NE": 0}


def _overlay_acl(overlay_name: str) -> str:
    """A JSON-encoded overlayAcl string, keys intentionally unsorted."""
    return json.dumps(
        {
            "options": {"merge": True, "templateApply": False},
            "data": {
                f"Overlay_{overlay_name}": {
                    "entry": {
                        "1010": {"prot": "ip", "dscp": "ef", "misc": "", "self": True},
                    }
                }
            },
        },
        sort_keys=False,
    )


def _regional_overlay_config(overlay_id: int, overlay_name: str, hubs: list[str]) -> dict[str, Any]:
    """One regional overlay config, trimmed from the 27-field live payload to
    the fields whose shapes were actually observed."""
    return {
        "name": overlay_name,
        "id": overlay_id,
        "topology": {
            "topologyType": 1,
            "hubs": hubs,
            "useRegions": False,
            "externalHubs": [],
        },
        "match": {"overlayAcl": _overlay_acl(overlay_name)},
        "brownoutThresholds": {"latency": 150, "loss": 5, "jitter": 30},
        "wanPorts": {"primary": ["1"], "secondary": [], "backup": []},
        "bondingPolicy": 1,
        "useBackupOnBrownout": False,
        "useSecondaryOnBrownout": True,
    }


def _seed_regional_overlays() -> dict[str, dict[str, Any]]:
    """{overlayId: {regionId: config}}; overlay "1" matches _seed_overlays()."""
    return {
        "1": {
            "0": _regional_overlay_config(1, "CorpFabric", ["1.NE"]),
            "1": _regional_overlay_config(1, "CorpFabric", ["3.NE"]),
        }
    }


# -- end regions (#35) seed data ----------------------------------------------


# -- acls / ipObjects / appExpress (#31) --------------------------------------
#
# Two scopes in one issue (see resources/acls.py):
#   * ACLs are appliance-scope — GET/POST ECOS "acls" and GET
#     "dependency/acl/{name}", dispatched out of the generic /appliance/rest
#     proxy below (like subnets3, they need real behavior rather than the
#     opaque per-url KV store).
#   * ipObjects + appExpress are orchestrator-scope, with their own routes in
#     the matching labeled block inside create_app().
#
# The ACL seed is the live-captured shape (rules carry the `self` echo and the
# `gms_marked` flag so the strip/inject round-trip is exercised, and `qmap`/
# `rmap` sit alongside `entry` so normalize()'s drop of the server-derived
# reference info is exercised too). The ipObjects/appExpress seeds are
# spec-derived: those collections were empty on the live lab.


def _seed_acls() -> dict[str, dict[str, Any]]:
    """Per-appliance ACL table, ECOS path 'acls' (#31, live-captured shape)."""
    return {
        "1.NE": {
            "Overlay_RealTime": {
                "entry": {
                    "1000": {
                        "self": 1000,
                        "comment": "",
                        "gms_marked": False,
                        "permit": True,
                        "application": "rtp",
                    }
                },
                "qmap": {},
                "rmap": {},
            }
        },
        "3.NE": {
            "Overlay_BulkApps": {
                "entry": {
                    "1000": {
                        "self": 1000,
                        "comment": "",
                        "gms_marked": False,
                        "permit": True,
                        "application": "ftp",
                    },
                    "1010": {
                        "self": 1010,
                        "comment": "",
                        "gms_marked": False,
                        "permit": True,
                        "application": "rsync",
                    },
                },
                "qmap": {},
                "rmap": {},
            },
            "Overlay_CriticalApps": {
                "entry": {
                    "1000": {
                        "self": 1000,
                        "comment": "",
                        "gms_marked": False,
                        "permit": True,
                        "application": "sap",
                    }
                },
                "qmap": {},
                "rmap": {},
            },
        },
        "5.NE": {},
    }


def _seed_acl_dependencies() -> dict[str, dict[str, Any]]:
    """``GET dependency/acl/{name}`` payloads, keyed {nePk: {aclName: body}}.

    Seeded empty: the live lab had no dependent objects to capture, so the
    real response shape is unknown and resources/acls.py parses it
    tolerantly. Tests populate this to exercise the in-use pre-flight.
    """
    return {"1.NE": {}, "3.NE": {}, "5.NE": {}}


def _seed_address_groups() -> dict[str, Any]:
    """``/ipObjects/addressGroup`` — spec-derived (live lab returned [])."""
    return {
        "Branch-Nets": {
            "type": "AG",
            "name": "Branch-Nets",
            "rules": [
                {
                    "includedIPs": ["10.1.0.0/16", "10.2.0.0/16"],
                    "excludedIPs": ["10.1.99.0/24"],
                    "includedGroups": [],
                    "comment": "branch subnets",
                }
            ],
        }
    }


def _seed_service_groups() -> dict[str, Any]:
    """``/ipObjects/serviceGroup`` — spec-derived (live lab returned [])."""
    return {
        "Web": {
            "type": "SG",
            "name": "Web",
            "rules": [
                {
                    "protocol": "TCP",
                    "icmpTypes": [],
                    "icmpCodes": [],
                    "includedPorts": ["443", "8000-8002"],
                    "excludedPorts": [],
                    "includedGroups": [],
                    "excludedGroups": [],
                    "comment": "web ports",
                }
            ],
        }
    }


def _seed_app_express_groups() -> dict[str, Any]:
    """``/applicationDefinition/appExpressGroup/config`` — spec-derived (live
    lab returned {}). Keyed by lower-cased group name, per the spec."""
    return {
        "saas": {
            "name": "SaaS",
            "targetQoE": "EXCELLENT",
            "overlayId": 1,
            "eligibleTransportPaths": ["INET1", "MPLS1"],
            "userQoEUpdateInterval": 300,
            "pingQoEUpdateInterval": 60,
            "pingInterval": 10,
            "sourceLoopbacks": [],
            "useSystemDnsServer": True,
            "dnsServers": [],
            "appExpressApps": ["office365"],
        }
    }


def _seed_app_express_associations() -> list[dict[str, Any]]:
    """``/applicationDefinition/appExpressGroup/association`` — spec-derived
    (live lab returned []); the whole table is replaced by one POST."""
    return [{"nePk": "1.NE", "appExpressGroupName": "SaaS"}]


# -- end acls / ipObjects / appExpress (#31) seed data ------------------------


# -- state -------------------------------------------------------------------


@dataclass
class MockState:
    """Resettable in-memory Orchestrator state shared with the FastAPI app.

    Tests reach into this directly (e.g. ``state.actions`` for action keys,
    ``state.fail_next_action`` to force a failing job, ``state.require_auth``
    to disable auth checks).
    """

    require_auth: bool = True
    action_delay_polls: int = 1
    fail_next_action: bool = False
    appliances: list[dict[str, Any]] = field(default_factory=_seed_appliances)
    interface_labels: dict[str, Any] = field(default_factory=_seed_interface_labels)
    template_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    template_selection: dict[str, list[str]] = field(default_factory=dict)
    template_association: dict[str, list[str]] = field(default_factory=dict)
    overlays: dict[str, dict[str, Any]] = field(default_factory=_seed_overlays)
    overlay_association: dict[str, list[str]] = field(default_factory=_seed_overlay_association)
    security_maps: dict[str, Any] = field(default_factory=_seed_security_maps)
    #: Segment-pair keyed security policy data ("0_0" -> SecurityMaps object).
    security_policies: dict[str, Any] = field(default_factory=dict)
    #: Orchestrator firewall zone table ({zone_id: {"name": ...}}) plus the
    #: monotonic id allocator and the end-to-end ZBFW flag.
    zones: dict[str, Any] = field(default_factory=_seed_zones)
    zones_next_id: int = 1
    zones_ee_enable: bool = False
    #: Read-only views: segment<->zone map and per-appliance cached zone lists.
    vrf_zones_map: dict[str, Any] = field(default_factory=_seed_vrf_zones_map)
    zone_list_meta: dict[str, Any] = field(default_factory=_seed_zone_list_meta)
    #: Static routes (#15): per-appliance "configured" table
    #: ({nePk: {"prefix": {cidr: {...}}}}), mutated by addMultiple/
    #: deleteMultiple; and the extra prefixes visible only via subnets3/all
    #: (simulated learned routes), never mutated by those two endpoints.
    static_routes: dict[str, dict[str, Any]] = field(default_factory=_seed_static_routes)
    static_routes_learned: dict[str, dict[str, Any]] = field(
        default_factory=_seed_static_routes_learned
    )
    #: Per-appliance ECOS store reached via the /appliance/rest proxy:
    #: {nePk: {ecosPath: payload}}. Seeded per-resource by
    #: _seed_appliance_ecos() (vrrp #14, per-appliance loopback #18).
    appliance_ecos: dict[str, dict[str, Any]] = field(default_factory=_seed_appliance_ecos)
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: set[str] = field(default_factory=set)
    next_overlay_id: int = 2
    # bgp (#16) and ospf (#17) ride the generic appliance_ecos proxy store
    # above (Stage 2) — see _seed_bgp_system/_seed_bgp_neighbor/
    # _seed_ospf_system/_seed_ospf_interfaces, merged in by
    # _seed_appliance_ecos(). No dedicated MockState fields needed.
    # -- deployment (#12) --
    #: Consumed by the next POST .../url=deployment/validate call, like
    #: fail_next_action: forces one deterministic {err: ...} response.
    deployment_fail_validate: bool = False
    # -- loopback orchestration (#18) --
    #: Fabric-wide loopbackOrch structure ({segmentId: {loopbackPool,
    #: interfaces}}), replaced wholesale by POST /loopbackOrch.
    loopback_orch: dict[str, Any] = field(default_factory=_seed_loopback_orch)
    #: Pool allocation detail ({segmentId: {segment, subnet, totalAddr,
    #: addrAllocated, addrDeleted}}) — mutated only by the reclaim
    #: endpoints' addrDeleted bookkeeping, never by POST /loopbackOrch.
    loopback_orch_pool: dict[str, Any] = field(default_factory=_seed_loopback_orch_pool)
    # -- internal subnets (#37) --
    #: Fabric-wide internal-subnet table, replaced wholesale by
    #: POST /gms/internalSubnets2.
    internal_subnets: dict[str, Any] = field(default_factory=_seed_internal_subnets)
    # -- priorities (#36) --
    #: Overlay route-map priority map ({priority: overlayId}), replaced
    #: wholesale by POST /gms/overlays/priority.
    overlay_priority: dict[str, Any] = field(default_factory=_seed_overlay_priority)
    #: Ordered template-group apply order (list of group names), replaced
    #: wholesale by POST /template/templateGroupsPriorities.
    template_group_priorities: list[str] = field(
        default_factory=_seed_template_group_priorities
    )
    # -- regions (#35) --
    #: Network regions, the array shape GET /regions returns.
    regions: list[dict[str, Any]] = field(default_factory=_seed_regions)
    #: Next server-allocated regionId (there is no allocator endpoint; the
    #: real Orchestrator hands the id out on POST /regions, as modeled here).
    regions_next_id: int = 2
    #: {nePk: regionId} — one region per appliance.
    region_appliances: dict[str, int] = field(default_factory=_seed_region_appliances)
    #: {overlayId: {regionId: overlay config}}, region-scope-merged by
    #: PUT /gms/overlays/config/regions.
    regional_overlays: dict[str, dict[str, Any]] = field(default_factory=_seed_regional_overlays)
    # -- nat (#32) --
    #: Fabric-wide disabled inter-segment S-NAT rules, replaced wholesale by
    #: POST /vrf/config/snatMaps. The appliance-scope NAT tables (natMaps,
    #: nat/natPools) need no field of their own — they ride appliance_ecos.
    snat_maps: dict[str, Any] = field(default_factory=_seed_snat_maps)
    #: Read-only inter-segment D-NAT view ({nePk: VRFPolicyMap}); there is no
    #: D-NAT write endpoint in either spec.
    dnat_maps: dict[str, Any] = field(
        default_factory=lambda: {"1.NE": {"0_1": {"enable": True}}}
    )
    # -- common settings (#38) --
    # snmp / logging/config / mgmtServices / banners are appliance-scope and
    # live in appliance_ecos above (no dedicated fields). Only the Orchestrator
    # schedule timezone needs its own state + route.
    schedule_timezone: dict[str, Any] = field(default_factory=_seed_schedule_timezone)
    # -- acls / ipObjects / appExpress (#31) --
    #: Per-appliance ACL table ({nePk: {aclName: {entry, qmap, rmap}}}),
    #: replaced wholesale by POST acls with options.merge=false.
    acls: dict[str, dict[str, Any]] = field(default_factory=_seed_acls)
    #: {nePk: {aclName: dependency payload}} served by GET
    #: dependency/acl/{name}; seeded empty (see _seed_acl_dependencies).
    acl_dependencies: dict[str, dict[str, Any]] = field(default_factory=_seed_acl_dependencies)
    #: Orchestrator ip-object stores, keyed by group name.
    address_groups: dict[str, Any] = field(default_factory=_seed_address_groups)
    service_groups: dict[str, Any] = field(default_factory=_seed_service_groups)
    #: AppExpress group configs keyed by lower-cased name, plus the flat
    #: association array replaced wholesale by one POST.
    app_express_groups: dict[str, Any] = field(default_factory=_seed_app_express_groups)
    app_express_associations: list[dict[str, Any]] = field(
        default_factory=_seed_app_express_associations
    )

    def reset(self) -> None:
        """Restore every field to its seeded default (in place)."""
        fresh = MockState()
        for spec in fields(self):
            setattr(self, spec.name, getattr(fresh, spec.name))

    def new_action(
        self, key: str | None = None, ne_pks: list[str] | None = None, name: str = ""
    ) -> str:
        """Register an async action; it finishes after ``action_delay_polls`` polls.

        A poll is one GET that returns the action's records — via either
        ``/action/status?key=`` or the ``/action`` listing; a single waiter
        only ever uses one of the two, so ``action_delay_polls`` means the
        same thing on both paths. A pending ``fail_next_action`` flag is
        consumed by the next action created, which then finishes as a failure
        (``completionStatus`` false, result ``"mock failure"``).
        """
        action_key = key or str(uuid.uuid4())
        self.actions[action_key] = {
            "polls": 0,
            "delay_polls": self.action_delay_polls,
            "fail": self.fail_next_action,
            "ne_pks": list(ne_pks) if ne_pks else [],
            "name": name,
            "startTime": int(time.time() * 1000),
        }
        self.fail_next_action = False
        return action_key


# -- app factory -------------------------------------------------------------


async def _json_body(request: Request) -> Any:
    """Parse the request body as JSON; tolerate empty/invalid bodies as None."""
    raw = await request.body()
    if not raw:
        return None
    try:
        return await request.json()
    except (ValueError, UnicodeDecodeError):
        return None


# -- static routes (#15): real add/delete-delta semantics for subnets3/* ----
#
# These four ECOS paths are proxied through the generic /appliance/rest
# handler below (appliance_proxy), like every other appliance-scope call —
# but unlike the opaque per-url KV store it falls back to, they need real
# merge/delta behavior so a resource's addMultiple/deleteMultiple round-trips
# through fetch()/normalize() correctly. appliance_proxy dispatches here for
# exactly these four (nePk, url) combinations before falling into that store.


def _mark_unsaved(mock: MockState, ne_pk: str) -> None:
    for appliance in mock.appliances:
        if appliance.get("nePk") == ne_pk:
            appliance["hasUnsavedChanges"] = True


async def _subnets3_dispatch(request: Request, mock: MockState, ne_pk: str, url: str) -> Any:
    configured = mock.static_routes.setdefault(ne_pk, {"prefix": {}})
    if url == _SUBNETS_CONFIGURED_PATH and request.method == "GET":
        return configured
    if url == _SUBNETS_ALL_PATH and request.method == "GET":
        learned = mock.static_routes_learned.get(ne_pk, {"prefix": {}})
        merged = {**learned.get("prefix", {}), **configured.get("prefix", {})}
        return {"prefix": merged}
    if url == _SUBNETS_ADD_PATH and request.method == "POST":
        body = await _json_body(request)
        new_prefixes = body.get("prefix") if isinstance(body, dict) else None
        if not isinstance(new_prefixes, dict):
            return JSONResponse(
                {"error": "addMultiple body must carry a 'prefix' object"}, status_code=400
            )
        configured.setdefault("prefix", {}).update(new_prefixes)
        _mark_unsaved(mock, ne_pk)
        return Response(status_code=204)
    if url == _SUBNETS_DELETE_PATH and request.method == "POST":
        body = await _json_body(request)
        drop = body.get("prefixes") if isinstance(body, dict) else None
        if not isinstance(drop, list):
            return JSONResponse(
                {"error": "deleteMultiple body must carry a 'prefixes' list"}, status_code=400
            )
        table = configured.setdefault("prefix", {})
        for cidr in drop:
            table.pop(str(cidr), None)
        _mark_unsaved(mock, ne_pk)
        return Response(status_code=204)
    return JSONResponse(
        {"error": f"unsupported subnets3 call: {request.method} {url}"}, status_code=400
    )


# -- acls / ipObjects / appExpress (#31): ECOS acl + dependency dispatch ----
#
# Like subnets3 above, the two ACL ECOS paths need real behavior rather than
# the opaque per-url KV store the generic proxy falls back to: POST carries
# {data, options{merge, delDependent}} and the dependency endpoint is a
# separate read used by the resource's removal pre-flight.

_ACLS_ECOS_PATH = "acls"
_ACL_DEPENDENCY_PREFIX = "dependency/acl/"


def _acl_is_in_use(dependency_payload: Any) -> bool:
    """Whether a dependency payload names anything at all.

    A record like ``{"rmap": [], "qmap": {}}`` is *present but empty* — the
    ACL is unreferenced. Only a non-empty member counts as in use.
    """
    if isinstance(dependency_payload, dict):
        return any(bool(value) for value in dependency_payload.values())
    return bool(dependency_payload)


async def _acls_dispatch(request: Request, mock: MockState, ne_pk: str, url: str) -> Any:
    table = mock.acls.setdefault(ne_pk, {})
    if url.startswith(_ACL_DEPENDENCY_PREFIX):
        if request.method != "GET":
            return JSONResponse(
                {"error": f"unsupported acl dependency call: {request.method} {url}"},
                status_code=400,
            )
        acl_name = url[len(_ACL_DEPENDENCY_PREFIX) :]
        return mock.acl_dependencies.get(ne_pk, {}).get(acl_name, {})
    if request.method == "GET":
        return table
    if request.method == "POST":
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "acls body must be an object"}, status_code=400)
        data = body.get("data")
        options = body.get("options")
        if not isinstance(data, dict) or not isinstance(options, dict):
            return JSONResponse(
                {"error": "acls body must carry 'data' and an 'options' object"},
                status_code=400,
            )
        if "merge" not in options or "delDependent" not in options:
            return JSONResponse(
                {"error": "acls options must include 'merge' and 'delDependent'"},
                status_code=400,
            )
        if not options.get("delDependent"):
            # The appliance itself refuses to drop an ACL something still
            # references unless delDependent is set — the raw rejection the
            # resource's pre-flight exists to pre-empt.
            in_use = sorted(
                name
                for name in set(table) - set(data)
                if _acl_is_in_use(mock.acl_dependencies.get(ne_pk, {}).get(name))
            )
            if in_use:
                return JSONResponse(
                    {"error": f"ACL(s) in use: {', '.join(in_use)}"}, status_code=400
                )
        if options.get("merge"):
            table.update(copy.deepcopy(data))
        else:
            mock.acls[ne_pk] = copy.deepcopy(data)
        _mark_unsaved(mock, ne_pk)
        return Response(status_code=204)
    return JSONResponse(
        {"error": f"unsupported acls call: {request.method} {url}"}, status_code=400
    )


# -- end acls / ipObjects / appExpress (#31) proxy dispatch -------------------


def _action_records(key: str, action: dict[str, Any]) -> list[dict[str, Any]]:
    finished = action["polls"] >= max(int(action.get("delay_polls", 1)), 1)
    failed = bool(action.get("fail"))
    base = {
        "guid": key,
        "name": str(action.get("name", "")),
        "startTime": int(action.get("startTime", 0)),
    }
    records: list[dict[str, Any]] = []
    for ne_pk in action.get("ne_pks") or [""]:
        if not finished:
            records.append(
                {
                    **base,
                    "nepk": ne_pk,
                    "taskStatus": "In Progress",
                    "percentComplete": 50,
                    "completionStatus": False,
                    "endTime": 0,
                    "result": "",
                }
            )
        elif failed:
            records.append(
                {
                    **base,
                    "nepk": ne_pk,
                    "taskStatus": "Failed",
                    "percentComplete": 100,
                    "completionStatus": False,
                    "endTime": int(time.time() * 1000),
                    "result": "mock failure",
                }
            )
        else:
            records.append(
                {
                    **base,
                    "nepk": ne_pk,
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": int(time.time() * 1000),
                    "result": "mock apply complete",
                }
            )
    return records


def create_app(state: MockState | None = None) -> FastAPI:
    """Build the fake-Orchestrator FastAPI app around the given (or fresh) state."""
    mock = state if state is not None else MockState()
    app = FastAPI(title="pyecsdwan fake Orchestrator", docs_url=None, redoc_url=None)
    app.state.mock = mock

    def require_auth(request: Request) -> None:
        if not mock.require_auth:
            return
        if request.headers.get("X-Auth-Token"):
            return
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie and cookie in mock.sessions:
            return
        raise HTTPException(status_code=401, detail="authentication required")

    auth = APIRouter(prefix="/gms/rest")
    api = APIRouter(prefix="/gms/rest", dependencies=[Depends(require_auth)])

    # -- authentication ------------------------------------------------------

    @auth.post("/authentication/login")
    async def login(request: Request) -> Response:
        await _json_body(request)  # {user, password, loginType} — any accepted
        session_id = uuid.uuid4().hex
        mock.sessions.add(session_id)
        response = JSONResponse({"status": "logged in"})
        response.set_cookie(_SESSION_COOKIE, session_id, httponly=True)
        # Real Orchestrator also sets the CSRF cookie the client echoes back.
        response.set_cookie("orchCsrfToken", uuid.uuid4().hex)
        return response

    # Logout is a GET on the real Orchestrator (the client uses GET too).
    @auth.get("/authentication/logout")
    async def logout(request: Request) -> Response:
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie:
            mock.sessions.discard(cookie)
        response = Response(status_code=200)
        response.delete_cookie(_SESSION_COOKIE)
        return response

    # -- appliances ----------------------------------------------------------

    @api.get("/appliance")
    async def list_appliances() -> Any:
        return mock.appliances

    # -- interface labels ----------------------------------------------------

    @api.get("/gms/interfaceLabels")
    async def get_interface_labels(active: bool | None = None) -> Any:
        labels = mock.interface_labels
        if active is None:
            return labels
        out = dict(labels)
        for side in ("wan", "lan"):
            entries = labels.get(side) or {}
            out[side] = {
                label_id: entry
                for label_id, entry in entries.items()
                if isinstance(entry, dict) and bool(entry.get("active")) == active
            }
        return out

    # -- interface-labels constraints (#39) --
    # The real Orchestrator rejects a label table that reuses an id across
    # wan+lan, and refuses to drop a label an overlay still references unless
    # deleteDependencies=true. Enforced here so the client-side pre-flight in
    # resources/interface_labels.py is genuinely proven to fire *first*: a
    # test that ever sees one of these 400s means the pre-flight missed.

    def _overlay_label_ids() -> set[str]:
        """Label ids referenced by any overlay (wanPorts + local breakout)."""
        used: set[str] = set()
        for overlay in mock.overlays.values():
            ports = overlay.get("wanPorts")
            if isinstance(ports, dict):
                for key in ("primary", "secondary", "backup", "crossConnect"):
                    used |= {str(i) for i in ports.get(key) or []}
            policy = overlay.get("internetPolicy")
            if isinstance(policy, dict) and isinstance(policy.get("localBreakout"), dict):
                for key in ("primary", "backup"):
                    used |= {str(i) for i in policy["localBreakout"].get(key) or []}
        return used

    @api.post("/gms/interfaceLabels")
    async def replace_interface_labels(
        request: Request, deleteDependencies: bool = False
    ) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a wan/lan label object"}, status_code=400)
        sides = {side: {str(k) for k in (body.get(side) or {})} for side in ("wan", "lan")}
        clash = sorted(sides["wan"] & sides["lan"])
        if clash:
            return JSONResponse(
                {"error": f"label id(s) {', '.join(clash)} are used in both wan and lan"},
                status_code=400,
            )
        if not deleteDependencies:
            before = {
                str(k) for side in ("wan", "lan") for k in (mock.interface_labels.get(side) or {})
            }
            in_use = sorted((before - sides["wan"] - sides["lan"]) & _overlay_label_ids())
            if in_use:
                return JSONResponse(
                    {
                        "error": f"label id(s) {', '.join(in_use)} are in use by an "
                        f"overlay; retry with deleteDependencies=true"
                    },
                    status_code=400,
                )
        mock.interface_labels = body
        return Response(status_code=200)

    # -- template groups -----------------------------------------------------

    @api.get("/template/templateGroups")
    async def get_template_groups(templateGroup: str | None = None) -> Any:
        if templateGroup is None:
            return list(mock.template_groups.values())
        group = mock.template_groups.get(templateGroup)
        if group is None:
            return JSONResponse(
                {"error": f"no template group {templateGroup!r}"}, status_code=404
            )
        return group

    @api.post("/template/templateGroups")
    async def update_template_group(request: Request, templateGroup: str) -> Response:
        if templateGroup not in mock.template_groups:
            return JSONResponse(
                {"error": f"no template group {templateGroup!r}"}, status_code=404
            )
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a template group object"}, status_code=400)
        mock.template_groups[templateGroup] = {**body, "name": templateGroup}
        return Response(status_code=204)

    @api.post("/template/templateCreate")
    async def create_template_group(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict) or not body.get("name"):
            return JSONResponse({"error": "body must include a group name"}, status_code=400)
        name = str(body["name"])
        group = {**body, "name": name}
        group.setdefault("templates", [])
        mock.template_groups[name] = group
        mock.template_selection.setdefault(name, [])
        if "templates" not in body:
            return Response(status_code=204)
        return JSONResponse(group, status_code=200)

    @api.delete("/template/templateGroups")
    async def delete_template_group(templateGroup: str) -> Response:
        if templateGroup not in mock.template_groups:
            return JSONResponse(
                {"error": f"no template group {templateGroup!r}"}, status_code=404
            )
        del mock.template_groups[templateGroup]
        mock.template_selection.pop(templateGroup, None)
        for ne_pk, groups in mock.template_association.items():
            mock.template_association[ne_pk] = [g for g in groups if g != templateGroup]
        return Response(status_code=204)

    @api.get("/template/templateSelection")
    async def get_template_selection(templateGroup: str) -> Any:
        if templateGroup not in mock.template_groups:
            return JSONResponse(
                {"error": f"no template group {templateGroup!r}"}, status_code=404
            )
        return mock.template_selection.get(templateGroup, [])

    @api.post("/template/templateSelection")
    async def set_template_selection(request: Request, templateGroup: str) -> Response:
        if templateGroup not in mock.template_groups:
            return JSONResponse(
                {"error": f"no template group {templateGroup!r}"}, status_code=404
            )
        body = await _json_body(request)
        if not isinstance(body, list):
            return JSONResponse({"error": "body must be a list of template names"}, status_code=400)
        mock.template_selection[templateGroup] = [str(name) for name in body]
        return Response(status_code=204)

    @api.get("/template/applianceAssociation")
    async def get_appliance_association(nePk: str | None = None) -> Any:
        if nePk is None:
            out: dict[str, list[str]] = {
                str(a.get("nePk") or a.get("id")): [] for a in mock.appliances
            }
            for ne_pk, groups in mock.template_association.items():
                out[ne_pk] = list(groups)
            return out
        return {"templateIds": list(mock.template_association.get(nePk, []))}

    @api.post("/template/applianceAssociation")
    async def set_appliance_association(request: Request, nePk: str) -> Response:
        body = await _json_body(request)
        template_ids = body.get("templateIds") if isinstance(body, dict) else None
        if not isinstance(template_ids, list):
            return JSONResponse({"error": "body must include templateIds list"}, status_code=400)
        mock.template_association[nePk] = [str(g) for g in template_ids]
        # Fire-and-204, like the real endpoint: the push's per-appliance
        # results exist only as action-log records — no key in the response.
        mock.new_action(ne_pks=[nePk], name="template push")
        return Response(status_code=204)

    @api.get("/template/history/groupList")
    async def get_applied_group_list(nePk: str) -> Any:
        return list(mock.template_association.get(nePk, []))

    # -- overlays ------------------------------------------------------------

    @api.get("/gms/overlays/config")
    async def get_overlays(overlayId: str | None = None) -> Any:
        if overlayId is None:
            return list(mock.overlays.values())
        overlay = mock.overlays.get(str(overlayId))
        if overlay is None:
            return JSONResponse({"error": f"no overlay {overlayId!r}"}, status_code=404)
        return overlay

    @api.post("/gms/overlays/config")
    async def create_overlay(request: Request) -> Any:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an overlay object"}, status_code=400)
        overlay_id = mock.next_overlay_id
        mock.next_overlay_id += 1
        overlay = {**body, "id": overlay_id, "modifiedTime": int(time.time() * 1000)}
        mock.overlays[str(overlay_id)] = overlay
        return JSONResponse({"id": overlay_id}, status_code=200)

    @api.put("/gms/overlays/config")
    async def modify_overlay(request: Request, overlayId: str) -> Any:
        existing = mock.overlays.get(str(overlayId))
        if existing is None:
            return JSONResponse({"error": f"no overlay {overlayId!r}"}, status_code=404)
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an overlay object"}, status_code=400)
        overlay = {**body, "id": existing["id"], "modifiedTime": int(time.time() * 1000)}
        mock.overlays[str(overlayId)] = overlay
        return JSONResponse(overlay, status_code=200)

    @api.delete("/gms/overlays/config")
    async def delete_overlay(overlayId: str) -> Response:
        if str(overlayId) not in mock.overlays:
            return JSONResponse({"error": f"no overlay {overlayId!r}"}, status_code=404)
        del mock.overlays[str(overlayId)]
        mock.overlay_association.pop(str(overlayId), None)
        return Response(status_code=204)

    @api.get("/gms/overlays/association")
    async def get_overlay_association() -> Any:
        out: dict[str, list[str]] = {overlay_id: [] for overlay_id in mock.overlays}
        for overlay_id, ne_pks in mock.overlay_association.items():
            out[overlay_id] = list(ne_pks)
        return out

    @api.post("/gms/overlays/association")
    async def add_overlay_association(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must map overlay id -> nePk list"}, status_code=400
            )
        for overlay_id, ne_pks in body.items():
            if not isinstance(ne_pks, list):
                continue
            current = mock.overlay_association.setdefault(str(overlay_id), [])
            for ne_pk in ne_pks:
                if str(ne_pk) not in current:
                    current.append(str(ne_pk))
        return Response(status_code=204)

    @api.post("/gms/overlays/association/remove")
    async def remove_overlay_association(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must map overlay id -> nePk list"}, status_code=400
            )
        for overlay_id, ne_pks in body.items():
            if not isinstance(ne_pks, list):
                continue
            current = mock.overlay_association.get(str(overlay_id), [])
            drop = {str(ne_pk) for ne_pk in ne_pks}
            mock.overlay_association[str(overlay_id)] = [n for n in current if n not in drop]
        return Response(status_code=204)

    # -- action log ----------------------------------------------------------

    @api.get("/action/status")
    async def get_action_status(key: str | None = None) -> Any:
        if not key or key not in mock.actions:
            return JSONResponse({"error": f"unknown action key {key!r}"}, status_code=404)
        action = mock.actions[key]
        action["polls"] += 1
        return _action_records(key, action)

    @api.get("/action")
    async def list_actions(
        startTime: int,
        endTime: int,
        logLevel: int = 1,
        limit: int = 100,
        appliance: str | None = None,
    ) -> Any:
        """Action/audit log listing: epoch-ms window (required, like the real
        API) plus the ``appliance`` nePk filter. Every emitted action counts
        the call as one poll, so ``action_delay_polls`` drives the keyless
        waiter exactly as ``/action/status`` drives the keyed one."""
        records: list[dict[str, Any]] = []
        for key, action in mock.actions.items():
            if appliance is not None and appliance not in (action.get("ne_pks") or []):
                continue
            if not startTime <= int(action.get("startTime", 0)) <= endTime:
                continue
            action["polls"] += 1
            records.extend(_action_records(key, action))
        return records[: max(limit, 0)]

    # -- security maps -------------------------------------------------------

    @api.get("/securityMaps")
    async def get_security_maps(nePk: str | None = None, cached: bool | None = None) -> Any:
        return mock.security_maps

    # -- security policy orchestration (segment-pair scoped) ----------------

    @api.get("/vrf/config/securityPolicies")
    async def get_security_policies(map: str) -> Any:
        data = mock.security_policies.get(map)
        if data is None:
            return Response(status_code=204)
        return {"data": data}

    @api.post("/vrf/config/securityPolicies")
    async def set_security_policies(request: Request, map: str) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict) or "data" not in body:
            raise HTTPException(status_code=400, detail="body must carry a 'data' object")
        mock.security_policies[map] = body["data"]
        return Response(status_code=204)

    # -- zones (orchestrator scope) ------------------------------------------

    @api.get("/zones")
    async def get_zones(allVRFZones: bool = False) -> Any:
        # The mock keeps one unique-names table; allVRFZones=true would add
        # per-segment duplicates on a real Orchestrator.
        return mock.zones

    @api.post("/zones")
    async def replace_zones(request: Request, deleteDependencies: bool) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must map zone id -> zone object"}, status_code=400
            )
        mock.zones = {str(zone_id): zone for zone_id, zone in body.items()}
        # Real Orchestrator behavior: the Default zone is re-added to any
        # table posted without it.
        mock.zones.setdefault("0", {"name": "Default"})
        return Response(status_code=204)

    @api.get("/zones/nextId")
    async def get_zone_next_id() -> Any:
        return {"nextId": mock.zones_next_id}

    @api.post("/zones/nextId")
    async def set_zone_next_id(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict) or "nextId" not in body:
            return JSONResponse({"error": "body must carry nextId"}, status_code=400)
        mock.zones_next_id = int(body["nextId"])
        return Response(status_code=204)

    @api.get("/zones/eeEnable")
    async def get_zones_ee_enable() -> Any:
        return {"enable": mock.zones_ee_enable}

    @api.post("/zones/eeEnable")
    async def set_zones_ee_enable(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict) or "enable" not in body:
            return JSONResponse({"error": "body must carry enable"}, status_code=400)
        mock.zones_ee_enable = bool(body["enable"])
        return Response(status_code=204)

    @api.get("/zones/vrfZonesMap")
    async def get_vrf_zones_map() -> Any:
        return mock.vrf_zones_map

    @api.get("/appliance/zoneListMeta")
    async def get_zone_list_meta(nePk: str | None = None) -> Any:
        if nePk is None:
            return mock.zone_list_meta
        return {nePk: mock.zone_list_meta.get(nePk, {"zones": []})}

    # -- appliance proxy + save-changes -------------------------------------

    # vrrp (#14) is served through this generic proxy too: GET/POST
    # /appliance/rest?nePk=<pk>&url=vrrp reads/replaces
    # mock.appliance_ecos[nePk]["vrrp"], seeded by _seed_appliance_ecos()
    # above. No vrrp-specific route is needed — every appliance-scope
    # resource this proxy is generic over addresses it the same way.
    @api.api_route("/appliance/rest", methods=["GET", "POST", "PUT", "DELETE"])
    async def appliance_proxy(request: Request, nePk: str, url: str) -> Any:
        """Proxy to a per-appliance ECOS store keyed by (nePk, url). Writes set
        hasUnsavedChanges on the appliance until saveChanges clears it."""
        # -- static routes (#15): real behavior instead of the opaque store.
        if url in (
            _SUBNETS_ALL_PATH,
            _SUBNETS_CONFIGURED_PATH,
            _SUBNETS_ADD_PATH,
            _SUBNETS_DELETE_PATH,
        ):
            return await _subnets3_dispatch(request, mock, nePk, url)
        # -- acls / ipObjects / appExpress (#31): real ACL + dependency behavior.
        if url == _ACLS_ECOS_PATH or url.startswith(_ACL_DEPENDENCY_PREFIX):
            return await _acls_dispatch(request, mock, nePk, url)
        store = mock.appliance_ecos.setdefault(nePk, {})
        # -- deployment (#12) --
        # deployment/validate is a virtual ECOS endpoint: it never touches
        # the ecos store, it only inspects the candidate body and reports
        # {err, rebootRequired} per the real validate-then-apply contract.
        if url == "deployment/validate":
            body = await _json_body(request)
            force_fail = mock.deployment_fail_validate
            mock.deployment_fail_validate = False
            return _validate_deployment(body, force_fail)
        if url == "deployment" and request.method == "GET" and url not in store:
            # Seed realistic interface/IP fixture data on first read so
            # tests exercise the real captured shape, not an empty object.
            return _seed_deployment()
        # -- end deployment (#12) --
        if request.method == "GET":
            return store.get(url, {})
        if request.method == "DELETE":
            store.pop(url, None)
        else:
            body = await _json_body(request)
            store[url] = body
        for appliance in mock.appliances:
            if appliance.get("nePk") == nePk:
                appliance["hasUnsavedChanges"] = True
        return Response(status_code=204)

    @api.post("/appliance/saveChanges")
    async def save_changes(request: Request, nePk: str | None = None) -> Any:
        body = await _json_body(request)
        ne_pks = [nePk] if nePk else (body.get("nePks", []) if isinstance(body, dict) else [])
        # A save armed to fail (fail_next_action) persists nothing: the
        # hasUnsavedChanges flag stays set, like a real failed save. Checked
        # before new_action(), which consumes the flag.
        if not mock.fail_next_action:
            for appliance in mock.appliances:
                if appliance.get("nePk") in ne_pks:
                    appliance["hasUnsavedChanges"] = False
        return {
            "clientKey": mock.new_action(
                ne_pks=[str(p) for p in ne_pks], name="save changes"
            )
        }

    # bgp (#16) and ospf (#17) Stage 2 moved to the generic /appliance/rest
    # proxy below (appliance_ecos store) — no dedicated orchestrator-level
    # routes needed any more, matching vrrp/routes/zones/security-maps.

    # -- loopback (#18) -------------------------------------------------------
    #
    # Per-appliance loopback interfaces ride the generic /appliance/rest
    # proxy below (key "virtualif/loopback", seeded by _seed_appliance_ecos
    # above) — no dedicated route needed. Loopback orchestration is
    # fabric-wide (not per-appliance), so it gets its own routes here.

    @api.get("/loopbackOrch")
    async def get_loopback_orch() -> Any:
        return mock.loopback_orch

    @api.post("/loopbackOrch")
    async def replace_loopback_orch(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must map segment-id -> {loopbackPool, interfaces}"},
                status_code=400,
            )
        mock.loopback_orch = {str(seg_id): seg for seg_id, seg in body.items()}
        return Response(status_code=204)

    @api.get("/loopbackOrch/pool")
    async def get_loopback_orch_pool() -> Any:
        return mock.loopback_orch_pool

    @api.delete("/loopbackOrch/pool/reclaim/{loopback_id}")
    async def reclaim_one_loopback_ip(loopback_id: int) -> Response:
        # Mock bookkeeping only: decrements segment "0"'s addrDeleted count
        # (no per-id deleted-ip history is modeled here) so the route is
        # observably real rather than a no-op 204.
        pool = mock.loopback_orch_pool.get("0")
        if isinstance(pool, dict) and pool.get("addrDeleted", 0) > 0:
            pool["addrDeleted"] -= 1
        return Response(status_code=204)

    @api.delete("/loopbackOrch/pool/reclaim")
    async def reclaim_all_loopback_ips() -> Response:
        for pool in mock.loopback_orch_pool.values():
            if isinstance(pool, dict):
                pool["addrDeleted"] = 0
        return Response(status_code=204)

    # -- end loopback (#18) ---------------------------------------------------

    # -- internal subnets (#37) -----------------------------------------------
    #
    # Orchestrator scope (not the appliance proxy): one fabric-wide table,
    # read whole and replaced whole. The POST stores the body verbatim so a
    # test can observe that unknown keys survive the round trip.

    @api.get("/gms/internalSubnets2")
    async def get_internal_subnets() -> Any:
        return mock.internal_subnets

    @api.post("/gms/internalSubnets2")
    async def replace_internal_subnets(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must be an internal-subnets object"}, status_code=400
            )
        mock.internal_subnets = dict(body)
        return Response(status_code=204)

    # -- end internal subnets (#37) -------------------------------------------

    # -- nat (#32) ------------------------------------------------------------
    #
    # The appliance-scope NAT tables (ECOS "natMaps", "nat/natPools") need no
    # route: they ride the generic /appliance/rest proxy above, seeded by
    # _seed_appliance_ecos(). Only the writable orchestrator endpoint and the
    # read-only D-NAT view live here.

    @api.get("/vrf/config/snatMaps")
    async def get_snat_maps() -> Any:
        return mock.snat_maps

    @api.post("/vrf/config/snatMaps")
    async def replace_snat_maps(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must map '<srcVrfId>_<dstVrfId>' -> {enable: bool}"},
                status_code=400,
            )
        mock.snat_maps = {str(pair): rule for pair, rule in body.items()}
        return Response(status_code=204)

    @api.get("/dnatMaps")
    async def get_dnat_maps(nePk: str, cached: str = "false") -> Any:
        return mock.dnat_maps.get(nePk, {})

    # -- end nat (#32) --------------------------------------------------------
    # -- common settings (#38) ------------------------------------------------
    #
    # snmp / logging/config / mgmtServices / banners need no routes here: they
    # are appliance-scope and ride the generic /appliance/rest proxy above,
    # backed by the appliance_ecos store (seeded by _seed_appliance_ecos).
    # Only the Orchestrator schedule timezone is orchestrator-writable.

    @api.get("/gms/scheduleTimezone")
    async def get_schedule_timezone() -> Any:
        return mock.schedule_timezone

    @api.post("/gms/scheduleTimezone")
    async def set_schedule_timezone(request: Request) -> Response:
        # Body shape is the SDK's {"defaultTimezone": tz} object, not the
        # spec's bare string — see resources/common_settings.py.
        body = await _json_body(request)
        if not isinstance(body, dict) or not body.get("defaultTimezone"):
            return JSONResponse(
                {"error": "body must be {'defaultTimezone': '<Country/Location>'}"},
                status_code=400,
            )
        mock.schedule_timezone = dict(body)
        return Response(status_code=204)

    # -- end common settings (#38) --------------------------------------------

    # -- priorities (#36) -----------------------------------------------------
    #
    # Both are orchestrator-scope singletons written by full overwrite.

    @api.get("/gms/overlays/priority")
    async def get_overlay_priority() -> Any:
        return mock.overlay_priority

    @api.post("/gms/overlays/priority")
    async def replace_overlay_priority(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must map priority -> overlay id"}, status_code=400
            )
        # Real-Orchestrator rule (vendored SDK: "each overlay ID must have a
        # unique priority") — modeled so a resource that skipped its
        # pre-flight validation would get the 4xx it deserves here.
        seen: dict[str, str] = {}
        for priority, overlay_id in body.items():
            key = str(overlay_id)
            if key in seen:
                return JSONResponse(
                    {
                        "error": (
                            f"overlay {key} is assigned priorities {seen[key]} and "
                            f"{priority}; each overlay must have a unique priority"
                        )
                    },
                    status_code=400,
                )
            seen[key] = str(priority)
        mock.overlay_priority = {str(priority): value for priority, value in body.items()}
        return Response(status_code=204)

    @api.get("/template/templateGroupsPriorities")
    async def get_template_group_priorities() -> Any:
        return {"priorities": list(mock.template_group_priorities)}

    @api.post("/template/templateGroupsPriorities")
    async def replace_template_group_priorities(request: Request) -> Response:
        body = await _json_body(request)
        order = body.get("priorities") if isinstance(body, dict) else None
        if not isinstance(order, list):
            return JSONResponse(
                {"error": "body must carry a 'priorities' list of group names"},
                status_code=400,
            )
        # Order is the payload: stored verbatim, never sorted.
        mock.template_group_priorities = [str(name) for name in order]
        return Response(status_code=204)

    # -- end priorities (#36) -------------------------------------------------
    # -- regions (#35) --------------------------------------------------------

    def _region_record(region_id: int) -> dict[str, Any] | None:
        for region in mock.regions:
            if int(region["regionId"]) == region_id:
                return region
        return None

    def _association(ne_pk: str) -> dict[str, Any]:
        region_id = int(mock.region_appliances.get(ne_pk, 0))
        record = _region_record(region_id)
        return {
            "nePk": ne_pk,
            "regionId": region_id,
            "regionName": str(record["regionName"]) if record else "",
        }

    @api.get("/regions")
    async def get_regions(regionId: int | None = None) -> Any:
        if regionId is None:
            return mock.regions
        return [r for r in mock.regions if int(r["regionId"]) == regionId]

    @api.post("/regions")
    async def create_region(request: Request) -> Any:
        body = await _json_body(request)
        if not isinstance(body, dict) or not body.get("regionName"):
            return JSONResponse({"error": "body must be {regionName: ...}"}, status_code=400)
        region_id = mock.regions_next_id
        mock.regions_next_id += 1
        # Unknown fields pass through, like every other handler here.
        mock.regions.append({**body, "regionId": region_id, "regionName": str(body["regionName"])})
        return JSONResponse({"regionId": region_id}, status_code=200)

    @api.put("/regions")
    async def update_region(request: Request, regionId: int) -> Any:
        record = _region_record(regionId)
        if record is None:
            return JSONResponse({"error": f"no region {regionId}"}, status_code=404)
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a region object"}, status_code=400)
        record.update({**body, "regionId": regionId})
        return JSONResponse(record, status_code=200)

    @api.delete("/regions")
    async def delete_region(regionId: int) -> Response:
        if regionId == 0:
            return JSONResponse(
                {"error": "the global region (regionId 0) cannot be deleted"}, status_code=400
            )
        record = _region_record(regionId)
        if record is None:
            return JSONResponse({"error": f"no region {regionId}"}, status_code=404)
        mock.regions = [r for r in mock.regions if int(r["regionId"]) != regionId]
        # Appliances fall back to the global region; regional overlay entries
        # for the region go with it.
        for ne_pk, assigned in list(mock.region_appliances.items()):
            if int(assigned) == regionId:
                mock.region_appliances[ne_pk] = 0
        for by_region in mock.regional_overlays.values():
            by_region.pop(str(regionId), None)
        return Response(status_code=204)

    @api.get("/regions/appliances/regionId")
    async def get_region_appliances_by_region(regionId: int) -> Any:
        return [
            _association(ne_pk)
            for ne_pk, assigned in sorted(mock.region_appliances.items())
            if int(assigned) == regionId
        ]

    @api.get("/regions/appliances")
    async def get_region_appliances(nePk: str | None = None) -> Any:
        if nePk is None:
            return [_association(pk) for pk in sorted(mock.region_appliances)]
        if nePk not in mock.region_appliances:
            return JSONResponse({"error": f"no appliance {nePk!r}"}, status_code=404)
        return _association(nePk)

    @api.put("/regions/appliances")
    async def update_region_association(request: Request, nePk: str) -> Any:
        body = await _json_body(request)
        if not isinstance(body, dict) or "regionId" not in body:
            return JSONResponse({"error": "body must be {regionId: int}"}, status_code=400)
        region_id = int(body["regionId"])
        if _region_record(region_id) is None:
            return JSONResponse({"error": f"no region {region_id}"}, status_code=404)
        mock.region_appliances[nePk] = region_id
        return Response(status_code=204)

    @api.post("/regions/appliances")
    async def create_region_associations(request: Request) -> Any:
        body = await _json_body(request)
        if not isinstance(body, list):
            return JSONResponse(
                {"error": "body must be [{nePk, regionId}, ...]"}, status_code=400
            )
        for entry in body:
            if not isinstance(entry, dict) or "nePk" not in entry or "regionId" not in entry:
                continue
            mock.region_appliances[str(entry["nePk"])] = int(entry["regionId"])
        return Response(status_code=204)

    @api.get("/gms/overlays/config/regions")
    async def get_regional_overlays(
        overlayId: str | None = None, regionId: str | None = None
    ) -> Any:
        out: dict[str, dict[str, Any]] = {}
        for oid, by_region in mock.regional_overlays.items():
            if overlayId is not None and str(overlayId) != oid:
                continue
            entries = {
                rid: cfg
                for rid, cfg in by_region.items()
                if regionId is None or str(regionId) == rid
            }
            if entries:
                out[oid] = entries
        return out

    @api.put("/gms/overlays/config/regions")
    async def modify_regional_overlays(request: Request) -> Response:
        """Region-scoped update: only the (overlayId, regionId) pairs named in
        the body are replaced; every other entry is left untouched. This is the
        merge reading of the spec's "update an existing overlay configuration"
        — resources/regions.py deliberately sends the whole merged table so it
        stays correct under the replace reading too (see its module docstring).
        """
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must map overlayId -> {regionId: config}"}, status_code=400
            )
        for overlay_id, by_region in body.items():
            if not isinstance(by_region, dict):
                continue
            target = mock.regional_overlays.setdefault(str(overlay_id), {})
            for region_id, config in by_region.items():
                target[str(region_id)] = config
        return Response(status_code=204)

    # -- end regions (#35) ----------------------------------------------------

    # -- acls / ipObjects / appExpress (#31) ----------------------------------
    #
    # Orchestrator-scope half of #31. (The ACL half is appliance-scope and is
    # served through the /appliance/rest proxy above — see _acls_dispatch.)
    # These shapes are spec-derived: all four collections were empty on the
    # live lab, so the mock follows specs/orchestrator-openapi-7.2.0.json.

    def _ip_object_get(store: dict[str, Any], name: str | None) -> Any:
        if name is None:
            return list(store.values())
        record = store.get(name)
        # GET ?name= is typed as a single object; an unknown name yields the
        # empty object the resource reads as "absent".
        return record if record is not None else {}

    def _ip_object_write(store: dict[str, Any], body: Any, type_code: str) -> Response:
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a group object"}, status_code=400)
        name = body.get("name")
        if not name:
            return JSONResponse({"error": "group body must carry 'name'"}, status_code=400)
        if body.get("type") != type_code:
            return JSONResponse(
                {"error": f"group 'type' must be {type_code!r}"}, status_code=400
            )
        rules = body.get("rules")
        if not isinstance(rules, list):
            return JSONResponse({"error": "group body must carry 'rules'"}, status_code=400)
        store[str(name)] = copy.deepcopy(body)
        return Response(status_code=204)

    @api.get("/ipObjects/addressGroup")
    async def get_address_groups(name: str | None = None) -> Any:
        return _ip_object_get(mock.address_groups, name)

    @api.api_route("/ipObjects/addressGroup", methods=["POST", "PUT"])
    async def write_address_group(request: Request) -> Response:
        return _ip_object_write(mock.address_groups, await _json_body(request), "AG")

    @api.delete("/ipObjects/addressGroup")
    async def delete_address_group(name: str) -> Response:
        mock.address_groups.pop(name, None)
        return Response(status_code=204)

    @api.get("/ipObjects/serviceGroup")
    async def get_service_groups(name: str | None = None) -> Any:
        return _ip_object_get(mock.service_groups, name)

    @api.api_route("/ipObjects/serviceGroup", methods=["POST", "PUT"])
    async def write_service_group(request: Request) -> Response:
        return _ip_object_write(mock.service_groups, await _json_body(request), "SG")

    @api.delete("/ipObjects/serviceGroup")
    async def delete_service_group(name: str) -> Response:
        mock.service_groups.pop(name, None)
        return Response(status_code=204)

    @api.get("/applicationDefinition/appExpressGroup/config")
    async def get_app_express_groups() -> Any:
        return mock.app_express_groups

    @api.post("/applicationDefinition/appExpressGroup/config")
    async def set_app_express_group(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict) or not body.get("name"):
            return JSONResponse(
                {"error": "appExpress group body must carry 'name'"}, status_code=400
            )
        # The real API keys the table by the lower-cased group name; POST
        # edits exactly one group and leaves the rest alone.
        mock.app_express_groups[str(body["name"]).lower()] = copy.deepcopy(body)
        return Response(status_code=204)

    @api.delete("/applicationDefinition/appExpressGroup/config")
    async def delete_app_express_group(groupName: str) -> Response:
        mock.app_express_groups.pop(groupName.lower(), None)
        mock.app_express_associations = [
            a
            for a in mock.app_express_associations
            if str(a.get("appExpressGroupName", "")).lower() != groupName.lower()
        ]
        return Response(status_code=204)

    @api.get("/applicationDefinition/appExpressGroup/association")
    async def get_app_express_associations(groupName: str | None = None) -> Any:
        if groupName is None:
            return mock.app_express_associations
        return [
            a
            for a in mock.app_express_associations
            if str(a.get("appExpressGroupName", "")) == groupName
        ]

    @api.post("/applicationDefinition/appExpressGroup/association")
    async def set_app_express_associations(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, list):
            return JSONResponse(
                {"error": "association body must be the complete array"}, status_code=400
            )
        mock.app_express_associations = copy.deepcopy(body)
        return Response(status_code=204)

    # -- end acls / ipObjects / appExpress (#31) ------------------------------

    # -- catch-all (must be registered last on this router) -----------------

    @api.api_route("/{_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def catch_all(_path: str) -> Any:
        return JSONResponse({"error": "no such endpoint in mock"}, status_code=404)

    app.include_router(auth)
    app.include_router(api)
    return app


# -- thread runner -----------------------------------------------------------


def run_in_thread(port: int = 0) -> tuple[str, MockState, Callable[[], None]]:
    """Start the mock in a daemon thread on 127.0.0.1; return (base_url, state, shutdown).

    ``port=0`` (the default) binds an ephemeral port; the returned ``base_url``
    carries the real port. Works with a plain sync httpx client. Call the
    returned ``shutdown()`` to stop the server and join the thread.
    """
    state = MockState()
    app = create_app(state)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="pyecsdwan-mock", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("mock server thread exited before startup completed")
        if time.monotonic() > deadline:
            raise RuntimeError("mock server did not start within 15s")
        time.sleep(0.01)
    sockets = server.servers[0].sockets
    real_port = sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{real_port}"

    def shutdown() -> None:
        server.should_exit = True
        thread.join(timeout=10.0)

    return base_url, state, shutdown
