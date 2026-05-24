# EZ Cams Raspberry Pi Agent

Standalone HTTPS agent that runs on a Raspberry Pi and serves local camera streams only to signed EZ Cams backend requests.

Run the Pi agent in the foreground for a quick test, or install the included
`systemd` service for production so it restarts automatically.

## Local Config

By default, setup writes agent files into `.ezcams-pi/` at the repository root:

```text
.ezcams-pi/
  config.json
  cameras.json
  device.key
  agent.crt
  agent-tls.key
```

That directory is gitignored so keys, certs, and local settings are not committed.
Override the location with `--config-dir` or the `EZCAMS_PI_CONFIG_DIR` environment
variable.

Example templates live in `examples/ezcams-pi/`. After `setup`, edit
`.ezcams-pi/cameras.json` using `examples/ezcams-pi/cameras.example.json` as a
guide.

## Camera Config

Create `.ezcams-pi/cameras.json` in the repo (or copy from the example):

```json
{
  "cameras": [
    {
      "key": "front-door",
      "name": "Front Door",
      "lat": 40.7128,
      "lng": -74.006,
      "stream_url": "http://192.168.1.50:8080/video",
      "snapshot_url": "",
      "stream_type": "mjpeg",
      "description": "Front entrance",
      "is_active": true,
      "is_available": true
    }
  ]
}
```

The `stream_url` and `snapshot_url` stay on the Pi. They are not sent to app users.

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

7. Forward router TCP port `8443` to the Pi LAN IP port `8443`.

8. Verify from another terminal:

   ```bash
   curl -k https://127.0.0.1:8443/health
   curl -k https://YOUR_PUBLIC_HOST_OR_IP:8443/health
   curl -k https://YOUR_PUBLIC_HOST_OR_IP:8443/stream/front-door
   ```

The unsigned stream request should return `401`. A stream should work through `cams-server` only after the backend signs the Pi request.

## Camera Router

The camera streaming service can run as a separate process. The Pi agent should
be started and stopped independently from the camera router, and future combined
health checks can report camera-router status through the Pi agent `/health`
endpoint.
