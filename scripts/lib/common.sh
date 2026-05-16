#!/usr/bin/env bash
# AMOF Common Library Functions
# Source this file in other scripts: source "$(dirname "$0")/../lib/common.sh"

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*" >&2
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

# Check if required commands exist
require_cmds() {
    local missing=()
    for cmd in "$@"; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done
    if (( ${#missing[@]} > 0 )); then
        log_error "Missing required commands: ${missing[*]}"
        exit 2
    fi
}

# Get AMOF root directory
get_amof_root() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    echo "$(cd "$script_dir/../.." && pwd)"
}

# Load manifest and extract value using simple parsing
get_manifest_value() {
    local key="$1"
    local manifest="${2:-$(get_amof_root)/ecosystem.yaml}"
    grep -E "^\s*${key}:" "$manifest" | head -1 | sed 's/.*:\s*//' | tr -d '"' | tr -d "'"
}

# Get customer config from manifest.yaml in ecosystem
get_customer_source() {
    local customer="$1"
    local field="$2"
    local manifest="${3:-manifest.yaml}"
    # Simple grep-based extraction - for complex cases use yq
    grep -A 20 "id: $customer" "$manifest" | grep "$field:" | head -1 | sed 's/.*:\s*//' | tr -d '"'
}

# Confirm action
confirm() {
    local prompt="${1:-Are you sure?}"
    read -r -p "$prompt [y/N]: " response
    case "$response" in
        [yY][eE][sS]|[yY]) return 0 ;;
        *) return 1 ;;
    esac
}

# Generate timestamp for filenames
timestamp() {
    date +"%Y-%m-%d-%H%M%S"
}

# Generate ISO timestamp
iso_timestamp() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

