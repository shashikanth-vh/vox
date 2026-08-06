# Mobile access to PRISM — the laptop proxy/tunnel setup (as demoed)

The VM lives on VMware NAT (`192.168.44.x`) — visible only to the laptop. Phones on
the same Wi-Fi reach it through a **port forward on the laptop**:

```
phone ──Wi-Fi──▶ laptop 192.168.1.10:8443 ──portproxy──▶ VM 192.168.44.128:8443 (nginx edge)
```

## One-time setup (Windows laptop, ADMIN PowerShell)

```powershell
# forward laptop:8443 → VM:8443
netsh interface portproxy add v4tov4 listenport=8443 listenaddress=0.0.0.0 `
  connectport=8443 connectaddress=192.168.44.128

# let inbound 8443 through the firewall (all profiles)
netsh advfirewall firewall add rule name="PRISM 8443" dir=in action=allow `
  protocol=TCP localport=8443 profile=any
```

Both survive reboots. Adjust the two IPs to your machine (`ipconfig`: the Wi-Fi
adapter's IPv4 is the laptop; the VMnet8 adapter reveals the VM subnet).

## The URLs (sslip.io names — Google refuses raw-IP origins)

| From          | URL                                            |
|---------------|------------------------------------------------|
| laptop        | `https://192-168-44-128.sslip.io:8443/ui/`     |
| phone         | `https://192-168-1-10.sslip.io:8443/ui/`       |

Every origin used must be listed in the Google OAuth client's **Authorized
JavaScript origins** (scheme+host+port, no path). Adding an origin never changes the
client id — no rebuild, no `.env` change; allow ~5 min propagation.

## Demo checklist (phone)

1. Open the phone URL, accept the certificate warning once.
2. Google button → account picker → the provisioned account (or a Dex demo user).
3. Floating VOX button → allow the microphone → record.
4. Optional: Chrome menu → *Add to Home screen* — VOX installs as an app (PWA).

Keep the laptop awake — its sleep kills the forward mid-recording.

## Verify / tear down

```powershell
netsh interface portproxy show v4tov4              # rule present?
netstat -ano | findstr :8443                       # LISTENING on 0.0.0.0:8443?

# remove when no longer wanted:
netsh interface portproxy delete v4tov4 listenport=8443 listenaddress=0.0.0.0
netsh advfirewall firewall delete rule name="PRISM 8443"
```

## Troubleshooting (in the order they actually bit)

| Symptom | Cause / fix |
|---|---|
| Phone: page never loads | Firewall profile or router client isolation — test with firewall briefly off; use the main SSID, not guest |
| Google: origin not allowed | Phone browsing raw IP, or origin missing/typo'd in the OAuth client — sslip name, dashes, `:8443`, no path |
| Signs in as the WRONG Gmail | Fixed in 74863a1 — the button now always shows the account picker |
| Authorized at Google, PRISM 401 | The account isn't a consent-screen test user / domain not in `*_OIDC_ALLOWED_DOMAINS` / no Access user provisioned |

## When this whole page becomes obsolete

This proxy exists only because the VM hides behind VMware NAT. Either of these
retires it: switch the VM's adapter to **Bridged** (VM gets a Wi-Fi IP, phones reach
it directly), or the real deployment posture — the `prism.evamfinance.com` DNS record
+ Let's Encrypt cert, one origin for everyone, registered once.
