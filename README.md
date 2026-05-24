# EZ Cams Raspberry Pi Agent

Standalone HTTPS agent that runs on a Raspberry Pi and serves local camera streams only to signed EZ Cams backend requests.

For now, run the Pi agent as a normal Python command from this repo's virtual
environment. It is not installed as a `systemd` service, so it only stays online
while the command is running.

## Camera Config

Create `/etc/ezcams-pi/cameras.json`:

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
   sudo /opt/ezcams-pi-agent/.venv/bin/ezcams-pi-agent setup \
     --backend-url https://your-api.example.com \
     --claim-code YOUR-CLAIM-CODE \
     --name "Home Pi" \
     --static-ip YOUR_PUBLIC_STATIC_IP \
     --port 8443
   ```

4. Edit cameras:

   ```bash
   sudo nano /etc/ezcams-pi/cameras.json
   ```

5. Run the Pi agent:

   ```bash
   sudo /opt/ezcams-pi-agent/.venv/bin/python -m ezcams_pi_agent run \
     --config-dir /etc/ezcams-pi
   ```

   The existing console script also works:

   ```bash
   sudo /opt/ezcams-pi-agent/.venv/bin/ezcams-pi-agent run \
     --config-dir /etc/ezcams-pi
   ```

   Leave this command running. If the terminal process exits, the Pi agent is
   offline until you start it again.

6. Forward router TCP port `8443` to the Pi LAN IP port `8443`.

7. Verify from another terminal:

   ```bash
   curl -k https://127.0.0.1:8443/health
   curl -k https://YOUR_PUBLIC_STATIC_IP:8443/health
   curl -k https://YOUR_PUBLIC_STATIC_IP:8443/stream/front-door
   ```

The unsigned stream request should return `401`. A stream should work through `cams-server` only after the backend signs the Pi request.

## Camera Router

The camera streaming service can run as a separate process. The Pi agent should
be started and stopped independently from the camera router, and future combined
health checks can report camera-router status through the Pi agent `/health`
endpoint.
