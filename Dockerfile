FROM python:3.12-slim

# Install system dependencies
# gosu is used for PUID/PGID support (running as non-root user)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    gosu \
    libchromaprint-tools \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp (latest version); pick the right binary for the host arch
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then \
        YT_DLP_BIN="yt-dlp_linux_aarch64"; \
    else \
        YT_DLP_BIN="yt-dlp"; \
    fi && \
    curl -L "https://github.com/yt-dlp/yt-dlp/releases/latest/download/${YT_DLP_BIN}" \
        -o /usr/local/bin/yt-dlp && \
    chmod a+rx /usr/local/bin/yt-dlp

# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges. Without
# one it still works, sort of, but YouTube hands back a reduced format list and
# perfectly available tracks fail with "requested format is not available".
# yt-dlp enables deno and only deno by default, so deno it is; having it on PATH
# is the entire configuration. The official bin image is a single static binary
# built for both our architectures, which beats unzipping release archives per
# arch. 92MB on a 2.2GB image, and yt-dlp has deprecated going without.
COPY --from=denoland/deno:bin-2.9.7 /deno /usr/local/bin/deno

# Install Python dependencies
RUN pip install --no-cache-dir \
    fastapi~=0.128.0 \
    uvicorn[standard]~=0.40.0 \
    httpx~=0.28.1 \
    pydantic~=2.12.5 \
    mutagen~=1.47.0 \
    playwright~=1.58.0 \
    apprise~=1.9.3 \
    bcrypt~=4.2.0 \
    pycryptodome~=3.23.0

# Create app directory
WORKDIR /app

# Copy application files
COPY *.py /app/
COPY static /app/static/
COPY entrypoint.sh /app/

# Create data directory for SQLite
RUN mkdir -p /data

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Expose port (default; override with LISTEN_PORT env var)
EXPOSE 8080

# Health check - uses LISTEN_PORT if set, falls back to 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${LISTEN_PORT:-8080}/ || exit 1

# Write print() output as it happens. Without this stdout is a pipe, Python
# block-buffers it, and app output turns up in docker logs late, out of order
# with uvicorn's (which goes to stderr), or not at all until something else
# happens to flush. Down here rather than at the top so it does not rebuild
# every layer above it.
ENV PYTHONUNBUFFERED=1

# Reap short-lived browser children as well as forwarding shutdown signals.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run the application
CMD ["/app/entrypoint.sh"]
