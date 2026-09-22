# Docker Installation

Running AstraMeter as a Docker container is the recommended way to deploy it on
a standalone server. It works on any Docker-compatible system and keeps the
environment the same across platforms.

## Prerequisites

- Docker installed on your system
- Docker Compose (optional, but recommended)

## Installation steps

1. Create a directory for the project.
2. Create your `config.ini` file **before** you start the container. The compose
   file bind-mounts `config.ini` as a single file, so Docker creates an empty
   **directory** named `config.ini` if the file isn't there yet. (To mount a
   directory instead, mount a folder to `/app/config` and point the container at
   it with `command: ["astrameter", "-c", "/app/config/config.ini"]`.)
   See the [Configuration reference](../configuration.md) for what to put in it.
3. Use the provided `docker-compose.yaml` to start the container:
   ```bash
   docker-compose up -d
   ```
   Set the `LOG_LEVEL` environment variable to control how much the container
   logs: add `LOG_LEVEL=debug` under the service's `environment:` in
   `docker-compose.yaml`. If you don't set it, the container uses `info`.
   To keep a long `debug` log for a bug report, add `LOG_FILE = astrameter.log`
   under `[GENERAL]` in `config.ini`; the file lands next to the config file
   in the mounted folder, rotated at 20 MB with two older files kept.

> **Note:** Host network mode is required because Marstek devices use UDP
> broadcasts to discover devices. Without host networking, the container can't
> receive those broadcasts properly.

Once the container is running, switch your Marstek battery to "Self-Adaptation"
mode to turn on the powermeter functionality.

## Pre-release builds (`next`)

CI publishes **pre-release** container images from the **`develop`** branch with
the **`next`** tag on GitHub Container Registry. They carry the latest changes
from before a stable release, and **may be less stable** than **`latest`**. Use
them to try fixes early, or to check the app before it lands on **`main`**.

Use the **`next`** image instead of **`latest`** in `docker-compose.yaml` (or
`docker run`):

```yaml
image: ghcr.io/tomquist/astrameter:next
```
