# Paperdrop

A small self-hosted web page for sending documents to [Paperless-ngx](https://docs.paperless-ngx.com/) from any device. Drop PDFs or photos, check the compressed result, then send it to Paperless or throw it away.

It runs as a single Docker container behind your reverse proxy and needs no database.

## Features

- **PDF, JPG and PNG upload** by drag and drop or from the file picker; on a phone, **Scan with camera** opens the camera directly
- **Photos become A4 PDFs**, one per photo or several merged into one document, with the rotation from the camera applied; uploaded PDFs keep their own page size
- **Compression with Ghostscript** in three strengths; the smaller of the original and the compressed file is kept
- **Review before sending**: every file shows its size before and after, can be opened and renamed, then uploaded or deleted, one by one or all at once
- **Consume status**: the page shows whether Paperless consumed each upload, still has it in the queue, or rejected it as failed or duplicate
- **Login that lasts 24 hours**, also across reloads, with a lockout after repeated failed attempts
- **Your own name and logo**, set in `docker-compose.yml`
- **Readable logs**: a failed upload states its cause in the container log; document titles appear shortened, as in `Inv******023`

The page is plain HTML, CSS and JavaScript served from the container. It loads nothing from other servers.

## Requirements

- Docker with Docker Compose
- A running Paperless-ngx instance
- A reverse proxy that terminates HTTPS, for example Nginx Proxy Manager, in a Docker network shared with Paperless

## Getting started

### 1. Get the files

```bash
git clone https://github.com/Pascal2108/paperdrop
cd paperdrop
cp secrets.env.example secrets.env
```

Only `docker-compose.yml` and `secrets.env` are needed on the server. The image itself is pulled from `ghcr.io/pascal2108/paperdrop`.

### 2. Create a Paperless token

Create a dedicated user in Paperless under **Settings → Users & Groups** and give it these permissions:

| Permission | Needed for |
|---|---|
| **Documents → Add** | Uploading. This is the only required one. |
| **PaperlessTask → View** | Optional: showing the consume status. Without it, uploads are listed as "sent". |

Then sign in as that user, open the profile at the top right and copy the **API Auth Token**.

The token of your own account works as well, but it gives the container full access to your documents.

#### Make the uploaded documents yours

Documents uploaded with a dedicated user belong to that user. Superusers see them anyway; other accounts do not. To hand them to your own account, add a workflow in Paperless under **Workflows → Add Workflow**:

| Field | Value |
|---|---|
| Name | any, for example `Paperdrop owner` |
| Trigger type | **Document Added** |
| Action type | **Assignment** |
| Assign owner | your own user |

This applies to every new document, whichever way it arrives. If several people add documents, use the trigger type **Consumption Started** instead and select only **API Upload** as source, so that the workflow covers just the documents that come in through the API.

To check it, upload a file and open the document's **Permissions** tab in Paperless.

### 3. Fill in `secrets.env`

| Variable | Meaning |
|---|---|
| `PAPERLESS_TOKEN` | The token from step 2 |
| `UPLOADER_USER` | Username for the upload page |
| `UPLOADER_PASSWORD` | Password for the upload page; choose a long one |
| `SECRET_KEY` | Random key that signs the login form; generate it with `openssl rand -hex 32` |

`secrets.env` is ignored by git. Never commit it.

### 4. Adjust `docker-compose.yml`

Two values depend on your setup. Each has a `# CHANGE` comment at the end of its line. Look them up with:

```bash
docker ps --format '{{.Names}}\t{{.Networks}}'
```

- **`PAPERLESS_URL`**: `http://<name of the Paperless webserver container>:8000`. Use the port inside the container, not a published one.
- **`networks.proxy.name`**: the Docker network that your reverse proxy and the Paperless webserver share.

The lines that start with `#` and are marked `(optional)` can stay as they are; see [Configuration](#configuration). Leave the block under `Security hardening` unchanged: it limits what a harmful file could do inside the container.

> **Security: use an internal network.** Paperdrop only ever talks to Paperless, so its network should have no route to the internet. Then a harmful file or a compromised library cannot send your documents anywhere. Create the network with `docker network create --internal <name>`, or set `internal: true` where it is defined, and let Paperdrop, the proxy and the Paperless webserver join it. The proxy has to stay in a second, normal network as well, or it can no longer be reached and cannot renew certificates.

### 5. Start

```bash
docker compose up -d
docker compose logs -f
```

The log shows `Paperdrop started` when the container is ready.

### 6. Add the reverse proxy host

The container publishes no port on the host. It is reachable only inside the Docker network, so all traffic has to come through the proxy.

In Nginx Proxy Manager, add a proxy host:

| Field | Value |
|---|---|
| Scheme | `http` |
| Forward Hostname | `paperdrop` |
| Forward Port | `8000` |
| SSL | Request a certificate and enable **Force SSL** |

Compressing large scans can take longer than the proxy's default timeout. Add this under **Advanced**:

```nginx
proxy_read_timeout 600s;
proxy_send_timeout 600s;
```

Put exactly one proxy in front of the app. The login lockout relies on the client address reported by that proxy.

## Using it

1. Open your domain and sign in.
2. Add files: drop them, choose them, or on a phone take a photo with **Scan with camera**. Remove a file again with the × next to it.
3. Choose whether several images become one PDF and how strongly to compress, then press **Compress**. The results appear under **Ready for review**.
4. Click the page icon to open a PDF and edit the title if you like; Paperless receives the title as shown. Then choose **Upload** or **Delete**, or send everything with **Upload all**.
5. **Sent to Paperless** lists what you uploaded since signing in, with its consume status.

Files that are neither uploaded nor deleted are removed after `KEEP_MINUTES`.

You stay signed in for `SESSION_HOURS`, counted from signing in, also when you reload the page or close the browser. The login is kept in one cookie that scripts cannot read and that is sent over HTTPS only. **Sign out** ends it at once; so does a restart of the container. The login only works from the address you signed in from, so a device whose address changes has to sign in again.

## Configuration

These are set under `environment:` in `docker-compose.yml`. Only `PAPERLESS_URL` is required. The others are listed there as comments with their default value:

```yaml
      # MAX_UPLOAD_MB: 200        # (optional) largest accepted file in MB
```

To change one, remove the `#` at the start of its line and set the value. The comment behind the value can stay. Then apply it with `docker compose up -d`.

| Variable | Default | Meaning |
|---|---|---|
| `PAPERLESS_URL` | required | Address of the Paperless webserver inside the Docker network |
| `APP_NAME` | `Paperdrop` | Name shown on the page and in the browser tab |
| `GS_QUALITY` | `ebook` | Compression strength that is preselected: `printer` (Light), `ebook` (Balanced) or `screen` (Strong) |
| `MAX_UPLOAD_MB` | `200` | Largest accepted file, and the limit for images that are merged into one document |
| `SESSION_HOURS` | `24` | How long you stay signed in |
| `KEEP_MINUTES` | `30` | How long files that were neither uploaded nor deleted are kept |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail |

### Your own name and logo

Out of the box the page shows the Paperdrop name and logo. Both can be replaced in `docker-compose.yml`:

- **Name:** remove the `#` at the start of the `APP_NAME` line and set the name.
- **Logo:** remove the `#` at the start of the logo line under `volumes:` and change `./logo.png` to the path of your file. It can be a PNG, JPG or WEBP image. Leave the part after the colon as it is.

```yaml
    environment:
      APP_NAME: Family Archive
    volumes:
      - tmp:/tmp
      - ./logo.png:/logo:ro
```

Apply the change with `docker compose up -d`. If the logo does not appear, the container log says why. The browser tab icon stays the Paperdrop one.

## Troubleshooting

When an upload fails, the page and the container log both state the cause. Read the log with `docker compose logs` or in Portainer.

| Message | Cause |
|---|---|
| `cannot reach http://…` | `PAPERLESS_URL` has the wrong container name or port, or the containers share no network |
| `HTTP 401` | `PAPERLESS_TOKEN` is wrong |
| `HTTP 403` | The token's user lacks the permission to add documents |
| Red dot next to the Paperless host name | Paperless cannot be reached or rejects the token; hover over it for the reason |
| Uploads stay on "sent" | The token's user may not view tasks; see step 2 |
| `502 Bad Gateway` from the proxy | The container is not running, or the forward hostname or network is wrong |
| `Too many failed attempts` | Five wrong logins from one address; wait 15 minutes |
| Signed out before the time is up | The container was restarted, or the device's address changed; the log then shows `Session started from … refused for …` |
| Container keeps restarting | `secrets.env` still has the example password or `SECRET_KEY`; the log names which |
| `larger than 120 megapixels` | The photo is too large to process safely; take it at a lower resolution |

## Updating

```bash
docker compose pull && docker compose up -d
```

A new image is published on every push to `main` and once a week, so that security updates of the base image and the libraries are included. Tools such as Watchtower pick it up automatically.
