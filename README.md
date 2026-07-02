# EZ Cams Raspberry Pi Agent

Standalone HTTPS agent that runs on a Raspberry Pi and serves local camera streams only to signed EZ Cams backend requests.

Run the Pi agent in the foreground for a quick test, or install the included
`systemd` service for production so it restarts automatically.

## Local Config

All runtime state lives in **one directory** next to the install:

```text
<install>/.ezcams-pi/
  config.json
  cameras.json
  clips/
  device.secret
  agent.crt
  agent-tls.key
```

For a clone at `/opt/ezcams-pi-agent`, that is `/opt/ezcams-pi-agent/.ezcams-pi/`.
For this dev tree, it is `./.ezcams-pi/` at the repo root.

`device.secret` is issued once at `setup` and sent as `Authorization: Bearer …` for
`unregister` and optional manual `sync-once`. The Pi does **not** periodically push
to the backend. Camera discovery happens when the backend probes this device via
signed `GET /backend/heartbeat` and reads the camera catalog from the response.
Stream/snapshot requests from the backend still require signed `X-EZCams-*` headers
verified with `backend_public_key_pem`.

Path fields in `config.json` are stored **relative to `.ezcams-pi/`** so the
install folder is self-contained and portable.

The whole `.ezcams-pi/` tree is gitignored so keys, certs, cameras, and clips are
never committed. Example templates live in `examples/ezcams-pi/`. After `setup`,
edit `.ezcams-pi/cameras.json` using `examples/ezcams-pi/cameras.example.json` as a
guide.

`recordings_dir` defaults to `clips/` under `.ezcams-pi/`. `ezcams-pi-inference`
writes there unless `--clip-dir` is passed. V1 does not auto-delete recordings;
monitor free disk space or prune old `.mkv` files manually.

Advanced override only: `--config-dir` or `EZCAMS_PI_CONFIG_DIR` (tests, custom
layouts). Normal installs should not need either.

## Camera Config

Each entry in `.ezcams-pi/cameras.json` is identified by a URL-safe `key` and
declares a `source` of one of three types: `native` (picamera2/libcamera),
`rtsp`, or `http_mjpeg`. The flat `stream_url` field is preserved as a legacy
shortcut for `http_mjpeg` so existing configs keep working.

### Source types

#### Native Pi camera (`source.type = "native"`)

Captures from a directly-attached camera with hardware JPEG encoding via
picamera2 + `JpegEncoder`.

```json
{
  "key": "porch",
  "name": "Porch",
  "lat": 40.7129,
  "lng": -74.0061,
  "fps": 10,
  "source": {
    "type": "native",
    "camera_index": 0,
    "resolution": { "width": 1280, "height": 720 },
    "hflip": false,
    "vflip": false
  }
}
```

Requires the system package on the Pi (not pip-installable):

```bash
sudo apt install -y python3-picamera2 python3-libcamera
```

When using a venv, recreate it with `--system-site-packages` so picamera2 is
importable:

```bash
python3 -m venv --system-site-packages .venv
```

#### RTSP (`source.type = "rtsp"`)

Decoded with OpenCV + FFmpeg (`rtsp_transport=tcp`, `low_delay`, buffer size 1)
and re-encoded to JPEG at `runtime.jpeg_quality`.

```json
{
  "key": "backyard",
  "name": "Backyard",
  "lat": 40.713,
  "lng": -74.0058,
  "fps": 15,
  "source": {
    "type": "rtsp",
    "url": "rtsp://user:pass@192.168.1.60:554/stream1"
  }
}
```

#### HTTP MJPEG (`source.type = "http_mjpeg"`, or legacy `stream_url`)

Pulled with `requests`; JPEG frames are extracted by scanning SOI/EOI markers
so any continuous JPEG body works.

```json
{
  "key": "front-door",
  "name": "Front Door",
  "lat": 40.7128,
  "lng": -74.006,
  "stream_url": "http://192.168.1.50:8080/video",
  "snapshot_url": "",
  "is_active": true,
  "is_available": true
}
```

`stream_url` / `snapshot_url` stay on the Pi. They are not sent to app users.

### Stream access

Remote `/stream/{camera_key}` and `/snapshot/{camera_key}` requests require
valid `X-EZCams-Payload` and `X-EZCams-Signature` headers from the backend.
Unsigned remote requests return **401**. `/health` is always public.

