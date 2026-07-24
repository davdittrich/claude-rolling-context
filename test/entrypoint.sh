#!/bin/sh
# Entrypoint for the e2e container built by test/Dockerfile.e2e.
#
# docker-compose.e2e.yml mounts the host credentials file read-only at
# /tmp/credentials.json (comment: "Mount credentials file, copy to writable
# location at runtime"). Nothing can write through a read-only bind mount,
# so this copies it to $HOME/.claude/.credentials.json — the path the real
# install (install.sh, hooks/start-proxy.sh) uses on the host — before
# starting the proxy, in case the proxy process or a client invoked inside
# the container expects credentials there.
set -e

mkdir -p "$HOME/.claude"
if [ -f /tmp/credentials.json ]; then
    cp /tmp/credentials.json "$HOME/.claude/.credentials.json"
fi

exec python3 proxy/server.py
