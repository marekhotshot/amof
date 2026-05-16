#!/usr/bin/env sh

set -eu

runtime="${AMOF_BUILDER_RUNTIME:-docker}"

if [ "$runtime" = "containerd" ]; then
  address="${CONTAINERD_ADDRESS:-/var/run/containerd.sock}"
  namespace="${CONTAINERD_NAMESPACE:-default}"
  buildkit_host="${BUILDKIT_HOST:-unix:///run/buildkit/buildkitd.sock}"
  buildkit_socket="${buildkit_host#unix://}"
  buildkit_config="${BUILDKIT_CONFIG:-/tmp/buildkitd.toml}"
  insecure_registry="${AMOF_BUILDER_INSECURE_REGISTRY:-}"

  mkdir -p "$(dirname "$buildkit_socket")"
  if [ -n "$insecure_registry" ] && [ ! -f "$buildkit_config" ]; then
    cat >"$buildkit_config" <<'EOF'
[registry."172.18.0.1:5000"]
  http = true
  insecure = true

[registry."host.k3d.internal:5000"]
  http = true
  insecure = true

[registry."k3d-amof-registry:5000"]
  http = true
  insecure = true
EOF
  fi

  if ! /usr/bin/buildctl --addr "$buildkit_host" debug workers >/dev/null 2>&1; then
    config_args=""
    if [ -f "$buildkit_config" ]; then
      config_args="--config $buildkit_config"
    fi
    /usr/bin/buildkitd \
      --addr "$buildkit_host" \
      --containerd-worker=false \
      --oci-worker=true \
      $config_args \
      >/tmp/buildkitd.log 2>&1 &

    ready=0
    for _attempt in $(seq 1 30); do
      if /usr/bin/buildctl --addr "$buildkit_host" debug workers >/dev/null 2>&1; then
        ready=1
        break
      fi
      sleep 1
    done

    if [ "$ready" -ne 1 ]; then
      echo "Failed to start buildkitd for containerd runtime." >&2
      if [ -f /tmp/buildkitd.log ]; then
        cat /tmp/buildkitd.log >&2
      fi
      exit 1
    fi
  fi

  export BUILDKIT_HOST="$buildkit_host"
  if [ -n "$insecure_registry" ]; then
    if [ "${1:-}" = "build" ]; then
      shift
      exec /usr/bin/nerdctl --address "$address" --namespace "$namespace" --insecure-registry build --buildkit-host "$buildkit_host" "$@"
    fi
    exec /usr/bin/nerdctl --address "$address" --namespace "$namespace" --insecure-registry "$@"
  fi
  if [ "${1:-}" = "build" ]; then
    shift
    exec /usr/bin/nerdctl --address "$address" --namespace "$namespace" build --buildkit-host "$buildkit_host" "$@"
  fi
  exec /usr/bin/nerdctl --address "$address" --namespace "$namespace" "$@"
fi

exec /usr/local/bin/docker-cli "$@"
