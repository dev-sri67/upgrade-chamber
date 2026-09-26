#!/usr/bin/env bash
set -euo pipefail

# Install the API scaffold from a trusted local checkout. This does not start services.
readonly APP_DIR=/opt/upgrade-chamber
readonly STATE_DIR=/var/lib/upgrade-chamber
readonly CONFIG_DIR=/etc/upgrade-chamber
readonly SERVICE_USER=upgrade-chamber
readonly WORKER_USER=upgrade-chamber-worker
readonly UV_BIN=/opt/uv-0.11.8/bin/uv
readonly EXPECTED_UV_VERSION='uv 0.11.8'
readonly UV_VERSION_PATTERN='^uv 0[.]11[.]8( [(][^)]*[)])?$'
readonly FINGERPRINT_FILE=.source-fingerprint

if [[ $# -ne 1 ]]; then
    echo "Usage: sudo bash deploy/bootstrap.sh /absolute/path/to/trusted/checkout" >&2
    exit 2
fi
if [[ $EUID -ne 0 ]]; then
    echo 'Run bootstrap as root.' >&2
    exit 2
fi

# shellcheck source=/dev/null
source /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 ]]; then
    echo 'Bootstrap supports Ubuntu 24.04 LTS only.' >&2
    exit 2
fi
if [[ ! -x $UV_BIN || ! $($UV_BIN --version) =~ $UV_VERSION_PATTERN ]]; then
    echo "Install $EXPECTED_UV_VERSION at $UV_BIN before bootstrap." >&2
    exit 2
fi
source_dir=$(realpath -e -- "$1")
if [[ ! -f $source_dir/pyproject.toml || ! -f $source_dir/uv.lock || ! -d $source_dir/src/upgrade_chamber ]]; then
    echo 'Checkout must contain pyproject.toml, uv.lock, and src/upgrade_chamber.' >&2
    exit 2
fi
if [[ -n $(find "$source_dir/pyproject.toml" "$source_dir/uv.lock" "$source_dir/src" -type l -print -quit) ]]; then
    echo 'Deployment inputs must not contain symlinks.' >&2
    exit 2
fi
if [[ -n $(find "$source_dir/src/upgrade_chamber" -type f ! -name '*.py' ! -path '*/__pycache__/*' -print -quit) ]]; then
    echo 'Unexpected non-Python package source; review the deployment manifest.' >&2
    exit 2
fi
if [[ -L $APP_DIR || -L $STATE_DIR || -L $CONFIG_DIR ]]; then
    echo 'Deployment directories must not be symlinks.' >&2
    exit 2
fi

fingerprint() {
    (
        cd "$1"
        sha256sum pyproject.toml uv.lock
        find src/upgrade_chamber -type d -name __pycache__ -prune -o -type f -name '*.py' -print0 |
            LC_ALL=C sort -z | xargs -0 sha256sum
    ) | sha256sum | cut -d ' ' -f 1
}
source_fingerprint=$(fingerprint "$source_dir")

if [[ -e $APP_DIR ]]; then
    if [[ ! -f $APP_DIR/$FINGERPRINT_FILE ]]; then
        echo "$APP_DIR exists without a bootstrap fingerprint; refusing to overwrite it." >&2
        exit 2
    fi
    installed_fingerprint=$(cat "$APP_DIR/$FINGERPRINT_FILE")
    if [[ $installed_fingerprint != "$source_fingerprint" || $(fingerprint "$APP_DIR") != "$source_fingerprint" ]]; then
        echo 'Installed source differs from checkout. Review and prepare a new deployment; no files were overwritten.' >&2
        exit 2
    fi
else
    install -d -m 0755 -o root -g root "$APP_DIR"
    install -m 0644 -o root -g root "$source_dir/pyproject.toml" "$APP_DIR/pyproject.toml"
    install -m 0644 -o root -g root "$source_dir/uv.lock" "$APP_DIR/uv.lock"
    install -d -m 0755 -o root -g root "$APP_DIR/src"
    while IFS= read -r -d '' source_file; do
        relative_path=${source_file#"$source_dir"/}
        install -d -m 0755 -o root -g root "$APP_DIR/$(dirname -- "$relative_path")"
        install -m 0644 -o root -g root "$source_file" "$APP_DIR/$relative_path"
    done < <(find "$source_dir/src/upgrade_chamber" -type d -name __pycache__ -prune -o -type f -name '*.py' -print0)
    if [[ $(fingerprint "$APP_DIR") != "$source_fingerprint" ]]; then
        echo 'Copied source fingerprint does not match checkout.' >&2
        exit 1
    fi
    printf '%s\n' "$source_fingerprint" > "$APP_DIR/$FINGERPRINT_FILE"
    chmod 0644 "$APP_DIR/$FINGERPRINT_FILE"
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
if id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx docker; then
    echo 'The API service account must not belong to the Docker group.' >&2
    exit 2
fi
if ! id "$WORKER_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin --gid "$SERVICE_USER" "$WORKER_USER"
fi
usermod -aG docker "$WORKER_USER"
if ! id -nG "$WORKER_USER" | tr ' ' '\n' | grep -qx docker; then
    echo 'The worker service account must belong to the Docker group.' >&2
    exit 2
fi
install -d -m 0770 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
install -d -m 0770 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR/artifacts"
install -d -m 0700 -o root -g root "$CONFIG_DIR"
if [[ -L $CONFIG_DIR/api.env ]]; then
    echo 'Environment file must not be a symlink.' >&2
    exit 2
fi
if [[ ! -e $CONFIG_DIR/api.env ]]; then
    install -m 0600 -o root -g root /dev/null "$CONFIG_DIR/api.env"
fi
chown root:root "$CONFIG_DIR/api.env"
chmod 0600 "$CONFIG_DIR/api.env"
# worker.env must never contain inference credentials.
if [[ -L $CONFIG_DIR/worker.env ]]; then
    echo 'Environment file must not be a symlink.' >&2
    exit 2
fi
if [[ ! -e $CONFIG_DIR/worker.env ]]; then
    install -m 0640 -o root -g root /dev/null "$CONFIG_DIR/worker.env"
fi
chown root:root "$CONFIG_DIR/worker.env"
chmod 0640 "$CONFIG_DIR/worker.env"

export UV_PYTHON_INSTALL_DIR="$APP_DIR/.python"
export UV_CACHE_DIR="$APP_DIR/.uv-cache"
export UV_PROJECT_ENVIRONMENT="$APP_DIR/.venv"
export PYTHONDONTWRITEBYTECODE=1
(cd "$APP_DIR" && "$UV_BIN" sync --locked --no-dev --no-editable --python 3.11 --managed-python)
"$APP_DIR/.venv/bin/python" -c 'import upgrade_chamber.api, upgrade_chamber.worker'

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install -m 0644 -o root -g root "$script_dir/upgrade-chamber-api.service" /etc/systemd/system/upgrade-chamber-api.service
install -m 0644 -o root -g root "$script_dir/upgrade-chamber-worker.service" /etc/systemd/system/upgrade-chamber-worker.service
systemctl daemon-reload
echo 'API and worker files and both service units installed. Configure api.env, worker.env, and Caddy, then start services explicitly.'
