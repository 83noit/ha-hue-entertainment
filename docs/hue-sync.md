# Hue Sync app support (experimental)

Philips Ambilight TVs talk to the bridge over plain HTTP on port 80 and stream
over DTLS on port 2100 — that is what this integration was built for, and it
needs nothing from this page.

Signify's own apps — notably the **Hue Sync app on Samsung TVs** — speak the
newer **CLIP v2** API, which a real bridge only serves over **HTTPS on port
443**. The *Hue Sync app support* option adds that: an HTTPS listener with a
Hue-bridge-style certificate, the CLIP v2 resources an entertainment client
reads, start/stop, and the event stream. Streaming then uses the same DTLS
server and the same Zigbee-paced engine as the Ambilight path.

> **Status: untested with a real Hue Sync app.** The implementation follows
> the [diyHue](https://github.com/diyHue/diyHue) emulator, which Samsung TV
> users report working, and is covered by automated tests (including a real
> TLS + DTLS end-to-end run). Whether a given app version accepts an emulated
> bridge is decided by Signify, not by this integration. If you try it,
> please report back in
> [issue #1](https://github.com/83noit/ha-hue-entertainment/issues/1) —
> success or failure, with the logs described below.

## Enabling it

**Configure** on the integration card → turn on **Hue Sync app support
(experimental)** → **Submit**.

It is off by default: nothing listens on port 443 unless you turn it on.
When you do, the integration first checks that it can actually use port 443
and refuses to save the option if it can't (see below).

Then, in the Hue Sync app, search for a bridge and pair while the
integration's pairing window is open (**Configure → Pair TV now**, the same
60-second window the Ambilight TV uses).

## Port 443

Official apps only ever connect to `https://<bridge IP>` on port 443 — the
port can't be changed. The integration runs its own TLS listener because the
apps check a bridge-shaped certificate, which Home Assistant's own web server
can't present.

| Your setup | What happens |
|---|---|
| Home Assistant on port 80 or 8123 (the defaults) | port 443 is free; the listener starts |
| Home Assistant's web server itself on 443 (you set a certificate and port 443) | the option can't be enabled — see below |
| Something else holds 443 (e.g. a reverse-proxy add-on) | the option can't be enabled — see below |

If port 443 is taken:

- **Move Home Assistant's own HTTPS off 443**: back to port 80 or 8123
  (Settings → System → Network), using a reverse proxy or Home Assistant
  Cloud for remote HTTPS access instead.
- **Give the bridge its own IP address** (Container/Core installs): add a
  secondary IP (macvlan or an IP alias, as in
  [port-conflicts.md](port-conflicts.md)) and set it as the **Bind IP
  address**. Home Assistant must then listen on its main IP only
  (`server_host` in its `http:` configuration), because Linux won't let the
  bridge bind `<bridge IP>:443` while another server holds `0.0.0.0:443`.
  Home Assistant OS has no secondary IPs, so this route isn't available there.

Never forward port 443 (or 80) from your router to the bridge: it only
answers requests coming directly from the local network, and refuses
anything else.

If port 443 becomes unavailable later (say, after installing an add-on), a
**repair issue** appears; Ambilight keeps working either way.

The TV must also reach the Home Assistant host on **TCP 443**, in addition to
TCP 80 and UDP 2100.

With the option on, the bridge also advertises itself the way a real bridge
does: mDNS announces port 443, and it answers SSDP (UPnP) searches on UDP 1900,
sharing that port with Home Assistant's own SSDP. If 1900 can't be shared,
SSDP simply stays off (the log says so); mDNS still works.

## Known limitations

- **One entertainment area**, built from the lights you selected (channel
  order = selection order). Creating or editing areas from the app isn't
  supported; those requests are refused and logged.
- **Discovery**: official apps ask Signify's cloud for bridges first. If a
  real Hue bridge is on the same network, the app may only offer that one.
- **App-side requirements** (as reported by users, not verified here): a
  Samsung TV from 2022 or later, the paid Hue Sync app, a Hue account, and an
  entertainment area set up before syncing.
- Signify could tighten certificate checks in a future app update — newer
  Hue Sync desktop builds already reject emulated bridges.

## What to send when reporting

1. Turn on debug logging for the integration (**Enable debug logging** on the
   integration card), reproduce, then disable it to download the log.
2. Lines starting with `CLIP v2 client asked for` and `First HueStream
   frame` show what the app tried.
3. **Download diagnostics** from the integration card: it reports the HTTPS
   listener status and the certificate fingerprint (no credentials).
