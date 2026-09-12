# Tesla Fleet API Setup — Full Walkthrough

This documents the full path from zero to working `lock`/`unlock` commands via Postman, using Tesla's Fleet API and the official `tesla-http-proxy`. It covers every phase, every error hit along the way, and how each one got resolved.

**Vehicle used for testing:** 2021 Model 3 (requires signed commands, cannot use the legacy Owner API)

**Known open issue:** ngrok free tier gives a random domain every time the tunnel restarts. Every restart currently requires re-registering the domain and re-pairing the virtual key. A free ngrok static domain fixes this permanently — see the appendix.

---

## Phase 1: OAuth Setup (Tesla Developer App)

### 1.1 Create the app

Registered an app at [developer.tesla.com/dashboard](https://developer.tesla.com/dashboard). This gives you:
- `Client ID`
- `Client Secret`
- Fields for Allowed Origins and Allowed Redirect URIs (set here, referenced everywhere downstream)

### 1.2 Environment variables

```
TESLA_CLIENT_ID=<YOUR_CLIENT_ID>
TESLA_CLIENT_SECRET=<YOUR_CLIENT_SECRET>
TESLA_REDIRECT_URI=http://localhost:3000/callback
TESLA_SCOPES=openid offline_access vehicle_device_data vehicle_cmds
TESLA_FLEET_API_BASE=https://fleet-api.prd.na.vn.cloud.tesla.com
```

**Scopes must exactly match what's checked under API and Scopes on the dashboard.** Checking only "Vehicle Information" and "Vehicle Commands" on the dashboard means your scope string should only include `vehicle_device_data` and `vehicle_cmds` (plus the standard `openid` and `offline_access`, which aren't tied to a dashboard checkbox).

### 1.3 Get the authorization code (manual browser flow)

Postman's built-in OAuth helper uses its own callback catcher (`https://oauth.pstmn.io/v1/callback`), which won't match your registered redirect URI unless you also add Postman's callback URL to your Allowed Redirect URIs. To avoid that dependency, do this manually:

