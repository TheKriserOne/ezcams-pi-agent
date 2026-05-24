# EZ Cams Raspberry Pi Setup Guide

This guide registers one Raspberry Pi with `cams-server`, installs the standalone
Pi agent, and verifies that camera access only works through the backend.

## 1. Prepare The Backend

From your development machine or server where `cams-server` runs:

```bash
cd cams-server
uv sync
```

Generate the backend device signing key:

```bash
./.venv/bin/python -m auth.device_crypto > .device-signing-key.pem
chmod 600 .device-signing-key.pem
```

Add this to `cams-server/.env` or your deployment environment:

```bash
BACKEND_DEVICE_SIGNING_PRIVATE_KEY_PATH=./.device-signing-key.pem
DEVICE_ACCESS_TOKEN_EXPIRE_MINUTES=5
```

Restart `cams-server`.

## 2. Get A User Access Token

Use any active logged-in EZ Cams account. Local database accounts and Google or
social sign-in accounts can create Pi claim codes.

For local development:

```bash
export CAMS_SERVER_URL=http://127.0.0.1:3000
```

Log in and save the access token:

```bash
export ACCESS_TOKEN=$(
  curl -s -X POST "$CAMS_SERVER_URL/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"adm","password":"YOUR_PASSWORD"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'
)
```

Confirm it worked:

```bash
curl "$CAMS_SERVER_URL/auth/me" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

## 3. Create A Pi Claim Code

### Option A: From The App

Log in, open the app menu, then choose **Add Raspberry Pi**. Press
**Generate Claim Code**. The app will create a one-time claim code and show a
copyable `ezcams-pi-agent setup` command.

The Raspberry Pi supplies its own name, public host/IP, and forwarded HTTPS
port when you run setup. Once the Pi claims the code, the app shows a success
message and the device appears in your Raspberry Pi list.

The code expires quickly and can be used only once. The app does not configure
the Pi directly; you still run the generated command on the Pi.

### Option B: With Curl

Create a one-time setup code:

```bash
curl -X POST "$CAMS_SERVER_URL/devices/claims" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Home Pi","expires_minutes":15}'
```

Copy the returned `code`. It expires quickly and can be used only once.

## 4. Install The Pi Agent On Raspberry Pi

On the Raspberry Pi:

```bash
sudo apt update
sudo apt install -y python3 python3-venv git
```

Clone the repo:

```bash
cd /opt
sudo git clone YOUR_REPO_URL ez-cams
cd /opt/ez-cams/cams-pi-agent
```

Create a virtual environment and install the agent:

```bash
sudo python3 -m venv /opt/ezcams-pi-agent/.venv
sudo /opt/ezcams-pi-agent/.venv/bin/pip install -e .
```

## 5. Register The Pi With The Backend

Replace the placeholders:

- `YOUR_BACKEND_URL`: your public backend URL, such as `https://api.example.com`
- `YOUR_CLAIM_CODE`: the code from step 3
- `YOUR_PUBLIC_HOST_OR_IP`: the public DNS name or static IP where the backend can reach the Pi
- `8443`: the public/router port you will forward to this Pi

Run:

```bash
sudo /opt/ezcams-pi-agent/.venv/bin/ezcams-pi-agent setup \
  --backend-url YOUR_BACKEND_URL \
  --claim-code YOUR_CLAIM_CODE \
  --name "Home Pi" \
  --static-ip YOUR_PUBLIC_HOST_OR_IP \
  --port 8443
```

This creates local Pi secrets under:

```text
/etc/ezcams-pi/
```

The Pi private key stays on the Pi. The backend stores only the Pi public key and
the Pi HTTPS certificate.

## 6. Configure Local Cameras

Edit:

```bash
sudo nano /etc/ezcams-pi/cameras.json
```

Example:

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

Important: keep `key` simple, using only letters, numbers, dots, underscores,
and dashes. Example: `front-door`.

## 7. Start The Pi Agent

For a quick foreground test, run:

```bash
sudo /opt/ezcams-pi-agent/.venv/bin/python -m ezcams_pi_agent run \
  --config-dir /etc/ezcams-pi
```

The console script also works:

```bash
sudo /opt/ezcams-pi-agent/.venv/bin/ezcams-pi-agent run \
  --config-dir /etc/ezcams-pi
```

For production, install the systemd service:

```bash
sudo cp /opt/ez-cams/cams-pi-agent/systemd/ezcams-pi-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ezcams-pi-agent
```

Check status:

```bash
sudo systemctl status ezcams-pi-agent
```

Watch logs:

```bash
sudo journalctl -u ezcams-pi-agent -f
```

## 8. Configure Router Port Forwarding

On your router, forward:

```text
TCP 8443 -> Raspberry Pi LAN IP:8443
```

Do not forward raw camera ports. Only forward the Pi agent port.

## 9. Verify Security

Health should respond:

```bash
curl -k https://YOUR_PUBLIC_HOST_OR_IP:8443/health
```

Unsigned stream access should fail:

```bash
curl -k https://YOUR_PUBLIC_HOST_OR_IP:8443/stream/front-door
```

Expected result:

```text
401 Unauthorized
```

That is good. The Pi should not serve streams directly to random internet
requests.

## 10. Verify Backend Camera Access

Force one heartbeat and camera sync:

```bash
sudo /opt/ezcams-pi-agent/.venv/bin/ezcams-pi-agent sync-once
```

On your backend:

```bash
curl "$CAMS_SERVER_URL/cams/" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

You should see the Pi-backed camera, but its `url` should be empty:

```json
"source": "pi",
"url": ""
```

Stream through the backend:

```bash
curl "$CAMS_SERVER_URL/cams/CAMERA_ID/stream" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

The app also uses the backend route, not the Pi route.

## Troubleshooting

If creating a claim code returns `401`, sign in again and retry. Any active
authenticated account can create a Pi claim code.

If creating a claim code returns `403`, the account is inactive or the backend
is using an older role-gated deployment.

If Pi setup returns `503`, the backend device signing key is probably missing.
Check `BACKEND_DEVICE_SIGNING_PRIVATE_KEY_PATH` or
`BACKEND_DEVICE_SIGNING_PRIVATE_KEY_PEM`.

If backend streaming returns a certificate error, rerun Pi setup with the correct
public host/IP and forwarded port, or revoke the old device and register again.

If `/cams/` does not show the Pi camera, run:

```bash
sudo /opt/ezcams-pi-agent/.venv/bin/ezcams-pi-agent sync-once
```

Then check:

```bash
sudo journalctl -u ezcams-pi-agent -n 100
```
