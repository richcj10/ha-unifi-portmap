"""UniFi Port Map.

Logs into a UniFi OS console (Cloud Key / UDM) with a view-only account and publishes to Home Assistant
via MQTT discovery:
  * for one switch, which client is plugged into which port (plus each port's link, speed and PoE), used by
    the RackView "Port Hunt" page to find and light a jack;
  * every client on the network (wired and Wi-Fi) with where it connects and when the controller first saw
    it, used for new-device alerts.
"""
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("unifi_portmap")

OPTIONS_PATH = "/data/options.json"
BASE = "unifi_portmap"


def load_options() -> dict:
    with open(OPTIONS_PATH) as f:
        return json.load(f)


def supervisor_token() -> str:
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
        try:
            with open(f"/run/s6/container_environment/{name}") as f:
                tok = f.read().strip()
                if tok:
                    return tok
        except OSError:
            pass
    return ""


def mqtt_settings(opts: dict) -> Optional[dict]:
    if opts.get("mqtt_host"):
        return dict(host=opts["mqtt_host"], port=int(opts.get("mqtt_port") or 1883),
                    username=opts.get("mqtt_user", ""), password=opts.get("mqtt_password", ""))
    tok = supervisor_token()
    if not tok:
        log.error("No Supervisor token; set mqtt_host/mqtt_user/mqtt_password in the add-on options")
        return None
    req = urllib.request.Request("http://supervisor/services/mqtt", headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)["data"]
    except Exception as e:
        log.error("Supervisor MQTT lookup failed (%s); set mqtt_host/mqtt_user/mqtt_password instead", e)
        return None
    log.info("MQTT settings from Supervisor: %s:%s user=%s", d["host"], d["port"], d.get("username"))
    return dict(host=d["host"], port=int(d["port"]), username=d.get("username", ""), password=d.get("password", ""))


class UniFi:
    """Minimal UniFi OS client: cookie login + the classic Network API (read-only use)."""

    def __init__(self, host: str, username: str, password: str, site: str, verify_tls: bool):
        self.base = f"https://{host}"
        self.username, self.password, self.site = username, password, site
        ctx = ssl.create_default_context()
        if not verify_tls:                      # Cloud Keys ship a self-signed certificate
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                                  urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""
        self.logged_in = False

    def _req(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        if self.csrf:
            req.add_header("X-CSRF-Token", self.csrf)
        with self.opener.open(req, timeout=15) as r:
            tok = r.headers.get("X-CSRF-Token") or r.headers.get("x-updated-csrf-token")
            if tok:
                self.csrf = tok
            raw = r.read()
            return json.loads(raw) if raw else None

    def login(self) -> None:
        self.jar.clear()
        self.csrf = ""
        self._req("POST", "/api/auth/login", {"username": self.username, "password": self.password, "remember": True})
        self.logged_in = True
        log.info("Logged into UniFi OS at %s as %s", self.base, self.username)

    def get(self, path: str) -> List[dict]:
        if not self.logged_in:
            self.login()
        try:
            return (self._req("GET", f"/proxy/network/api/s/{self.site}{path}") or {}).get("data", [])
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):            # session expired: log in again once
                self.login()
                return (self._req("GET", f"/proxy/network/api/s/{self.site}{path}") or {}).get("data", [])
            raise


def build_state(u: UniFi, switch_ip: str) -> Dict[str, Any]:
    devices = u.get("/stat/device")
    sw = next((d for d in devices if d.get("ip") == switch_ip), None)
    if not sw:
        raise RuntimeError(f"no UniFi device with IP {switch_ip}")
    sw_mac = (sw.get("mac") or "").lower()
    ports = []
    for p in sw.get("port_table") or []:
        ports.append(dict(port=p.get("port_idx"), name=p.get("name") or f"Port {p.get('port_idx')}",
                          up=bool(p.get("up")), speed=p.get("speed") or 0,
                          poe_w=round(float(p.get("poe_power") or 0), 1),
                          uplink=bool(p.get("is_uplink")), media=p.get("media")))
    clients = []
    for c in u.get("/stat/sta"):
        if not c.get("is_wired") or (c.get("sw_mac") or "").lower() != sw_mac or c.get("sw_port") is None:
            continue
        clients.append(dict(name=c.get("name") or c.get("hostname") or c.get("oui") or c.get("mac"),
                            hostname=c.get("hostname"), ip=c.get("ip") or c.get("last_ip"), mac=c.get("mac"),
                            port=c.get("sw_port"), network=c.get("network"), vlan=c.get("vlan"),
                            speed=c.get("wired_rate_mbps"), uptime=c.get("uptime"), oui=c.get("oui")))
    clients.sort(key=lambda c: str(c["name"]).lower())
    return dict(switch_name=sw.get("name") or sw.get("model"), switch_model=sw.get("model"), switch_ip=switch_ip,
                switch_mac=sw_mac, clients=clients, ports=ports, client_count=len(clients),
                ports_up=sum(1 for p in ports if p["up"]), updated=int(time.time()), status="ok")


