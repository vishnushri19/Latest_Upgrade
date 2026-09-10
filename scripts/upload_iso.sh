#!/usr/bin/env bash
set -euo pipefail

# Upload BIG-IP ISO to /shared/images and verify SHA.
#
# Usage:
#   scripts/upload_iso.sh --host <ip_or_dns> --user <user> --iso <path_to_iso>
#
# Example:
#   scripts/upload_iso.sh --host 10.155.0.86 --user admin --iso /path/to/BIGIP-21.x.iso

HOST=""
USER="admin"
ISO=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2;;
    --user) USER="$2"; shift 2;;
    --iso)  ISO="$2";  shift 2;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

if [[ -z "$HOST" || -z "$ISO" ]]; then
  echo "Usage: $0 --host <ip_or_dns> --user <user> --iso <path_to_iso>"
  exit 1
fi

if [[ ! -f "$ISO" ]]; then
  echo "ISO not found: $ISO"
  exit 1
fi

DST_DIR="/shared/images"
BASENAME="$(basename "$ISO")"

echo "[*] Local SHA256:"
LOCAL_SHA="$(shasum -a 256 "$ISO" | awk '{print $1}')"
echo "    $LOCAL_SHA  $BASENAME"

echo "[*] Uploading to $USER@$HOST:$DST_DIR/"
scp -p "$ISO" "$USER@$HOST:$DST_DIR/"

echo "[*] Remote verify (ls + sha256):"
ssh "$USER@$HOST" "ls -lh $DST_DIR/$BASENAME && shasum -a 256 $DST_DIR/$BASENAME | awk '{print \$1}'"

REMOTE_SHA="$(ssh "$USER@$HOST" "shasum -a 256 $DST_DIR/$BASENAME | awk '{print \$1}'")"

if [[ "$LOCAL_SHA" != "$REMOTE_SHA" ]]; then
  echo "[!] SHA mismatch. Upload corrupted/partial."
  exit 2
fi

echo "[+] Upload OK and SHA verified."
echo "[*] If EXEC-IMG-001 still shows 0 due to REST caching:"
echo "    ssh $USER@$HOST 'bigstart restart restjavad'"