Build the URL yourself and open it in a real browser (not Postman's embedded webview — captchas render poorly in embedded windows):

```
https://auth.tesla.com/oauth2/v3/authorize?client_id=<YOUR_CLIENT_ID>&redirect_uri=http://localhost:3000/callback&response_type=code&scope=openid%20offline_access%20vehicle_device_data%20vehicle_cmds
```

Log in. You'll be redirected to `http://localhost:3000/callback?code=...`, which will show a browser connection error since nothing is listening on that port — that's fine, the error page is expected. Copy the `code` value from the address bar immediately (it expires in ~1–2 minutes and is single-use).

### 1.4 Exchange the code for tokens

```
POST https://auth.tesla.com/oauth2/v3/token
Body (x-www-form-urlencoded):
  grant_type: authorization_code
  client_id: <YOUR_CLIENT_ID>
  client_secret: <YOUR_CLIENT_SECRET>
  code: <the code from the browser>
  redirect_uri: http://localhost:3000/callback
  audience: https://fleet-api.prd.na.vn.cloud.tesla.com
```

Returns `access_token`, `refresh_token`, `id_token`, `expires_in`. Only `access_token` and `refresh_token` matter going forward.

---

## Phase 2: Refreshing Tokens

Access tokens expire (`expires_in`, generally a few hours). Refresh with:

```
POST https://auth.tesla.com/oauth2/v3/token
Body (x-www-form-urlencoded):
  grant_type: refresh_token
  client_id: <YOUR_CLIENT_ID>
  client_secret: <YOUR_CLIENT_SECRET>
  refresh_token: <current refresh_token>
  audience: https://fleet-api.prd.na.vn.cloud.tesla.com
```

**Refresh tokens rotate.** Every refresh call returns a *new* `refresh_token`. The old one is immediately invalidated. You must save both the new `access_token` and the new `refresh_token` every single time, or the next refresh attempt fails with `invalid authentication`.

---

## Phase 3: Partner Registration

Reading vehicle data returned `412 Precondition Failed` until the app itself was registered as a Fleet API partner in the region.

### 3.1 Get a partner token (different grant type, no user login)

```
POST https://auth.tesla.com/oauth2/v3/token
Body (x-www-form-urlencoded):
  grant_type: client_credentials
  client_id: <YOUR_CLIENT_ID>
  client_secret: <YOUR_CLIENT_SECRET>
  scope: openid vehicle_device_data vehicle_cmds vehicle_charging_cmds
  audience: https://fleet-api.prd.na.vn.cloud.tesla.com
```

This token is separate from your personal user token — used only for partner-level calls.

### 3.2 Generate the virtual key pair

```bash
openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem
openssl ec -in private-key.pem -pubout -out public-key.pem
```

Tesla vehicles only support `prime256v1` for this key. **`private-key.pem` must never be hosted anywhere and should be backed up securely** — losing it means regenerating the pair and redoing domain verification + re-pairing on the vehicle.

### 3.3 Host the public key

Rename `public-key.pem` to `com.tesla.3p.public-key.pem` and host it at:

```
https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem
```

Since we didn't have a production domain, used ngrok to expose a local file server:

```bash
mkdir -p serve/.well-known/appspecific
# copy com.tesla.3p.public-key.pem into serve/.well-known/appspecific/
cd serve
python -m http.server 8000
```

In a second terminal:

```bash
ngrok http 8000
```

Verify with `curl <ngrok-url>/.well-known/appspecific/com.tesla.3p.public-key.pem` — should print the raw key text.

### 3.4 Add the domain to Allowed Origins

On the Tesla dashboard, Client Details → Edit → add the **bare origin only** (no path, no trailing slash):

```
https://<your-ngrok-domain>.ngrok-free.app
```

### 3.5 Register the domain

```
POST https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/partner_accounts
Authorization: Bearer <partner_token>
Body (raw JSON):
{ "domain": "<your-ngrok-domain>.ngrok-free.app" }
```

Success returns `account_id`, `public_key`, and `public_key_hash` — confirming Tesla fetched and stored your key.

---

## Phase 4: Vehicle Data (Read-Only)

Once registered:

```
GET https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles
Authorization: Bearer <access_token>
```

Returns vehicle `id`, `vehicle_id`, `vin`, `state`, etc. This confirms OAuth + registration are both working. VIN is what matters for the next phase — the numeric `id`/`vehicle_id` fields are **not** used by the command proxy.

---

## Phase 5: Virtual Key Pairing (Phone + Vehicle)

Commands require the vehicle to have your app's public key installed in its keychain. This step happens on the phone, near the car:

```
https://tesla.com/_ak/<your-registered-domain>
```

Open this link on a phone with Bluetooth on, physically near the vehicle, logged into the Tesla app with the account that owns the car. Approve the prompt.

**This must be redone any time the key pair changes, or any time the registered domain changes** (e.g., ngrok restarts and gets a new subdomain).

To remove a stale/old key: Tesla app → car → Locks → Keys → select the key → remove. (Not strictly required before pairing a new one — vehicles support multiple paired keys — but good hygiene.)

---

## Phase 6: Local Command-Signing Proxy (`tesla-http-proxy`)

2021+ vehicles require every command to be cryptographically signed. Fleet API alone can't do this — signing happens locally via Tesla's official proxy, run via Docker.

### 6.1 TLS certificate — use Tesla's exact command

The proxy requires a TLS cert. **Do not freehand this — use Tesla's officially documented parameters exactly:**

```bash
mkdir config
openssl req -x509 -nodes -newkey ec \
    -pkeyopt ec_paramgen_curve:secp384r1 \
    -pkeyopt ec_param_enc:named_curve \
    -subj '/CN=localhost' \
    -keyout config/tls-key.pem -out config/tls-cert.pem -sha256 -days 3650 \
    -addext "extendedKeyUsage = serverAuth" \
    -addext "keyUsage = digitalSignature, keyCertSign, keyAgreement"
```

(If running this in Git Bash on Windows, `/CN=localhost` gets mangled into a Windows path by Git Bash's auto path-conversion. Use `//CN=localhost` — double slash — to prevent that.)

### 6.2 Place the fleet key

Copy your `private-key.pem` (from Phase 3.2) into the same config folder, renamed:

```
config/fleet-key.pem
```

### 6.3 Run the proxy via Docker

```bash
docker pull tesla/vehicle-command:latest
docker run --security-opt=no-new-privileges:true \
  -v "<full-path-to>/config:/config" \
  -p 127.0.0.1:4443:4443 \
  tesla/vehicle-command:latest \
  -tls-key /config/tls-key.pem \
  -cert /config/tls-cert.pem \
  -key-file /config/fleet-key.pem \
  -host 0.0.0.0 \
  -port 4443
```

Leave this running. A startup warning about "do not listen on a network interface without adding client authentication" is expected and not an error.

Sanity check (from Git Bash, not PowerShell — PowerShell aliases `curl` to `Invoke-WebRequest`, which doesn't support `-k`):

```bash
curl -k https://localhost:4443/api/1/vehicles
```

Expected response: `{"error":"client did not provide an OAuth token",...}` — confirms the proxy is alive and correctly rejecting unauthenticated requests.

---

## Phase 7: Sending Commands

```
POST https://localhost:4443/api/1/vehicles/{VIN}/command/door_lock
POST https://localhost:4443/api/1/vehicles/{VIN}/command/door_unlock
Authorization: Bearer <access_token>   (regular user token, not the partner token)
Body: {}
```

**Use the VIN in the URL path, not the numeric vehicle ID.** The proxy requires VIN specifically — this differs from some legacy Owner API clients.

For Postman specifically: the proxy's cert is self-signed, so either disable SSL verification for the request, or (Tesla's documented approach) add `tls-cert.pem` as a trusted CA certificate under Postman Settings → Certificates.

---

## Troubleshooting Appendix

| Error | Cause | Fix |
|---|---|---|
| `The 'redirect_uri' supplied is not registered for this 'client_id'` | Callback URL typed into Postman/browser didn't exactly match what's registered on the Tesla dashboard | Copy the exact registered URI, including protocol, port, and trailing slash |
| `Request was missing the 'redirect_uri' parameter` | Visited the bare `/authorize` endpoint with no query parameters | Build the full URL manually with `client_id`, `redirect_uri`, `response_type`, `scope` |
| `invalid_auth_code` / "may have already been used or expired" | Authorization codes are single-use and expire in ~1–2 minutes | Get a fresh code and exchange it immediately |
| `invalid authentication` on refresh | Used an already-rotated (stale) `refresh_token` | Always save the *new* `refresh_token` returned by every refresh call, not just the new access token |
| `412 Precondition Failed` on `/vehicles` | App not registered as a Fleet API partner in the region | Complete partner registration (Phase 3) |
| `getaddrinfo ENOTFOUND fleet-api.prd.na.vehicle-command.tesla.com` | Wrong/nonexistent API domain | Correct domain is `fleet-api.prd.na.vn.cloud.tesla.com` |
| `Invalid domain: ...` (partner registration) | Domain string included `www.`, capital letters, a path/subpath, `https://`, or a trailing slash | Domain field wants a bare lowercase host — same value used for Allowed Origins |
| `Invalid URL / URI format` on Allowed Origins | Entered a URL with a path (e.g. `github.io/repo-name`) | Allowed Origins must be scheme + host only, no path |
| `Root domain ... must match registered allowed origin` | ngrok/GitHub domain wasn't yet added to Allowed Origins on the dashboard | Add it, save, and (if using GitHub Pages) confirm the save actually persisted after a refresh |
| GitHub Pages 404 on the key file | `.well-known` folder was misnamed (e.g. `.wellknown`), or GitHub's Jekyll build silently dropped the dot-folder | Fix the folder name exactly; add an empty `.nojekyll` file at repo root |
| Browser downloads the `.pem` instead of displaying it | GitHub Pages has no registered MIME type for `.pem` — this is cosmetic only | Verify with `curl` instead of a browser; a clean text response means it's working |
| `SSL routines:OPENSSL_internal:SSLV3_ALERT_HANDSHAKE_FAILURE` connecting to the local proxy | TLS cert generated with wrong curve (`secp521r1`) and missing `extendedKeyUsage`/`keyUsage` extensions | Regenerate using Tesla's exact documented command (Phase 6.1) |
| `asn1: structure error: tags don't match` starting the proxy | `fleet-key.pem` was corrupted, empty, or the wrong file (e.g. accidentally used the public key) | Confirm the file starts with `-----BEGIN EC PRIVATE KEY-----`; regenerate if lost |
| `vehicle rejected request: your public key has not been paired with the vehicle` | Either pairing was never completed, or the vehicle has a *different* key paired than the one currently in `fleet-key.pem` | Confirm the public key derived from the current private key (`openssl ec -in fleet-key.pem -pubout`) matches what's registered and what's paired on the vehicle. Re-pair if they don't match. |
| Domain verification / pairing silently fails despite everything looking correct | ngrok free-tier domain changed after a tunnel restart, but the old (now-dead) domain was still registered/paired | Any time ngrok restarts, its subdomain changes. Re-verify the domain, re-register, and re-pair from scratch — or use a static ngrok domain (see below) |
| Postman shows stale/identical responses across different requests | Postman-in-VS-Code caching quirk, not a real API issue | Close and reopen the request tab, or reload the VS Code window (`Ctrl+Shift+P` → Developer: Reload Window). Consider using the standalone Postman desktop app instead. |
| `openssl: command not found` in PowerShell | OpenSSL isn't on PATH in native PowerShell | Use Git Bash (bundles OpenSSL) instead |
| Git Bash mangles `/CN=localhost` into a Windows path | Git Bash auto-converts leading-slash arguments into filesystem paths | Use `//CN=localhost` (double slash), or set `MSYS_NO_PATHCONV=1` for that command |
| `curl -k` fails in PowerShell (`parameter cannot be found`) | PowerShell aliases `curl` to `Invoke-WebRequest`, which doesn't support curl's flags | Use `Invoke-WebRequest -Uri ... -SkipCertificateCheck`, or run real curl from Git Bash / `curl.exe` explicitly |

---

## Known Follow-Up: Get a Static ngrok Domain

Every ngrok free-tier restart currently forces a full re-registration + re-pairing cycle. Fix: claim the one free static domain every ngrok account gets (ngrok dashboard → Domains), then always launch with:

```bash
ngrok http --url=<your-static-domain>.ngrok-free.app 8000
```

Register and pair against that domain once — it won't need to be redone on future restarts.
