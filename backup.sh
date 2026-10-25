#!/usr/bin/env sh
# Run from this project directory. Saves SQL and photo-volume archives in ./backups.
set -eu
mkdir -p backups
STAMP="$(date +%Y%m%d-%H%M%S)"
SQL_FILE="backups/mysql-${STAMP}.sql"
PHOTO_FILE="backups/photos-${STAMP}.tar.gz"
echo "[1/2] Exporting MySQL..."
docker compose exec -T db sh -c 'exec mysqldump --single-transaction --routines --triggers -u root -p"$MYSQL_ROOT_PASSWORD" archive' > "$SQL_FILE"
echo "[2/2] Archiving MinIO data volume..."
docker compose run --rm --no-deps --entrypoint sh backup-photos -c "tar -czf /backups/photos-${STAMP}.tar.gz -C /photos ."
echo "Backup created: $SQL_FILE and $PHOTO_FILE"
echo "IMPORTANT: copy these files to another device or encrypted cloud storage. Backups on this same server do not protect against disk loss."
