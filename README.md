# UniFi Port Map

Publishes, for one UniFi switch, which wired client is plugged into which port, plus each
port's link state, speed and PoE draw, as the **UniFi Port Map** MQTT device in Home Assistant.
The RackView "Port Hunt" page uses it to find a client's jack and blink it via UIWLED.

Use a **view-only local account** on the UniFi OS console (Cloud Key / UDM):
Settings → Admins & Users → Create New → "Restrict to local access only",
role: Network = *View Only*, no other apps.

The Cloud Key's self-signed certificate is accepted by default (`verify_tls: false`).

## Install

1. Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/richcj10/ha-unifi-portmap`.
2. Install **UniFi Port Map**, set its options on the **Configuration** tab, and start it.
3. Requires the Mosquitto broker add-on (MQTT credentials are taken from it automatically);
   to use another broker, set `mqtt_host`, `mqtt_user` and `mqtt_password`.

Part of [RackViewer](https://github.com/richcj10/RackViewer), a rack status display, which
reads this add-on's entities from Home Assistant.
