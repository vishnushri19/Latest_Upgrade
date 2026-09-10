# Prereq: Target Image Presence (EXEC-IMG-001)

If `EXEC-IMG-001` fails (`image_count=0` via `/mgmt/tm/sys/software/image`), upload the BIG-IP 21.x ISO to the target device:

```bash
scripts/upload_iso.sh --host <standby_mgmt_ip> --user admin --iso /path/to/BIGIP-21.x.iso

