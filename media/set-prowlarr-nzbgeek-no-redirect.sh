#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
db="$script_dir/prowlarr/config/prowlarr.db"
container=prowlarr
backup="$db.pre-redirect-override-$(date +%Y%m%d-%H%M%S)-$$"
was_running=0

if [ "$(podman inspect --format '{{.State.Running}}' "$container")" = true ]; then
    was_running=1
    podman stop "$container" >/dev/null
fi

restore_container() {
    if [ "$was_running" -eq 1 ]; then
        podman start "$container" >/dev/null
    fi
}
trap restore_container EXIT

count=$(podman unshare sqlite3 "$db" \
    "select count(*) from Indexers where Name='NZBgeek';")
if [ "$count" -ne 1 ]; then
    echo "Expected exactly one NZBgeek indexer, found $count" >&2
    exit 1
fi

podman unshare cp --preserve=all "$db" "$backup"
podman unshare sqlite3 "$db" \
    "update Indexers set Redirect=0 where Name='NZBgeek';"

redirect=$(podman unshare sqlite3 "$db" \
    "select Redirect from Indexers where Name='NZBgeek';")
if [ "$redirect" -ne 0 ]; then
    echo "Failed to set NZBgeek Redirect to 0" >&2
    exit 1
fi

restore_container
trap - EXIT

echo "NZBgeek Redirect is 0"
echo "Backup: $backup"