Remote `/recordings/{camera_key}` and `/recordings/{camera_key}/{recording_id}`
use the same backend signature check. Recording IDs are `.mkv` filenames under
that camera's clip directory.

On-device inference may pull frames over `https://127.0.0.1:{port}/stream/{key}`
without signatures when `allow_loopback_unsigned` is true (default). Disable
with `"allow_loopback_unsigned": false` or `EZCAMS_PI_ALLOW_LOOPBACK_UNSIGNED=0`.

### Per-instance runtime tuning (optional)

Add a top-level `runtime` block to `.ezcams-pi/config.json` to tune the camera
manager. All fields are optional and fall back to safe defaults.

```json
{
  "runtime": {
    "jpeg_quality": 80,
    "reconnect_delay_seconds": 5.0,
    "client_start_timeout_seconds": 10.0
  }
}
```

### Architecture

The agent runs a single `CameraManager` with one daemon `CameraWorker` thread
per active camera. Each worker owns the upstream connection and publishes the
latest JPEG with push-based fan-out (`asyncio.Event` per subscriber). Many
viewers share the same upstream — no per-client camera re-opens, and snapshots
return the most recent cached frame instantly.

## Install On Raspberry Pi

1. Install Python:

   ```bash
   sudo apt update
   sudo apt install -y git python3 python3-venv
   ```

2. Put this repo on the Pi, then install the agent:

   ```bash
   cd /opt
   sudo git clone https://github.com/TheKriserOne/ezcams-pi-agent.git ezcams-pi-agent
   cd /opt/ezcams-pi-agent
   sudo python3 -m venv /opt/ezcams-pi-agent/.venv
   sudo /opt/ezcams-pi-agent/.venv/bin/pip install -e .
   ```

3. Register the Pi with a backend claim code:

   ```bash
   ./.venv/bin/ezcams-pi-agent setup \
     --backend-url https://your-api.example.com \
     --claim-code YOUR-CLAIM-CODE \
     --name "Home Pi" \
     --static-ip YOUR_PUBLIC_HOST_OR_IP \
     --port 8443
   ```

   This creates `.ezcams-pi/config.json` and related files in the repo.

4. Edit cameras:

   ```bash
   nano .ezcams-pi/cameras.json
   ```

5. Run the Pi agent in the foreground for a quick test:

   ```bash
   ./.venv/bin/ezcams-pi-agent ensure
   ./.venv/bin/python -m ezcams_pi_agent run
   ```

   The console script also works:

   ```bash
   ./.venv/bin/ezcams-pi-agent run
   ```

6. For production, install and start the systemd service:

   ```bash
   sudo cp /opt/ezcams-pi-agent/systemd/ezcams-pi-agent.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now ezcams-pi-agent
   ```

   The service runs `ezcams-pi-agent ensure` before startup and restarts
   automatically. `ensure` validates local config only; it does not call the
   backend. Camera rows update when the backend probes this Pi.

7. Forward router TCP port `8443` to the Pi LAN IP port `8443`.

8. Verify from another terminal:

   ```bash
   curl -k https://127.0.0.1:8443/health
   curl -k https://YOUR_PUBLIC_HOST_OR_IP:8443/health
   curl -k https://YOUR_PUBLIC_HOST_OR_IP:8443/stream/front-door
   ```

The unsigned stream request should return `401`. A stream should work through `cams-server` only after the backend signs the Pi request.

After `setup`, the Pi stores a `device.secret` credential for `unregister` and
optional manual `sync-once`. Backend→Pi stream requests still use signed
`X-EZCams-*` headers verified with the backend public key in `config.json`.

Emergency manual camera push (normally not needed):

```bash
./.venv/bin/ezcams-pi-agent sync-once
```

To remove this Pi from the backend while it is still online:

```bash
./.venv/bin/ezcams-pi-agent unregister
```

If the backend is unreachable and you only want to remove local registration
files, use:

```bash
./.venv/bin/ezcams-pi-agent unregister --local-only
```

`--local-only` does not notify the backend; revoke the Pi in the app later.

## Camera Router

The camera streaming service can run as a separate process. The Pi agent should
be started and stopped independently from the camera router, and future combined
health checks can report camera-router status through the Pi agent `/health`
endpoint.
