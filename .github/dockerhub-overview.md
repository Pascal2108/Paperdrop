<img src="https://raw.githubusercontent.com/Pascal2108/Paperdrop/main/static/icon-180.png" alt="Paperdrop" width="72" align="left">

# Paperdrop

A small self-hosted web page that **compresses PDFs and photos and sends them to [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) after you had a last look at them.** Built for phones and desktops, for use in your home network or VPN.

<br clear="left">

**Full documentation, setup guide and source code: [github.com/Pascal2108/paperdrop](https://github.com/Pascal2108/paperdrop)**

## Features

- **PDF, JPG and PNG upload** by drag and drop or file picker; on a phone, **Scan with camera** opens the camera directly
- **Document scanner**: finds the page in a camera photo, lets you adjust its corners with a magnifier, and straightens it; runs in the browser, can be switched off
- **Merging**: several photos, or PDFs and photos together, into one PDF, in an order you set by dragging
- **Compression with Ghostscript** in three strengths; the smaller of original and compressed file is kept
- **Review before sending**: size before and after, open the PDF, rename it, then upload or delete
- **Consume status**: shows whether Paperless consumed each upload, still has it queued, or rejected it
- **Hardened container**: runs as non-root, read-only, all capabilities dropped; documents stay in memory and never touch the disk

## Quick start

Paperdrop runs next to Paperless-ngx, behind a reverse proxy that provides HTTPS. You need two files.

`secrets.env`, with every value replaced by your own (Paperdrop refuses to start with these examples; create the key with `openssl rand -hex 32`):

```
PAPERLESS_TOKEN=your-token-here
UPLOADER_USER=your-username
UPLOADER_PASSWORD=a-long-secure-password
SECRET_KEY=paste-random-key-here
```

`docker-compose.yml`:

```yaml
services:
  paperdrop:
    image: pascal2108/paperdrop:latest
    container_name: paperdrop
    restart: unless-stopped
    env_file:
      - secrets.env
    environment:
      PAPERLESS_URL: http://paperless-webserver-1:8000   # container name of your Paperless webserver
    tmpfs:
      - /tmp:size=1g
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: 2g
    pids_limit: 200
    networks:
      - proxy

networks:
  proxy:
    external: true
    name: nginx_reverse_proxy   # network shared by your reverse proxy and Paperless
```

Then `docker compose up -d` and add a proxy host for port `8000` of the container `paperdrop`. The [setup guide](https://github.com/Pascal2108/paperdrop#getting-started) explains every step, including a Paperless user with limited rights for the token, and all optional settings.

## Tags

| Tag | Contains |
|---|---|
| `latest` | The current stable version, rebuilt weekly with security updates |
| `1.1.0`, … | Exactly that release; never changes and receives no security updates |
| `beta` | The next release while it is being tested; may break |

Images are built for `linux/amd64` and `linux/arm64` (e.g. Raspberry Pi). The same images are published as `ghcr.io/pascal2108/paperdrop`.

## Security

Paperdrop is meant for **private networks only**, not for the public internet: it has one shared login, no two-factor authentication, and has not been audited. Please report vulnerabilities privately as described in the [security policy](https://github.com/Pascal2108/paperdrop/security/policy), not in a public issue.

## License

[AGPL-3.0](https://github.com/Pascal2108/paperdrop/blob/main/LICENSE). The image also contains Ghostscript (AGPL-3.0) and other software under its own licenses.
