# syntax=docker/dockerfile:1
#
# One build, two images: the default target is the standalone Docker image,
# `--target addon` the Home Assistant add-on. Both run on a minimal root
# filesystem assembled below rather than on a distribution image: the Python
# interpreter, the parts of its standard library a daemon imports, and exactly
# the shared libraries those link, plus bash, curl, jq and busybox for scripts
# and `docker exec`. tests/test_image.py runs inside the built image in CI and
# fails when the code needs something this filesystem leaves out.

FROM python:3.12-slim AS builder

# cffi (pulled in transitively via aioesphomeapi -> cryptography) ships no
# prebuilt wheel for linux/arm/v7, so it compiles from source and needs a full C
# toolchain (compiler, libc headers + startup objects) plus the libffi headers.
# cryptography itself has an armv7 wheel, so no Rust toolchain is required here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv==0.11.2"

WORKDIR /app

# Fixed hash seed: compiled bytecode is then byte-identical between builds.
ENV PYTHONHASHSEED=0

# The dependencies get a layer of their own, so a release that only changes
# AstraMeter's code ships a layer of a few hundred kB instead of all of them.
# Wheels carry their debug symbols; stripping halves the compiled extensions.
# Neither uv nor the base image ship bytecode, and PYTHONDONTWRITEBYTECODE stops
# it being cached at run time, so every start would compile every module from
# source: slower, and several MiB of compiler garbage kept resident. Compile
# once here instead; unchecked-hash pycs stay valid whatever timestamps the
# layer copy leaves.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project \
    && find .venv -name '*.so*' -type f -exec strip --strip-unneeded {} + \
    && python -m compileall -q -j 0 --invalidation-mode unchecked-hash .venv

FROM builder AS app
COPY src/ src/
# The simulator is a development tool; the images run the meter only.
RUN uv sync --frozen --no-dev --no-editable \
    && SITE=.venv/lib/python3.12/site-packages \
    && rm -rf "$SITE/astrameter/simulator" .venv/bin/astra-sim \
    && python -m compileall -q -j 0 --invalidation-mode unchecked-hash "$SITE/astrameter" \
    && mkdir /out \
    && cp -a --parents "$SITE/astrameter" "$SITE"/astrameter-*.dist-info .venv/bin/astrameter /out/

