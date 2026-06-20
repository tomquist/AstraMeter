# Docker Installation

Running AstraMeter as a Docker container is the recommended option for a
standalone server deployment. It works on any Docker-compatible system and keeps
the environment consistent across platforms.

## Prerequisites

- Docker installed on your system
- Docker Compose (optional, but recommended)

## Installation steps

1. Create a directory for the project.
2. Create your `config.ini` file **before** starting the container. The compose
   file bind-mounts `config.ini` as a single file, and Docker will create an
   empty **directory** named `config.ini` if the file doesn't exist yet. (If you
   prefer a directory mount, mount a folder to `/app/config` and point the
   container at it with `command: ["astrameter", "-c", "config/config.ini"]`.)
   See the [Configuration reference](../configuration.md) for what to put in it.
3. Use the provided `docker-compose.yaml` to start the container:
   ```bash
   docker-compose up -d
   ```
   You can control the verbosity by setting the `LOG_LEVEL` environment variable
   (for example `-e LOG_LEVEL=debug`). If not set the container defaults to
   `info`.

> **Note:** Host network mode is required because Marstek devices use UDP
> broadcasts for device discovery. Without host networking, the container won't
> be able to receive these broadcasts properly.

When the container is running, switch your Marstek battery to "Self-Adaptation"
mode to enable the powermeter functionality.

## Pre-release builds (`next`)

CI publishes **pre-release** container images from the **`develop`** branch with
the **`next`** tag on GitHub Container Registry. These track the latest changes
before a stable release and **may be less stable** than **`latest`** — use them
to try fixes early or to validate the app before it lands on **`main`**.

Use the **`next`** image instead of **`latest`** in `docker-compose.yaml` (or
`docker run`):

```yaml
image: ghcr.io/tomquist/astrameter:next
```