def build_network(u: UniFi) -> Dict[str, Any]:
    """Every client on the network, kept short (HA keeps attributes under ~16 KB)."""
    names = {(d.get("mac") or "").lower(): d.get("name") or d.get("model") or "?" for d in u.get("/stat/device")}
    out = []
    for c in u.get("/stat/sta"):
        wired = bool(c.get("is_wired"))
        if wired:
            at = f"{names.get((c.get('sw_mac') or '').lower(), 'switch')} port {c.get('sw_port')}"
        else:
            at = f"{names.get((c.get('ap_mac') or '').lower(), 'AP')} · {c.get('essid') or 'Wi-Fi'}"
        out.append({"m": c.get("mac"), "n": (c.get("name") or c.get("hostname") or c.get("oui") or c.get("mac") or "?")[:32],
                    "ip": c.get("ip") or c.get("last_ip"), "w": 1 if wired else 0, "at": at[:40],
                    "net": (c.get("network") or "")[:20], "fs": c.get("first_seen"), "o": (c.get("oui") or "")[:20]})
    out.sort(key=lambda c: -(c["fs"] or 0))
    return {"network": out[:180], "count": len(out), "updated": int(time.time())}


def publish_discovery(client: mqtt.Client, uid: str, switch_ip: str, name: str) -> None:
    dev = {"identifiers": [uid], "name": "UniFi Port Map", "manufacturer": "Ubiquiti",
           "model": "UniFi Port Map", "configuration_url": f"https://{switch_ip}/"}
    ents = [
        ("sensor", "clients", f"{name} Wired Clients", "client_count",
         {"icon": "mdi:lan-connect", "state_class": "measurement", "json_attributes_topic": f"{BASE}/state"}),
        ("sensor", "ports_up", f"{name} Ports Up", "ports_up", {"icon": "mdi:ethernet", "state_class": "measurement"}),
        ("sensor", "status", "Controller Status", "status", {"icon": "mdi:cloud-check-outline",
                                                             "entity_category": "diagnostic"}),
        ("sensor", "network_clients", "Network Clients", "count",
         {"icon": "mdi:devices", "state_class": "measurement", "json_attributes_topic": f"{BASE}/network",
          "state_topic": f"{BASE}/network"}),
    ]
    for comp, oid, ename, key, extra in ents:
        cfg = {"name": ename, "unique_id": f"{uid}_{oid}", "device": dev, "state_topic": f"{BASE}/state",
               "value_template": "{{ value_json.%s }}" % key, "availability_topic": f"{BASE}/status"}
        cfg.update(extra)
        client.publish(f"homeassistant/{comp}/{uid}/{oid}/config", json.dumps(cfg), retain=True)
    log.info("Published discovery (%s)", uid)


def main() -> None:
    opts = load_options()
    logging.getLogger().setLevel(getattr(logging, str(opts.get("log_level", "INFO")).upper(), logging.INFO))
    if not opts.get("username") or not opts.get("password"):
        log.error("Set username/password (a UniFi OS *view-only* local account) in the add-on configuration")
        while True:
            time.sleep(3600)
    u = UniFi(opts["host"], opts["username"], opts["password"], opts.get("site", "default"),
              bool(opts.get("verify_tls", False)))
    switch_ip = opts["switch_ip"]
    uid = "unifi_portmap_" + switch_ip.replace(".", "_")

    ms = mqtt_settings(opts)
    while ms is None:
        time.sleep(60)
        ms = mqtt_settings(opts)
    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id="unifi_portmap")
    if ms["username"]:
        client.username_pw_set(ms["username"], ms["password"])
    client.will_set(f"{BASE}/status", "offline", retain=True)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        log.info("MQTT connected: %s", reason_code)
        c.publish(f"{BASE}/status", "online", retain=True)

    client.on_connect = on_connect
    client.connect(ms["host"], ms["port"], 60)
    client.loop_start()

    published = False
    interval = int(opts.get("poll_interval", 30))
    while True:
        try:
            st = build_state(u, switch_ip)
            if not published:
                publish_discovery(client, uid, switch_ip, st["switch_name"] or "Switch")
                published = True
            client.publish(f"{BASE}/state", json.dumps(st), retain=True)
            client.publish(f"{BASE}/network", json.dumps(build_network(u)), retain=True)
            log.debug("%d clients, %d ports up", st["client_count"], st["ports_up"])
        except urllib.error.HTTPError as e:
            log.error("UniFi API error %s: %s", e.code, e.reason)
            if e.code in (400, 401, 403):
                log.error("Check the username/password (view-only local UniFi OS account)")
            u.logged_in = False
        except Exception as e:
            log.error("Poll failed: %s", e)
            u.logged_in = False
        time.sleep(interval)


if __name__ == "__main__":
    main()
