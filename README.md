# Paperdrop

A small self-hosted web page for sending documents to [Paperless-ngx](https://docs.paperless-ngx.com/) from any device. Drop PDFs or photos, check the compressed result, then send it to Paperless or throw it away.

It runs as a single Docker container behind your reverse proxy and needs no database.

## Features

- **PDF, JPG and PNG upload** by drag and drop or from the file picker; on a phone, **Scan with camera** opens the camera directly
- **Photos become A4 PDFs**, one per photo or several merged into one document, with the rotation from the camera applied; large photos are fitted to the page, small images such as screenshots keep their size; uploaded PDFs keep their own page size
- **Merging**: all photos into one PDF, or all files, PDFs and photos together, into one PDF in the order of the list
- **Compression with Ghostscript** in three strengths; the smaller of the original and the compressed file is kept
- **Review before sending**: every file shows its size before and after, can be opened and renamed, then uploaded or deleted, one by one or all at once
- **Consume status**: the page shows whether Paperless consumed each upload, still has it in the queue, or rejected it as failed or duplicate
- **Login that lasts 24 hours**, also across reloads, with a lockout after repeated failed attempts
- **Dark, light or automatic theme**, and your own name and logo, all set in `docker-compose.yml`
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
2. Add files: drop them, choose them, or on a phone take a photo with **Scan with camera**. Remove a file again with the × next to it. Drag a file to change the order, which is the page order of a merged PDF; on a phone, drag it by the dotted grip on its left.
3. Choose what to merge into one PDF and how strongly to compress, then press **Compress**. **Merge** offers **Off** (one PDF per file), **Images** (all photos into one PDF, each PDF on its own) and **All files** (everything into one PDF, in the order of the list; password-protected PDFs cannot be merged). The results appear under **Ready for review**.
4. Click the page icon to open a PDF and edit the title if you like; Paperless receives the title as shown. Then choose **Upload** or **Delete**, or send everything with **Upload all**.
5. **Sent to Paperless** lists what you uploaded since signing in, with its consume status.

Files that are neither uploaded nor deleted are removed after `KEEP_MINUTES`. Until then they are kept in the container's memory, not on disk, so a restart of the container removes them as well.

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
| `THEME` | `dark` | Colours of the page: `dark`, `light`, or `auto`, which follows the light or dark setting of each device |
| `GS_QUALITY` | `ebook` | Compression strength that is preselected: `printer` (Light), `ebook` (Balanced) or `screen` (Strong) |
| `MAX_UPLOAD_MB` | `200` | Largest accepted file, and the limit for files that are merged into one document |
| `SESSION_HOURS` | `24` | How long you stay signed in |
| `KEEP_MINUTES` | `30` | How long files that were neither uploaded nor deleted are kept |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail |

### Your own name and logo

Out of the box the page shows the Paperdrop name and logo. Both can be replaced in `docker-compose.yml`:

- **Name:** remove the `#` at the start of the `APP_NAME` line and set the name.
- **Logo:** remove the `#` at the start of the `volumes:` line and of the logo line below it, then change `./logo.png` to the path of your file. It can be a PNG, JPG or WEBP image. Leave the part after the colon as it is.

```yaml
    environment:
      APP_NAME: Family Archive
    volumes:
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
| `Server error (500)` on a very large file | The 1 GB of memory for files in progress is full; upload or delete what is waiting for review, or raise `size=1g` under `tmpfs:` in `docker-compose.yml` together with `mem_limit` |
| `larger than 120 megapixels` | The photo is too large to process safely; take it at a lower resolution |

## Updating

```bash
docker compose pull && docker compose up -d
```

A new image is published on every push to `main` and once a week, so that security updates of the base image and the libraries are included. Tools such as Watchtower pick it up automatically.

### Versions

The image is published under these tags. Set the one you want in the `image:` line of `docker-compose.yml`.

| Tag | Contains | Changes |
|---|---|---|
| `latest` | The current stable version | With every release, and weekly for security updates |
| `1.0.0`, `1.1.0`, … | Exactly that [release](https://github.com/Pascal2108/paperdrop/releases) | Never; it also receives no security updates |
| `beta` | What is being tested for the next release | Often; it may break |
| `1.0.2-beta.1`, … | Exactly that test version ([pre-release](https://github.com/Pascal2108/paperdrop/releases)) | Never |

Use `latest` unless you have a reason not to. A version number is for going back when an update causes trouble: the release notes say what changed and whether you have to adjust your `docker-compose.yml`.

## Questions and feature requests

Open an [issue on GitHub](https://github.com/Pascal2108/paperdrop/issues/new/choose) (a free account is needed) and choose **Bug report** or **Feature request**; for a question, open a blank issue. Issues are public, so leave out tokens, passwords, host names and document titles.

## Legal

This section is information from the author, not legal advice.

### Not for the public internet

Paperdrop is built for private use inside a network you control: a home network or a VPN such as Tailscale or WireGuard. It is **not meant to be reachable from the public internet**. It has one shared login, no user management and no two-factor authentication, and it has not been audited by anyone. Do not publish its port, do not forward a port to it, and do not put it behind a proxy that is open to the internet.

### License

Paperdrop is free software under the [GNU Affero General Public License, version 3](LICENSE) (AGPL-3.0).
Copyright (C) 2026 Pascal2108.

- You may use, study, change and share it.
- If you share it, changed or not, you have to pass on the same rights: keep the license and the copyright notice, and make the source code available.
- This also applies if you change it and let other people use your version over a network, without handing them a copy. They are entitled to the source code of your version.

The image also contains other software under its own licenses, among them Ghostscript (AGPL-3.0), which Paperdrop runs as a separate program.

### No warranty, no liability

Paperdrop is a private project, provided as it is and without warranty of any kind, as sections 15 and 16 of the license state. You run it at your own risk. To the extent the law permits, the author is not liable for damage, data loss, leaked or lost documents, or any other consequence of installing, running or misconfiguring it. You are responsible for securing your server, for keeping it updated and for the documents you process with it.

### Cookies and personal data

- **One cookie:** Paperdrop sets a single cookie, `__Host-paperdrop`, when you sign in. It holds a random session token, lasts for `SESSION_HOURS`, and is removed when you sign out. Without it the login cannot work.
- **No tracking:** there are no analytics, advertising or third-party cookies, and the page loads nothing from other servers.
- **Consent:** a cookie that is strictly necessary for a service the user asked for generally needs no consent banner under the EU ePrivacy rules (in Germany § 25 TDDDG). This fits a login cookie, but the assessment for your own installation is yours.
- **Personal data:** the container log records the address of each device that signs in or fails to. The documents themselves stay in memory until you upload or delete them and are then sent only to your Paperless instance.

If other people use your installation, you are its operator: informing them and complying with data protection law, such as the GDPR, is your responsibility.
