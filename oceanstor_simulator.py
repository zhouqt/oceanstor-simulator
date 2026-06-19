#!/usr/bin/env python3
"""Dummy Huawei OceanStor REST API simulator for testing charm-cinder-oceanstor.

Implements the REST API surface that the Cinder HuaweiISCSIDriver expects,
backed by in-memory state. Runs as a self-signed HTTPS server.

Usage:
    python3 oceanstor_simulator.py --port 8088 --pool OpenStack_Pool
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

LOG = logging.getLogger("oceanstor-sim")

DEVICE_ID = "210235G7J20000000000"
TOKEN = "simulator-token-001"
PRODUCT_NAME = "Dorado"
PRODUCT_VERSION = "V300R006C30"

CAPACITY_10TB_SECTORS = str(10 * 1024 * 1024 * 1024 * 2)  # 10TB in 512-byte sectors


class StorageState:
    """In-memory storage state."""

    def __init__(self, pool_names, target_ip):
        self._lock = threading.Lock()
        self._next_id = 100
        self.target_ip = target_ip

        self.pools = {}
        for i, name in enumerate(pool_names):
            self.pools[str(i)] = {
                "ID": str(i),
                "NAME": name,
                "USAGETYPE": "1",
                "USERFREECAPACITY": CAPACITY_10TB_SECTORS,
                "USERTOTALCAPACITY": CAPACITY_10TB_SECTORS,
                "DATASPACE": CAPACITY_10TB_SECTORS,
                "TIER0CAPACITY": "100",
                "TIER1CAPACITY": "0",
                "TIER2CAPACITY": "0",
            }

        self.luns = {}
        self.snapshots = {}
        self.hosts = {}
        self.hostgroups = {}
        self.lungroups = {}
        self.mappingviews = {}
        self.portgroups = {}
        self.iscsi_initiators = {}
        self.lun_copies = {}
        self.qos_policies = {}

        self.hostgroup_hosts = {}
        self.lungroup_luns = {}
        self.view_associations = {}

        self.tgt_lun_map = {}
        self.iscsi_mgr = None

        default_pg_id = self._alloc_id()
        self.portgroups[default_pg_id] = {
            "ID": default_pg_id,
            "NAME": "default_iscsi_portgroup",
            "TYPE": "257",
        }

    def _alloc_id(self):
        with self._lock:
            rid = str(self._next_id)
            self._next_id += 1
            return rid


class ISCSITargetManager:
    """Manages a real iSCSI target via tgtadm for exposing LUN backing files."""

    TID = 1

    def __init__(self, target_ip, volume_dir="/app/volumes"):
        self.target_ip = target_ip
        self.target_iqn = f"iqn.2006-08.com.huawei:oceanstor:simulator:0001:{target_ip}"
        self.volume_dir = volume_dir
        self._lock = threading.Lock()
        self._next_tgt_lun = 1
        self._lun_tgt_map = {}

    def start(self):
        os.makedirs(self.volume_dir, exist_ok=True)
        self._run(["tgtadm", "--lld", "iscsi", "--op", "new",
                   "--mode", "target", "--tid", str(self.TID),
                   "-T", self.target_iqn])
        self._run(["tgtadm", "--lld", "iscsi", "--op", "bind",
                   "--mode", "target", "--tid", str(self.TID), "-I", "ALL"])

    def create_backing_file(self, lun_id, size_sectors):
        path = os.path.join(self.volume_dir, f"lun-{lun_id}.img")
        size_bytes = int(size_sectors) * 512
        with open(path, "wb") as f:
            f.truncate(size_bytes)
        LOG.info("Created backing file %s (%d bytes)", path, size_bytes)
        return path

    def delete_backing_file(self, lun_id):
        path = os.path.join(self.volume_dir, f"lun-{lun_id}.img")
        if os.path.exists(path):
            os.unlink(path)
            LOG.info("Deleted backing file %s", path)

    def expand_backing_file(self, lun_id, new_size_sectors):
        path = os.path.join(self.volume_dir, f"lun-{lun_id}.img")
        size_bytes = int(new_size_sectors) * 512
        with open(path, "ab") as f:
            f.truncate(size_bytes)
        LOG.info("Expanded backing file %s to %d bytes", path, size_bytes)

    def expose_lun(self, lun_id):
        with self._lock:
            if lun_id in self._lun_tgt_map:
                return self._lun_tgt_map[lun_id]
            lun_num = self._next_tgt_lun
            self._next_tgt_lun += 1
        path = os.path.join(self.volume_dir, f"lun-{lun_id}.img")
        self._run(["tgtadm", "--lld", "iscsi", "--op", "new",
                   "--mode", "logicalunit", "--tid", str(self.TID),
                   "--lun", str(lun_num), "-b", path])
        with self._lock:
            self._lun_tgt_map[lun_id] = lun_num
        LOG.info("Exposed LUN %s as tgt lun %d", lun_id, lun_num)
        return lun_num

    def unexpose_lun(self, lun_id):
        with self._lock:
            lun_num = self._lun_tgt_map.pop(lun_id, None)
        if lun_num is not None:
            self._run(["tgtadm", "--lld", "iscsi", "--op", "delete",
                       "--mode", "logicalunit", "--tid", str(self.TID),
                       "--lun", str(lun_num)])
            LOG.info("Unexposed LUN %s (tgt lun %d)", lun_id, lun_num)

    def get_tgt_lun_num(self, lun_id):
        return self._lun_tgt_map.get(lun_id)

    def _run(self, cmd):
        LOG.debug("tgtadm: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            LOG.error("tgtadm failed (rc=%d): %s", result.returncode, result.stderr.strip())
        return result


STATE: StorageState = None


def success(data=None):
    resp = {"error": {"code": 0, "description": "success"}}
    if data is not None:
        resp["data"] = data
    return resp


def error(code, desc="error"):
    return {"error": {"code": code, "description": desc}}


def _make_wwn(lun_id):
    h = hashlib.md5(f"lun-{lun_id}".encode()).hexdigest()
    return f"6643e8c1004c5f67{h[:16]}"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_login(method, path, body, qs):
    if method != "POST":
        return error(-1, "Method not allowed")
    return success({
        "username": body.get("username", "admin"),
        "iBaseToken": TOKEN,
        "deviceid": DEVICE_ID,
        "accountstate": 2,
    })


def handle_logout(method, path, body, qs):
    return success()


def handle_system(method, path, body, qs):
    return success({
        "ID": DEVICE_ID,
        "NAME": "OceanStor-Simulator",
        "PRODUCTNAME": PRODUCT_NAME,
        "PRODUCTVERSION": PRODUCT_VERSION,
        "wwn": "2102350BSH10H2000000",
        "HEALTHSTATUS": "1",
        "RUNNINGSTATUS": "27",
    })


# --- Storage Pools ---

def handle_storagepool(method, path, body, qs):
    return success(list(STATE.pools.values()))


# --- LUN ---

def handle_lun(method, path, body, qs):
    parts = path.split("/")

    if method == "POST":
        lun_id = STATE._alloc_id()
        name = body.get("NAME", f"lun-{lun_id}")
        parent_id = body.get("PARENTID", "0")
        pool_name = STATE.pools.get(parent_id, {}).get("NAME", "unknown")
        capacity = body.get("CAPACITY", "1048576")
        alloc_type = body.get("ALLOCTYPE", "1")
        lun = {
            "ID": lun_id,
            "NAME": name,
            "WWN": _make_wwn(lun_id),
            "CAPACITY": str(capacity),
            "ALLOCTYPE": str(alloc_type),
            "HEALTHSTATUS": "1",
            "RUNNINGSTATUS": "27",
            "PARENTID": parent_id,
            "PARENTNAME": pool_name,
            "DESCRIPTION": body.get("DESCRIPTION", ""),
            "IOCLASSID": "",
            "LUNTYPE": body.get("LUNTYPE", "11"),
            "MIRRORPOLICY": body.get("MIRRORPOLICY", "1"),
            "ISADD2LUNGROUP": "false",
        }
        STATE.luns[lun_id] = lun
        if STATE.iscsi_mgr:
            STATE.iscsi_mgr.create_backing_file(lun_id, capacity)
        LOG.info("Created LUN %s (name=%s)", lun_id, name)
        return success(lun)

    filter_val = qs.get("filter", [""])[0]
    if filter_val and "NAME::" in filter_val:
        name_filter = filter_val.replace("NAME::", "")
        matches = [l for l in STATE.luns.values() if l["NAME"] == name_filter]
        return success(matches)

    if "/lun/count" in path:
        assoc_id = qs.get("ASSOCIATEOBJID", [""])[0]
        lun_ids = STATE.lungroup_luns.get(assoc_id, set())
        return success({"COUNT": str(len(lun_ids))})

    if "/lun/associate" in path:
        assoc_obj_type = qs.get("ASSOCIATEOBJTYPE", [""])[0]
        assoc_id = qs.get("ASSOCIATEOBJID", [""])[0]
        if assoc_obj_type == "21":
            result_luns = []
            for view_id, va in STATE.view_associations.items():
                hg_id = va.get("hostgroup_id")
                lg_id = va.get("lungroup_id")
                if not hg_id or not lg_id:
                    continue
                if assoc_id not in STATE.hostgroup_hosts.get(hg_id, set()):
                    continue
                for lid in STATE.lungroup_luns.get(lg_id, set()):
                    if lid in STATE.luns:
                        lun_copy = dict(STATE.luns[lid])
                        host_lun_id = STATE.tgt_lun_map.get(lid, 1)
                        lun_copy["ASSOCIATEMETADATA"] = json.dumps({"HostLUNID": host_lun_id})
                        result_luns.append(lun_copy)
            return success(result_luns)
        lun_ids = STATE.lungroup_luns.get(assoc_id, set())
        return success([STATE.luns[lid] for lid in lun_ids if lid in STATE.luns])

    if "/lun/expand" in path and method == "PUT":
        lun_id = body.get("ID", "")
        if lun_id in STATE.luns:
            new_capacity = body.get("CAPACITY", STATE.luns[lun_id]["CAPACITY"])
            STATE.luns[lun_id]["CAPACITY"] = str(new_capacity)
            if STATE.iscsi_mgr:
                STATE.iscsi_mgr.expand_backing_file(lun_id, new_capacity)
            return success(STATE.luns[lun_id])
        return error(-1, "LUN not found")

    lun_id = parts[-1] if len(parts) >= 2 and parts[-1] != "lun" else None
    if lun_id and lun_id in STATE.luns:
        if method == "GET":
            return success(STATE.luns[lun_id])
        if method == "DELETE":
            if STATE.iscsi_mgr:
                STATE.iscsi_mgr.unexpose_lun(lun_id)
                STATE.iscsi_mgr.delete_backing_file(lun_id)
            STATE.tgt_lun_map.pop(lun_id, None)
            del STATE.luns[lun_id]
            LOG.info("Deleted LUN %s", lun_id)
            return success()
        if method == "PUT":
            lun = STATE.luns[lun_id]
            if "NAME" in body:
                lun["NAME"] = body["NAME"]
            if "DESCRIPTION" in body:
                lun["DESCRIPTION"] = body["DESCRIPTION"]
            if "CAPACITY" in body:
                lun["CAPACITY"] = str(body["CAPACITY"])
            return success(lun)

    if method == "GET" and lun_id and lun_id not in STATE.luns:
        return error(-1, "LUN not found")

    return success([])


# --- Snapshot ---

def handle_snapshot(method, path, body, qs):
    parts = path.split("/")

    if "/snapshot/count" in path and method == "GET":
        return success({"COUNT": "0"})

    if "/snapshot/activate" in path and method == "POST":
        return success()

    if "/snapshot/stop" in path and method == "PUT":
        snap_id = body.get("ID", "")
        if snap_id in STATE.snapshots:
            STATE.snapshots[snap_id]["RUNNINGSTATUS"] = "43"
        return success()

    if method == "POST" and path.rstrip("/").endswith("/snapshot"):
        snap_id = STATE._alloc_id()
        snap = {
            "ID": snap_id,
            "NAME": body.get("NAME", f"snap-{snap_id}"),
            "PARENTID": body.get("PARENTID", ""),
            "HEALTHSTATUS": "1",
            "RUNNINGSTATUS": "27",
            "DESCRIPTION": body.get("DESCRIPTION", ""),
        }
        STATE.snapshots[snap_id] = snap
        LOG.info("Created snapshot %s", snap_id)
        return success(snap)

    filter_val = qs.get("filter", [""])[0]
    if filter_val and "NAME::" in filter_val:
        name_filter = filter_val.replace("NAME::", "")
        matches = [s for s in STATE.snapshots.values() if s["NAME"] == name_filter]
        return success(matches)

    snap_id = parts[-1] if len(parts) >= 2 and parts[-1] != "snapshot" else None
    if snap_id and snap_id in STATE.snapshots:
        if method == "GET":
            return success(STATE.snapshots[snap_id])
        if method == "DELETE":
            del STATE.snapshots[snap_id]
            LOG.info("Deleted snapshot %s", snap_id)
            return success()
        if method == "PUT":
            snap = STATE.snapshots[snap_id]
            if "NAME" in body:
                snap["NAME"] = body["NAME"]
            if "DESCRIPTION" in body:
                snap["DESCRIPTION"] = body["DESCRIPTION"]
            return success(snap)

    if method == "GET" and snap_id and snap_id not in STATE.snapshots:
        return error(-1, "Snapshot not found")

    return success([])


# --- Host ---

def handle_host(method, path, body, qs):
    parts = path.split("/")

    if "/host/associate" in path:
        assoc_type = qs.get("ASSOCIATEOBJTYPE", [""])[0]
        assoc_id = qs.get("ASSOCIATEOBJID", [""])[0]
        if assoc_type == "14":
            host_ids = STATE.hostgroup_hosts.get(assoc_id, set())
            return success([STATE.hosts[hid] for hid in host_ids if hid in STATE.hosts])
        return success([])

    if method == "POST":
        host_id = STATE._alloc_id()
        host = {
            "ID": host_id,
            "NAME": body.get("NAME", f"host-{host_id}"),
            "OPERATIONSYSTEM": body.get("OPERATIONSYSTEM", "0"),
            "DESCRIPTION": body.get("DESCRIPTION", ""),
            "HEALTHSTATUS": "1",
            "RUNNINGSTATUS": "27",
            "ISFREE": "true",
            "ISADD2HOSTGROUP": "false",
            "INITIATORNUM": "0",
        }
        STATE.hosts[host_id] = host
        LOG.info("Created host %s", host_id)
        return success(host)

    filter_val = qs.get("filter", [""])[0]
    if filter_val and "NAME::" in filter_val:
        name_filter = filter_val.replace("NAME::", "")
        matches = [h for h in STATE.hosts.values() if h["NAME"] == name_filter]
        return success(matches)

    host_id = parts[-1] if len(parts) >= 2 and parts[-1] != "host" else None
    if host_id and host_id in STATE.hosts:
        if method == "GET":
            return success(STATE.hosts[host_id])
        if method == "DELETE":
            del STATE.hosts[host_id]
            LOG.info("Deleted host %s", host_id)
            return success()
        if method == "PUT":
            host = STATE.hosts[host_id]
            for k in ("NAME", "DESCRIPTION", "OPERATIONSYSTEM"):
                if k in body:
                    host[k] = body[k]
            return success(host)

    if method == "GET" and host_id and host_id not in STATE.hosts:
        return error(-1, "Host not found")

    return success([])


# --- Host Group ---

def handle_hostgroup(method, path, body, qs):
    if "/hostgroup/associate" in path:
        if method == "POST":
            hg_id = body.get("ID", "")
            host_id = body.get("ASSOCIATEOBJID", "")
            STATE.hostgroup_hosts.setdefault(hg_id, set()).add(host_id)
            if host_id in STATE.hosts:
                STATE.hosts[host_id]["ISADD2HOSTGROUP"] = "true"
            LOG.info("Associated host %s to hostgroup %s", host_id, hg_id)
            return success()
        if method == "DELETE":
            hg_id = qs.get("ID", [""])[0]
            host_id = qs.get("ASSOCIATEOBJID", [""])[0]
            hosts = STATE.hostgroup_hosts.get(hg_id, set())
            hosts.discard(host_id)
            if host_id in STATE.hosts:
                STATE.hosts[host_id]["ISADD2HOSTGROUP"] = "false"
            return success()

    parts = path.split("/")

    if method == "POST":
        hg_id = STATE._alloc_id()
        hg = {
            "ID": hg_id,
            "NAME": body.get("NAME", f"hostgroup-{hg_id}"),
            "TYPE": "14",
        }
        STATE.hostgroups[hg_id] = hg
        STATE.hostgroup_hosts[hg_id] = set()
        LOG.info("Created hostgroup %s", hg_id)
        return success(hg)

    if method == "GET":
        return success(list(STATE.hostgroups.values()))

    hg_id = parts[-1] if len(parts) >= 2 and parts[-1] != "hostgroup" else None
    if hg_id and hg_id in STATE.hostgroups:
        if method == "DELETE":
            del STATE.hostgroups[hg_id]
            STATE.hostgroup_hosts.pop(hg_id, None)
            LOG.info("Deleted hostgroup %s", hg_id)
            return success()

    return success([])


# --- LUN Group ---

def handle_lungroup(method, path, body, qs):
    if "/lungroup/associate" in path:
        if method == "POST":
            lg_id = body.get("ID", "")
            lun_id = body.get("ASSOCIATEOBJID", "")
            STATE.lungroup_luns.setdefault(lg_id, set()).add(lun_id)
            if lun_id in STATE.luns:
                STATE.luns[lun_id]["ISADD2LUNGROUP"] = "true"
            if STATE.iscsi_mgr:
                tgt_lun_num = STATE.iscsi_mgr.expose_lun(lun_id)
                STATE.tgt_lun_map[lun_id] = tgt_lun_num
            LOG.info("Associated LUN %s to lungroup %s", lun_id, lg_id)
            return success()
        if method == "DELETE":
            lg_id = qs.get("ID", [""])[0]
            lun_id = qs.get("ASSOCIATEOBJID", [""])[0]
            luns = STATE.lungroup_luns.get(lg_id, set())
            luns.discard(lun_id)
            if lun_id in STATE.luns:
                STATE.luns[lun_id]["ISADD2LUNGROUP"] = "false"
            if STATE.iscsi_mgr:
                STATE.iscsi_mgr.unexpose_lun(lun_id)
                STATE.tgt_lun_map.pop(lun_id, None)
            return success()

    path_upper = path.upper()
    parts = path.split("/")

    if method == "POST":
        lg_id = STATE._alloc_id()
        lg = {
            "ID": lg_id,
            "NAME": body.get("NAME", f"lungroup-{lg_id}"),
            "TYPE": "256",
            "DESCRIPTION": body.get("DESCRIPTION", ""),
        }
        STATE.lungroups[lg_id] = lg
        STATE.lungroup_luns[lg_id] = set()
        LOG.info("Created lungroup %s", lg_id)
        return success(lg)

    if method == "GET":
        return success(list(STATE.lungroups.values()))

    lg_id = parts[-1] if len(parts) >= 2 else None
    if lg_id and lg_id in STATE.lungroups:
        if method == "DELETE":
            del STATE.lungroups[lg_id]
            STATE.lungroup_luns.pop(lg_id, None)
            LOG.info("Deleted lungroup %s", lg_id)
            return success()

    return success([])


# --- Mapping View ---

def handle_mappingview(method, path, body, qs):
    path_upper = path.upper()

    if "MAPPINGVIEW/CREATE_ASSOCIATE" in path_upper and method == "PUT":
        view_id = body.get("ID", "")
        assoc_type = body.get("ASSOCIATEOBJTYPE", "")
        assoc_id = body.get("ASSOCIATEOBJID", "")
        va = STATE.view_associations.setdefault(view_id, {})
        if str(assoc_type) == "14":
            va["hostgroup_id"] = assoc_id
        elif str(assoc_type) == "256":
            va["lungroup_id"] = assoc_id
        elif str(assoc_type) == "257":
            va["portgroup_id"] = assoc_id
        LOG.info("Associated type=%s id=%s to mappingview %s", assoc_type, assoc_id, view_id)
        return success()

    if "MAPPINGVIEW/REMOVE_ASSOCIATE" in path_upper and method == "PUT":
        view_id = body.get("ID", "")
        assoc_type = body.get("ASSOCIATEOBJTYPE", "")
        va = STATE.view_associations.get(view_id, {})
        if str(assoc_type) == "14":
            va.pop("hostgroup_id", None)
        elif str(assoc_type) == "256":
            va.pop("lungroup_id", None)
        elif str(assoc_type) == "257":
            va.pop("portgroup_id", None)
        return success()

    if "/mappingview/associate" in path:
        assoc_type = qs.get("ASSOCIATEOBJTYPE", [""])[0]
        assoc_id = qs.get("ASSOCIATEOBJID", [""])[0]
        matches = []
        for vid, va in STATE.view_associations.items():
            if str(assoc_type) == "14" and va.get("hostgroup_id") == assoc_id:
                if vid in STATE.mappingviews:
                    matches.append(STATE.mappingviews[vid])
            elif str(assoc_type) == "256" and va.get("lungroup_id") == assoc_id:
                if vid in STATE.mappingviews:
                    matches.append(STATE.mappingviews[vid])
            elif str(assoc_type) == "257" and va.get("portgroup_id") == assoc_id:
                if vid in STATE.mappingviews:
                    matches.append(STATE.mappingviews[vid])
        return success(matches)

    parts = path.split("/")

    if method == "POST":
        mv_id = STATE._alloc_id()
        mv = {
            "ID": mv_id,
            "NAME": body.get("NAME", f"mappingview-{mv_id}"),
            "TYPE": "245",
            "AVAILABLEHOSTLUNIDLIST": json.dumps(list(range(0, 512))),
        }
        STATE.mappingviews[mv_id] = mv
        STATE.view_associations[mv_id] = {}
        LOG.info("Created mappingview %s", mv_id)
        return success(mv)

    if method == "GET":
        mv_id = parts[-1] if len(parts) >= 2 and parts[-1] != "mappingview" else None
        if mv_id and mv_id in STATE.mappingviews:
            return success(STATE.mappingviews[mv_id])
        return success(list(STATE.mappingviews.values()))

    if method == "PUT":
        mv_id = body.get("ID", "")
        if mv_id in STATE.mappingviews:
            return success(STATE.mappingviews[mv_id])
        return success()

    mv_id = parts[-1] if len(parts) >= 2 else None
    if mv_id and mv_id in STATE.mappingviews:
        if method == "DELETE":
            del STATE.mappingviews[mv_id]
            STATE.view_associations.pop(mv_id, None)
            LOG.info("Deleted mappingview %s", mv_id)
            return success()

    return success([])


# --- iSCSI ---

def handle_iscsi_initiator(method, path, body, qs):
    parts = path.split("/")

    if "/iscsi_initiator/remove_iscsi_from_host" in path and method == "POST":
        return success()

    if method == "POST" and path.rstrip("/").endswith("/iscsi_initiator"):
        iqn = body.get("ID", "")
        initiator = {
            "ID": iqn,
            "TYPE": "222",
            "ISFREE": "true",
            "USECHAP": body.get("USECHAP", "false"),
            "PARENTTYPE": "",
            "PARENTID": "",
            "PARENTNAME": "",
            "RUNNINGSTATUS": "27",
            "HEALTHSTATUS": "1",
        }
        STATE.iscsi_initiators[iqn] = initiator
        LOG.info("Added iSCSI initiator %s", iqn)
        return success(initiator)

    if method == "PUT":
        iqn = parts[-1] if len(parts) >= 2 and parts[-1] != "iscsi_initiator" else body.get("ID", "")
        if iqn in STATE.iscsi_initiators:
            ini = STATE.iscsi_initiators[iqn]
            if "PARENTID" in body:
                ini["PARENTID"] = body["PARENTID"]
                ini["PARENTTYPE"] = body.get("PARENTTYPE", "21")
                ini["PARENTNAME"] = STATE.hosts.get(body["PARENTID"], {}).get("NAME", "")
                ini["ISFREE"] = "false"
            if "USECHAP" in body:
                ini["USECHAP"] = body["USECHAP"]
            if "MULTIPATHTYPE" in body:
                ini["MULTIPATHTYPE"] = body["MULTIPATHTYPE"]
            return success(ini)
        if iqn:
            parent_id = body.get("PARENTID", "")
            initiator = {
                "ID": iqn,
                "TYPE": "222",
                "ISFREE": "false",
                "USECHAP": body.get("USECHAP", "false"),
                "PARENTTYPE": body.get("PARENTTYPE", "21"),
                "PARENTID": parent_id,
                "PARENTNAME": STATE.hosts.get(parent_id, {}).get("NAME", ""),
                "RUNNINGSTATUS": "27",
                "HEALTHSTATUS": "1",
            }
            STATE.iscsi_initiators[iqn] = initiator
            return success(initiator)
        return error(-1, "Initiator not found")

    if method == "GET":
        parent_id = qs.get("PARENTID", [""])[0]
        if parent_id:
            matches = [i for i in STATE.iscsi_initiators.values() if i.get("PARENTID") == parent_id]
        else:
            matches = list(STATE.iscsi_initiators.values())
        return success(matches)

    return success([])


def handle_iscsidevicename(method, path, body, qs):
    return success([{
        "CMO_ISCSI_DEVICE_NAME": "iqn.2006-08.com.huawei:oceanstor:simulator:0001"
    }])


def handle_iscsi_tgt_port(method, path, body, qs):
    target_iqn = f"iqn.2006-08.com.huawei:oceanstor:simulator:0001:{STATE.target_ip}"
    return success([{
        "ID": f"0+{target_iqn},t,0x0001",
        "ETHPORTID": "CTE0.A.H0",
        "TYPE": "249",
        "TPGT": "1",
    }])


# --- Port Group ---

def handle_portgroup(method, path, body, qs):
    if "/portgroup/associate" in path:
        assoc_type = qs.get("ASSOCIATEOBJTYPE", [""])[0]
        assoc_id = qs.get("ASSOCIATEOBJID", [""])[0]
        if assoc_type == "245":
            va = STATE.view_associations.get(assoc_id, {})
            pg_id = va.get("portgroup_id")
            if pg_id and pg_id in STATE.portgroups:
                return success([STATE.portgroups[pg_id]])
        return success([])

    if method == "GET":
        return success(list(STATE.portgroups.values()))

    return success([])


def handle_portg(method, path, body, qs):
    if method == "POST":
        pg_id = STATE._alloc_id()
        pg = {
            "ID": pg_id,
            "NAME": body.get("NAME", f"portgroup-{pg_id}"),
            "DESCRIPTION": body.get("DESCRIPTION", ""),
            "TYPE": "257",
        }
        STATE.portgroups[pg_id] = pg
        LOG.info("Created portgroup %s", pg_id)
        return success(pg)
    return success([])


# --- LUN Copy ---

def handle_luncopy(method, path, body, qs):
    path_upper = path.upper()
    parts = path.split("/")

    if "/LUNCOPY/start" in path or "/luncopy/start" in path:
        if method == "PUT":
            copy_id = body.get("ID", "")
            if copy_id in STATE.lun_copies:
                STATE.lun_copies[copy_id]["HEALTHSTATUS"] = "1"
                STATE.lun_copies[copy_id]["RUNNINGSTATUS"] = "40"
                STATE.lun_copies[copy_id]["COPYPROGRESS"] = "100"
            return success()

    if method == "POST":
        copy_id = STATE._alloc_id()
        lc = {
            "ID": copy_id,
            "NAME": body.get("NAME", f"luncopy-{copy_id}"),
            "TYPE": "219",
            "HEALTHSTATUS": "1",
            "RUNNINGSTATUS": "36",
            "COPYPROGRESS": "0",
            "COPYSPEED": body.get("COPYSPEED", "2"),
            "SOURCELUN": body.get("SOURCELUN", ""),
            "TARGETLUN": body.get("TARGETLUN", ""),
        }
        STATE.lun_copies[copy_id] = lc
        LOG.info("Created luncopy %s", copy_id)
        return success(lc)

    if method == "GET":
        copy_id = parts[-1] if len(parts) >= 2 and not parts[-1].upper().startswith("LUNCOPY") else None
        if copy_id and copy_id in STATE.lun_copies:
            return success(STATE.lun_copies[copy_id])
        return success(list(STATE.lun_copies.values()))

    copy_id = parts[-1] if len(parts) >= 2 else None
    if copy_id and copy_id in STATE.lun_copies:
        if method == "DELETE":
            del STATE.lun_copies[copy_id]
            LOG.info("Deleted luncopy %s", copy_id)
            return success()

    return success([])


# --- QoS / IO Class ---

def handle_ioclass(method, path, body, qs):
    parts = path.split("/")

    if "/ioclass/active/" in path and method == "PUT":
        qos_id = parts[-1]
        if qos_id in STATE.qos_policies:
            STATE.qos_policies[qos_id]["ENABLESTATUS"] = str(body.get("ENABLESTATUS", True)).lower()
        return success()

    if method == "POST":
        qos_id = STATE._alloc_id()
        qos = {
            "ID": qos_id,
            "NAME": body.get("NAME", f"qos-{qos_id}"),
            "TYPE": "230",
            "LUNLIST": json.dumps(body.get("LUNLIST", [])),
            "ENABLESTATUS": "true",
        }
        for k in ("MAXIOPS", "MINIOPS", "MAXBANDWIDTH", "MINBANDWIDTH", "LATENCY", "IOTYPE"):
            if k in body:
                qos[k] = str(body[k])
        STATE.qos_policies[qos_id] = qos
        LOG.info("Created QoS policy %s", qos_id)
        return success(qos)

    qos_id = parts[-1] if len(parts) >= 2 and parts[-1] != "ioclass" else None
    if qos_id and qos_id in STATE.qos_policies:
        if method == "GET":
            return success(STATE.qos_policies[qos_id])
        if method == "PUT":
            qos = STATE.qos_policies[qos_id]
            for k in ("LUNLIST", "MAXIOPS", "MINIOPS", "MAXBANDWIDTH", "MINBANDWIDTH", "LATENCY", "IOTYPE"):
                if k in body:
                    if k == "LUNLIST":
                        qos[k] = json.dumps(body[k])
                    else:
                        qos[k] = str(body[k])
            return success(qos)
        if method == "DELETE":
            del STATE.qos_policies[qos_id]
            LOG.info("Deleted QoS policy %s", qos_id)
            return success()

    if method == "GET":
        return success(list(STATE.qos_policies.values()))

    return success([])


# --- Ethernet Port ---

def handle_eth_port(method, path, body, qs):
    return success([{
        "ID": "CTE0.A.H0",
        "NAME": "P0",
        "IPV4ADDR": STATE.target_ip,
        "IPV4MASK": "255.255.255.0",
        "HEALTHSTATUS": "1",
        "RUNNINGSTATUS": "27",
        "LOGICTYPE": "0",
    }])


# --- FC Initiator ---

def handle_fc_initiator(method, path, body, qs):
    return success([])


# --- Host Link ---

def handle_host_link(method, path, body, qs):
    return success([])


# --- Replication Pair ---

def handle_replicationpair(method, path, body, qs):
    parts = path.split("/")

    if method == "POST":
        pair_id = STATE._alloc_id()
        return success({
            "ID": pair_id,
            "NAME": body.get("NAME", f"pair-{pair_id}"),
            "RUNNINGSTATUS": "1",
            "HEALTHSTATUS": "1",
        })

    if method in ("PUT", "DELETE"):
        return success()

    pair_id = parts[-1] if len(parts) >= 2 else None
    if pair_id and method == "GET":
        return success({
            "ID": pair_id,
            "RUNNINGSTATUS": "1",
            "HEALTHSTATUS": "1",
        })

    return success([])


# --- HyperMetro ---

def handle_hypermetropair(method, path, body, qs):
    parts = path.split("/")

    if method == "POST":
        metro_id = STATE._alloc_id()
        return success({
            "ID": metro_id,
            "RUNNINGSTATUS": "1",
            "HEALTHSTATUS": "1",
        })

    if method in ("PUT", "DELETE"):
        return success()

    metro_id = parts[-1] if len(parts) >= 2 else None
    if metro_id and method == "GET":
        return success({
            "ID": metro_id,
            "RUNNINGSTATUS": "1",
            "HEALTHSTATUS": "1",
        })

    return success([])


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

ROUTE_TABLE = [
    (r".*/xx/sessions$", handle_login),
    (r".*/sessions$", handle_logout),
    (r".*/system/?$", handle_system),
    (r".*/storagepool", handle_storagepool),
    (r".*/eth_port", handle_eth_port),
    (r".*/fc_initiator", handle_fc_initiator),
    (r".*/host_link", handle_host_link),
    (r".*/iscsidevicename", handle_iscsidevicename),
    (r".*/iscsi_tgt_port", handle_iscsi_tgt_port),
    (r".*/iscsi_initiator", handle_iscsi_initiator),
    (r".*/snapshot/activate", handle_snapshot),
    (r".*/snapshot/stop", handle_snapshot),
    (r".*/snapshot", handle_snapshot),
    (r".*/lun/count", handle_lun),
    (r".*/lun/associate", handle_lun),
    (r".*/lun/expand", handle_lun),
    (r".*/lun(?:/[^/]+)?$", handle_lun),
    (r".*/host/associate", handle_host),
    (r".*/host(?:/[^/]+)?$", handle_host),
    (r".*/hostgroup/associate", handle_hostgroup),
    (r".*/hostgroup(?:/[^/]+)?$", handle_hostgroup),
    (r".*/lungroup/associate", handle_lungroup),
    (r".*(?:/lungroup|/LUNGroup)(?:/[^/]+)?$", handle_lungroup),
    (r".*/MAPPINGVIEW/CREATE_ASSOCIATE", handle_mappingview),
    (r".*/mappingview/REMOVE_ASSOCIATE", handle_mappingview),
    (r".*/mappingview/associate", handle_mappingview),
    (r".*/mappingview(?:/[^/]+)?$", handle_mappingview),
    (r".*/portgroup/associate", handle_portgroup),
    (r".*/portgroup", handle_portgroup),
    (r".*/portg$", handle_portg),
    (r".*(?:/LUNCOPY|/luncopy)(?:/[^/]+)?$", handle_luncopy),
    (r".*/ioclass/active/", handle_ioclass),
    (r".*/ioclass(?:/[^/]+)?$", handle_ioclass),
    (r".*(?:/REPLICATIONPAIR|/replicationpair)(?:/[^/]+)?$", handle_replicationpair),
    (r".*(?:/HyperMetroPair|/hypermetropair)(?:/[^/]+)?$", handle_hypermetropair),
]

COMPILED_ROUTES = [(re.compile(pattern, re.IGNORECASE), handler) for pattern, handler in ROUTE_TABLE]


def route(method, full_path, body, qs):
    path_no_qs = full_path.split("?")[0]
    for pattern, handler in COMPILED_ROUTES:
        if pattern.match(path_no_qs):
            return handler(method, full_path, body, qs)
    LOG.warning("Unmatched route: %s %s", method, full_path)
    return success({})


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class OceanStorHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        LOG.debug(fmt, *args)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
        return {}

    def _send_response(self, resp):
        body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        body = self._read_body() if method in ("POST", "PUT", "DELETE") else {}

        LOG.info("%s %s", method, path)
        if body:
            LOG.debug("Body: %s", json.dumps(body, indent=2))

        resp = route(method, path, body, qs)
        self._send_response(resp)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


# ---------------------------------------------------------------------------
# SSL Certificate Generation
# ---------------------------------------------------------------------------

def generate_self_signed_cert(cert_dir="/app/certs"):
    cert_file = os.path.join(cert_dir, "server.pem")
    key_file = os.path.join(cert_dir, "server.key")

    if os.path.exists(cert_file) and os.path.exists(key_file):
        LOG.info("Using existing SSL certificates in %s", cert_dir)
        return cert_file, key_file

    os.makedirs(cert_dir, exist_ok=True)

    LOG.info("Generating self-signed SSL certificate...")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", key_file, "-out", cert_file,
        "-days", "3650", "-nodes",
        "-subj", "/CN=OceanStor-Simulator/O=Simulator/C=CN",
    ], check=True, capture_output=True)

    LOG.info("SSL certificate generated: %s", cert_file)
    return cert_file, key_file


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Huawei OceanStor REST API Simulator")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8088, help="Port (default: 8088)")
    parser.add_argument("--pool", default=None, help="Pool name(s), semicolon-separated")
    parser.add_argument("--target-ip", default=None, help="iSCSI target IP to advertise")
    parser.add_argument("--volume-dir", default=None, help="Directory for LUN backing files")
    parser.add_argument("--cert-dir", default="/app/certs", help="Directory for SSL certs")
    parser.add_argument("--no-ssl", action="store_true", help="Run without HTTPS")
    parser.add_argument("--no-iscsi", action="store_true", help="Disable real iSCSI target")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    pool_names = (args.pool or os.environ.get("OCEANSTOR_POOL", "OpenStack_Pool")).split(";")
    target_ip = args.target_ip or os.environ.get("OCEANSTOR_TARGET_IP", args.host if args.host != "0.0.0.0" else "127.0.0.1")
    volume_dir = args.volume_dir or os.environ.get("OCEANSTOR_VOLUME_DIR", "/app/volumes")

    global STATE
    STATE = StorageState(pool_names, target_ip)

    enable_iscsi = not args.no_iscsi and os.environ.get("OCEANSTOR_ENABLE_ISCSI", "true").lower() == "true"
    if enable_iscsi:
        if shutil.which("tgtadm"):
            STATE.iscsi_mgr = ISCSITargetManager(target_ip, volume_dir)
            STATE.iscsi_mgr.start()
            LOG.info("iSCSI target started: %s", STATE.iscsi_mgr.target_iqn)
        else:
            LOG.warning("tgtadm not found; running in API-only mode (no real iSCSI)")

    server = HTTPServer((args.host, args.port), OceanStorHandler)

    if not args.no_ssl:
        cert_file, key_file = generate_self_signed_cert(args.cert_dir)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        proto = "HTTPS"
    else:
        proto = "HTTP"

    LOG.info("OceanStor Simulator started on %s://%s:%d", proto, args.host, args.port)
    LOG.info("Storage pools: %s", ", ".join(pool_names))
    LOG.info("iSCSI target IP: %s", target_ip)
    LOG.info("iSCSI target mode: %s", "enabled" if STATE.iscsi_mgr else "disabled (API-only)")
    LOG.info("Device ID: %s", DEVICE_ID)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