FROM builder AS rootfs
# bash, curl and jq for [SCRIPT] power sources, which run user commands through
# the shell and were written against images that had them.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*
COPY --from=busybox:1.37-musl /bin/busybox /rootfs/usr/bin/busybox
RUN set -eu; R=/rootfs; STDLIB=/usr/local/lib/python3.12; \
    mkdir -p $R/usr/lib $R/usr/sbin $R/usr/local/bin $R/usr/local/lib $R/usr/share \
        $R/etc $R/tmp $R/app $R/root; \
    chmod 1777 $R/tmp; chmod 700 $R/root; \
    # Same merged-/usr layout as the builder (/lib -> usr/lib, ...).
    for d in bin lib lib64 sbin; do \
        if [ -L "/$d" ]; then mkdir -p "$R/$(readlink "/$d")"; ln -s "$(readlink "/$d")" "$R/$d"; fi; \
    done; \
    for b in bash curl jq; do cp "$(readlink -f "$(command -v $b)")" "$R/usr/bin/$b"; done; \
    # busybox fills in the rest of a shell environment; /bin/sh is its ash.
    for a in $($R/usr/bin/busybox --list); do \
        [ -e "$R/usr/bin/$a" ] || ln -s busybox "$R/usr/bin/$a"; \
    done; \
    # ctypes.util.find_library asks ldconfig.
    cp "$(readlink -f /sbin/ldconfig)" $R/usr/sbin/ldconfig; \
    cp -a /usr/local/bin/python3.12 $R/usr/local/bin/; \
    ln -s python3.12 $R/usr/local/bin/python3; \
    ln -s python3.12 $R/usr/local/bin/python; \
    cp -a /usr/local/lib/libpython3.12.so* $R/usr/local/lib/; \
    # The standard library, without what a daemon never imports: pip, IDLE,
    # Tk, 2to3, pydoc topics, curses, readline, dbm, sqlite and CPython's own
    # test modules.
    tar -C /usr/local/lib -cf - \
        --exclude='python3.12/site-packages/*' --exclude='python3.12/ensurepip' \
        --exclude='python3.12/idlelib' --exclude='python3.12/tkinter' \
        --exclude='python3.12/turtledemo' --exclude='python3.12/turtle.py' \
        --exclude='python3.12/lib2to3' --exclude='python3.12/pydoc_data' \
        --exclude='python3.12/config-3.12-*' --exclude='python3.12/__phello__' \
        --exclude='python3.12/sqlite3' --exclude='python3.12/curses' \
        --exclude='python3.12/dbm' --exclude='__pycache__' \
        --exclude='*/lib-dynload/_test*' --exclude='*/lib-dynload/_ctypes_test*' \
        --exclude='*/lib-dynload/xx*' --exclude='*/lib-dynload/_xx*' \
        --exclude='*/lib-dynload/_tkinter*' --exclude='*/lib-dynload/_sqlite3*' \
        --exclude='*/lib-dynload/_curses*' --exclude='*/lib-dynload/_dbm*' \
        --exclude='*/lib-dynload/_gdbm*' --exclude='*/lib-dynload/readline*' \
        python3.12 | tar -C $R/usr/local/lib -xf -; \
    # The standard library ships as bytecode only (tracebacks through it keep
    # file and line, just not the source text): half its size.
    python -m compileall -q -j 0 -b --invalidation-mode unchecked-hash \
        -s $R -p / $R$STDLIB; \
    find $R$STDLIB -name '*.py' -delete; \
    # Every shared library the interpreter, the compiled modules and the
    # script tools link.
    { for b in $R/usr/local/bin/python3.12 $R/usr/bin/bash $R/usr/bin/curl $R/usr/bin/jq; do ldd $b; done; \
      find $R/usr/local/lib /app/.venv -name '*.so*' -type f -exec ldd {} \; 2>/dev/null; } \
        | grep -o '/[^ ]*\.so[^ ]*' | grep -v "^$R" | sort -u \
        | while read -r lib; do cp -L --parents "$lib" $R; done; \
    cp -a /etc/ld.so.conf /etc/ld.so.conf.d /etc/nsswitch.conf $R/etc/; \
    cp -a /etc/ssl $R/etc/; \
    mkdir -p $R/usr/lib/ssl; cp -a /usr/lib/ssl/. $R/usr/lib/ssl/; \
    # Zone data for TZ, so log timestamps can be local time.
    cp -a /usr/share/zoneinfo $R/usr/share/; \
    ln -s /usr/share/zoneinfo/Etc/UTC $R/etc/localtime; \
    # uid/gid 999 as before, so files on existing volumes stay accessible.
    printf 'root:x:0:0:root:/root:/bin/sh\nastra:x:999:999::/app:/sbin/nologin\n' > $R/etc/passwd; \
    printf 'root:x:0:\nastra:x:999:\n' > $R/etc/group; \
    ldconfig -r $R; \
    # The config editor writes its temp file next to /app/config.ini.
    chown 999:999 $R/app

FROM scratch AS runtime
COPY --from=rootfs /rootfs/ /
COPY --from=builder /app/.venv /app/.venv
COPY --from=app /out/ /app/
WORKDIR /app
ENV PATH="/app/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
ARG GIT_COMMIT_SHA=
ENV GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

# Home Assistant add-on: runs as root (the Supervisor mounts /data and /config
# for it) and reads its configuration from the add-on options.
FROM runtime AS addon
LABEL io.hass.type="addon"
CMD ["astrameter", "--addon"]

# Standalone image (the default target).
FROM runtime
ENV LOG_LEVEL=info
EXPOSE 12345/udp
EXPOSE 52500/tcp
# busybox wget: a Python one-liner here would briefly add a second interpreter's
# worth of memory every 30 s.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["wget", "-q", "-T", "8", "-O", "/dev/null", "http://localhost:52500/health"]
USER astra
CMD ["astrameter"]
