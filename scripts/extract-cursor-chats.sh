#!/usr/bin/env bash
#
# extract-cursor-chats.sh
#
# Extracts Cursor chat histories from:
#   1. Host-side Cursor app data (Windows %APPDATA%\Cursor or Linux ~/.cursor)
#   2. All Docker containers (running + stopped) that have cursor-server data
#
# Usage:
#   Run from a WSL terminal (outside containers) or a regular Linux terminal:
#     bash extract-cursor-chats.sh [output_directory]
#
# Requirements:
#   - sqlite3 (apt install sqlite3)
#   - docker CLI
#   - python3 (for JSON processing)
#   - jq (optional, for prettier output)
#

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
OUTPUT_DIR="${1:-./cursor-chats-export}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="${OUTPUT_DIR}_${TIMESTAMP}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ─── Helper Functions ────────────────────────────────────────────────────────

log_info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${CYAN}[STEP]${NC}  $*"; }

check_dependencies() {
    local missing=()
    for cmd in sqlite3 python3 docker; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Missing required tools: ${missing[*]}"
        log_info "Install with: sudo apt install sqlite3 python3 docker.io"
        exit 1
    fi
}

# ─── Find Cursor Data Directories ───────────────────────────────────────────

find_cursor_host_dirs() {
    local dirs=()

    # Windows (via WSL mount)
    if [[ -d /mnt/c/Users ]]; then
        for user_dir in /mnt/c/Users/*/; do
            local appdata="${user_dir}AppData/Roaming/Cursor"
            if [[ -d "$appdata" ]]; then
                dirs+=("$appdata")
            fi
            # Also check Local (some versions use this)
            local localdata="${user_dir}AppData/Local/Cursor"
            if [[ -d "$localdata" ]]; then
                dirs+=("$localdata")
            fi
        done
    fi

    # Linux native
    if [[ -d "$HOME/.cursor" ]]; then
        dirs+=("$HOME/.cursor")
    fi
    if [[ -d "$HOME/.config/Cursor" ]]; then
        dirs+=("$HOME/.config/Cursor")
    fi

    # XDG
    local xdg="${XDG_CONFIG_HOME:-$HOME/.config}"
    if [[ -d "$xdg/Cursor" ]] && [[ "$xdg/Cursor" != "$HOME/.config/Cursor" ]]; then
        dirs+=("$xdg/Cursor")
    fi

    echo "${dirs[@]}"
}

# ─── Extract Chats from state.vscdb ─────────────────────────────────────────

extract_chats_from_vscdb() {
    local db_file="$1"
    local output_dir="$2"
    local source_label="$3"

    if [[ ! -f "$db_file" ]]; then
        return 1
    fi

    # Check if it's a valid SQLite file
    if ! sqlite3 "$db_file" "SELECT 1;" &>/dev/null; then
        log_warn "  Not a valid SQLite database: $db_file"
        return 1
    fi

    # List all tables
    local tables
    tables=$(sqlite3 "$db_file" ".tables" 2>/dev/null || true)

    # Get all keys that might contain chat data
    # Cursor uses ItemTable with key-value pairs
    local chat_keys
    chat_keys=$(sqlite3 "$db_file" "
        SELECT key FROM ItemTable
        WHERE key LIKE '%chat%'
           OR key LIKE '%conversation%'
           OR key LIKE '%composer%'
           OR key LIKE '%aiChat%'
           OR key LIKE '%aichat%'
           OR key LIKE '%cursorTab%'
           OR key LIKE '%history%'
           OR key LIKE '%workbench.panel.aichat%'
           OR key LIKE '%workbench.panel.composer%'
        ORDER BY key;
    " 2>/dev/null || true)

    if [[ -z "$chat_keys" ]]; then
        return 1
    fi

    local found_chats=0
    while IFS= read -r key; do
        [[ -z "$key" ]] && continue

        local safe_key
        safe_key=$(echo "$key" | tr '/:*?"<>|\\' '_')
        local out_file="${output_dir}/${safe_key}.json"

        # Extract the value
        sqlite3 "$db_file" "
            SELECT value FROM ItemTable WHERE key = '$key';
        " > "$out_file" 2>/dev/null || continue

        # Check if it's valid and non-empty
        local size
        size=$(stat -c%s "$out_file" 2>/dev/null || stat -f%z "$out_file" 2>/dev/null || echo 0)
        if [[ "$size" -lt 5 ]]; then
            rm -f "$out_file"
            continue
        fi

        found_chats=$((found_chats + 1))
        log_ok "    Extracted key: $key ($size bytes)"
    done <<< "$chat_keys"

    return $(( found_chats == 0 ? 1 : 0 ))
}

# ─── Extract ALL keys from state.vscdb (comprehensive mode) ─────────────────

extract_all_keys_from_vscdb() {
    local db_file="$1"
    local output_dir="$2"

    if [[ ! -f "$db_file" ]] || ! sqlite3 "$db_file" "SELECT 1;" &>/dev/null; then
        return 1
    fi

    # Dump all keys and their sizes
    sqlite3 "$db_file" "
        SELECT key, length(value) as size FROM ItemTable ORDER BY key;
    " 2>/dev/null > "${output_dir}/_all_keys.txt" || true

    # Also get table schema
    sqlite3 "$db_file" ".schema" > "${output_dir}/_schema.txt" 2>/dev/null || true
}

# ─── Process Host-Side Cursor Data ──────────────────────────────────────────

process_host_cursor_data() {
    local cursor_dir="$1"
    local host_output="${OUTPUT_DIR}/host"
    mkdir -p "$host_output"

    log_step "Processing host Cursor data: $cursor_dir"

    # 1. Find workspaceStorage state.vscdb files
    local ws_storage="${cursor_dir}/User/workspaceStorage"
    if [[ -d "$ws_storage" ]]; then
        log_info "  Scanning workspaceStorage..."
        local ws_count=0
        for ws_dir in "$ws_storage"/*/; do
            [[ ! -d "$ws_dir" ]] && continue
            local ws_id
            ws_id=$(basename "$ws_dir")
            local state_db="${ws_dir}state.vscdb"

            if [[ -f "$state_db" ]]; then
                local ws_output="${host_output}/workspace_${ws_id}"
                mkdir -p "$ws_output"

                # Try to get workspace info
                if [[ -f "${ws_dir}workspace.json" ]]; then
                    cp "${ws_dir}workspace.json" "${ws_output}/_workspace_info.json"
                    local ws_name
                    ws_name=$(python3 -c "
import json, sys
try:
    with open('${ws_dir}workspace.json') as f:
        d = json.load(f)
    folder = d.get('folder', d.get('workspace', 'unknown'))
    print(folder)
except:
    print('unknown')
" 2>/dev/null || echo "unknown")
                    log_info "    Workspace: $ws_name"
                fi

                if extract_chats_from_vscdb "$state_db" "$ws_output" "host/$ws_id"; then
                    ws_count=$((ws_count + 1))
                fi

                # Also dump all keys for reference
                extract_all_keys_from_vscdb "$state_db" "$ws_output"
            fi
        done
        log_info "  Found chat data in $ws_count workspace(s)"
    fi

    # 2. Check globalStorage
    local global_storage="${cursor_dir}/User/globalStorage"
    if [[ -d "$global_storage" ]]; then
        log_info "  Scanning globalStorage..."
        local global_output="${host_output}/global"
        mkdir -p "$global_output"

        # Check for state.vscdb in global storage
        if [[ -f "${global_storage}/state.vscdb" ]]; then
            extract_chats_from_vscdb "${global_storage}/state.vscdb" "$global_output" "host/global"
            extract_all_keys_from_vscdb "${global_storage}/state.vscdb" "$global_output"
        fi

        # Check for storage.json
        if [[ -f "${cursor_dir}/storage.json" ]]; then
            cp "${cursor_dir}/storage.json" "${global_output}/_storage.json"
            log_ok "    Copied storage.json"
        fi
    fi

    # 3. Check for Cursor-specific chat storage (newer versions)
    for chat_dir in \
        "${cursor_dir}/User/globalStorage/anysphere.cursor-chat" \
        "${cursor_dir}/User/globalStorage/cursor.chat" \
        "${cursor_dir}/chat" \
        "${cursor_dir}/conversations"; do
        if [[ -d "$chat_dir" ]]; then
            log_info "  Found chat directory: $chat_dir"
            cp -r "$chat_dir" "${host_output}/$(basename "$chat_dir")" 2>/dev/null || true
        fi
    done
}

# ─── Process Docker Containers ───────────────────────────────────────────────

process_docker_containers() {
    log_step "Scanning Docker containers..."

    # Check if Docker is accessible
    if ! docker info &>/dev/null; then
        log_warn "Docker is not accessible. Skipping container extraction."
        log_info "Make sure Docker Desktop is running and your user is in the docker group."
        return
    fi

    # List ALL containers (running + stopped)
    local containers
    containers=$(docker ps -a --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.CreatedAt}}' 2>/dev/null || true)

    if [[ -z "$containers" ]]; then
        log_warn "No Docker containers found."
        return
    fi

    local container_output="${OUTPUT_DIR}/containers"
    mkdir -p "$container_output"

    local total=0
    local extracted=0

    while IFS='|' read -r id name image status created; do
        [[ -z "$id" ]] && continue
        total=$((total + 1))

        log_info "  Container: $name ($id)"
        log_info "    Image: $image"
        log_info "    Status: $status"
        log_info "    Created: $created"

        local ctr_output="${container_output}/${name}_${id}"
        mkdir -p "$ctr_output"

        # Save container metadata
        cat > "${ctr_output}/_container_info.json" << JSONEOF
{
    "id": "$id",
    "name": "$name",
    "image": "$image",
    "status": "$status",
    "created": "$created"
}
JSONEOF

        # Try to find cursor-server data paths
        # Common locations inside dev containers
        local cursor_paths=(
            "/root/.cursor-server/data"
            "/home/vscode/.cursor-server/data"
            "/home/node/.cursor-server/data"
            "/home/nex/.cursor-server/data"
        )

        # Also try to detect the user dynamically
        local container_user
        if echo "$status" | grep -qi "up"; then
            container_user=$(docker exec "$id" whoami 2>/dev/null || echo "")
            if [[ -n "$container_user" && "$container_user" != "root" ]]; then
                cursor_paths+=("/home/${container_user}/.cursor-server/data")
            fi
        fi

        local found_data=false

        for cursor_path in "${cursor_paths[@]}"; do
            # Try docker cp (works for both running and stopped containers)
            log_info "    Trying: $cursor_path"

            # First check if the path exists
            local temp_dir
            temp_dir=$(mktemp -d)

            if docker cp "${id}:${cursor_path}" "$temp_dir/cursor-data" 2>/dev/null; then
                log_ok "    Found cursor-server data at: $cursor_path"
                found_data=true

                # Move data to output
                mv "$temp_dir/cursor-data" "${ctr_output}/cursor-server-data"

                # Process any vscdb files
                while IFS= read -r vscdb; do
                    [[ -z "$vscdb" ]] && continue
                    local vscdb_name
                    vscdb_name=$(basename "$(dirname "$vscdb")")_$(basename "$vscdb")
                    local vscdb_output="${ctr_output}/extracted_${vscdb_name}"
                    mkdir -p "$vscdb_output"
                    extract_chats_from_vscdb "$vscdb" "$vscdb_output" "container/$name"
                    extract_all_keys_from_vscdb "$vscdb" "$vscdb_output"
                done < <(find "${ctr_output}/cursor-server-data" -name "*.vscdb" 2>/dev/null)

                # Process checkpoint metadata
                local checkpoints_dir="${ctr_output}/cursor-server-data/User/globalStorage/anysphere.cursor-retrieval/checkpoints"
                if [[ -d "$checkpoints_dir" ]]; then
                    log_info "    Found retrieval checkpoints"
                    local cp_count
                    cp_count=$(find "$checkpoints_dir" -name "metadata.json" | wc -l)
                    log_ok "    $cp_count checkpoint(s) found"
                fi

                extracted=$((extracted + 1))
                break
            fi

            rm -rf "$temp_dir"
        done

        # Also try to find cursor data via volume mounts
        if [[ "$found_data" == "false" ]]; then
            # Check for any .cursor-server paths by listing the container filesystem
            local all_cursor_paths
            if echo "$status" | grep -qi "up"; then
                all_cursor_paths=$(docker exec "$id" find / -maxdepth 4 -name ".cursor-server" -type d 2>/dev/null || true)
            fi

            if [[ -n "${all_cursor_paths:-}" ]]; then
                while IFS= read -r found_path; do
                    [[ -z "$found_path" ]] && continue
                    local temp_dir2
                    temp_dir2=$(mktemp -d)
                    if docker cp "${id}:${found_path}/data" "$temp_dir2/cursor-data" 2>/dev/null; then
                        log_ok "    Found cursor data at: $found_path/data"
                        mv "$temp_dir2/cursor-data" "${ctr_output}/cursor-server-data"
                        extracted=$((extracted + 1))
                        break
                    fi
                    rm -rf "$temp_dir2"
                done <<< "$all_cursor_paths"
            fi
        fi

        if [[ "$found_data" == "false" ]]; then
            log_warn "    No cursor-server data found in this container"
        fi

        echo ""
    done <<< "$containers"

    log_info "  Processed $total container(s), extracted data from $extracted"
}

# ─── Convert Chats to Readable Markdown ──────────────────────────────────────

convert_chats_to_markdown() {
    log_step "Converting chat data to readable Markdown..."

    local md_output="${OUTPUT_DIR}/readable"
    mkdir -p "$md_output"

    # Find all extracted JSON files
    local json_files
    json_files=$(find "$OUTPUT_DIR" -name "*.json" ! -name "_*.json" ! -name "*.cache" -type f 2>/dev/null || true)

    if [[ -z "$json_files" ]]; then
        log_warn "No chat JSON files found to convert."
        return
    fi

    # Python script to parse and convert Cursor chat data
    python3 << 'PYEOF'
import json
import os
import sys
from datetime import datetime
from pathlib import Path

output_dir = os.environ.get("OUTPUT_DIR", "./cursor-chats-export")
md_output = os.path.join(output_dir, "readable")
os.makedirs(md_output, exist_ok=True)

all_conversations = []
errors = []

def parse_timestamp(ts):
    """Convert various timestamp formats to readable string."""
    if isinstance(ts, (int, float)):
        if ts > 1e12:  # milliseconds
            ts = ts / 1000
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except:
            return str(ts)
    return str(ts)

def extract_conversations_from_json(filepath):
    """Try to extract conversations from a JSON file."""
    convos = []
    try:
        with open(filepath, 'r', errors='replace') as f:
            content = f.read().strip()
            if not content:
                return convos

            data = json.loads(content)

            # Handle different data structures
            if isinstance(data, dict):
                # Direct conversation object
                if 'messages' in data or 'turns' in data:
                    convos.append(data)
                # Array of conversations
                elif 'conversations' in data:
                    items = data['conversations']
                    if isinstance(items, list):
                        convos.extend(items)
                    elif isinstance(items, dict):
                        convos.extend(items.values())
                # Tabs/panels with conversations
                elif 'tabs' in data:
                    for tab in (data['tabs'] if isinstance(data['tabs'], list) else []):
                        if isinstance(tab, dict):
                            if 'bubbles' in tab:
                                convos.append(tab)
                            elif 'conversation' in tab:
                                convos.append(tab['conversation'])
                # Cursor AI chat format with bubbles
                elif 'bubbles' in data:
                    convos.append(data)
                # Nested under various keys
                for key in ['chats', 'sessions', 'threads', 'entries', 'items']:
                    if key in data:
                        items = data[key]
                        if isinstance(items, list):
                            for item in items:
                                if isinstance(item, dict) and ('messages' in item or 'bubbles' in item or 'turns' in item or 'text' in item):
                                    convos.append(item)
                        elif isinstance(items, dict):
                            for v in items.values():
                                if isinstance(v, dict) and ('messages' in v or 'bubbles' in v or 'turns' in v):
                                    convos.append(v)

            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        if any(k in item for k in ['messages', 'bubbles', 'turns', 'text', 'content']):
                            convos.append(item)

    except json.JSONDecodeError:
        # Try line-by-line JSON
        try:
            with open(filepath, 'r', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            obj = json.loads(line)
                            if isinstance(obj, dict) and any(k in obj for k in ['messages', 'bubbles', 'turns']):
                                convos.append(obj)
                        except:
                            pass
        except:
            pass
    except Exception as e:
        errors.append(f"{filepath}: {e}")

    return convos

def conversation_to_markdown(convo, index, source_file):
    """Convert a conversation object to Markdown."""
    lines = []

    # Title
    title = convo.get('title', convo.get('name', convo.get('id', f'Conversation {index}')))
    lines.append(f"# {title}")
    lines.append("")

    # Metadata
    created = convo.get('createdAt', convo.get('created', convo.get('timestamp', '')))
    if created:
        lines.append(f"**Created:** {parse_timestamp(created)}")

    workspace = convo.get('workspace', convo.get('workspaceFolder', ''))
    if workspace:
        lines.append(f"**Workspace:** {workspace}")

    model = convo.get('model', convo.get('modelId', ''))
    if model:
        lines.append(f"**Model:** {model}")

    lines.append(f"**Source:** `{source_file}`")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Messages / Bubbles / Turns
    messages = convo.get('messages', convo.get('bubbles', convo.get('turns', [])))

    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict):
                role = msg.get('role', msg.get('type', msg.get('sender', 'unknown')))
                content = msg.get('content', msg.get('text', msg.get('message', '')))

                # Handle nested content
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, str):
                            parts.append(part)
                        elif isinstance(part, dict):
                            parts.append(part.get('text', part.get('content', str(part))))
                    content = '\n'.join(parts)
                elif isinstance(content, dict):
                    content = content.get('text', content.get('value', json.dumps(content, indent=2)))

                if not content:
                    content = "(empty message)"

                # Format role
                role_display = {
                    'user': 'User',
                    'human': 'User',
                    'assistant': 'Assistant',
                    'ai': 'Assistant',
                    'system': 'System',
                    '1': 'User',
                    '2': 'Assistant',
                }.get(str(role).lower(), str(role).title())

                ts = msg.get('timestamp', msg.get('createdAt', ''))
                ts_str = f" ({parse_timestamp(ts)})" if ts else ""

                if role_display in ('User', 'Human'):
                    lines.append(f"## {role_display}{ts_str}")
                elif role_display in ('Assistant', 'AI'):
                    lines.append(f"## {role_display}{ts_str}")
                else:
                    lines.append(f"## {role_display}{ts_str}")

                lines.append("")
                lines.append(str(content))
                lines.append("")
            elif isinstance(msg, str):
                lines.append(msg)
                lines.append("")

    # If there's a direct text/content field (simple format)
    if not messages:
        text = convo.get('text', convo.get('content', convo.get('query', '')))
        if text:
            lines.append("## Content")
            lines.append("")
            lines.append(str(text))
            lines.append("")

        response = convo.get('response', convo.get('answer', convo.get('result', '')))
        if response:
            lines.append("## Response")
            lines.append("")
            lines.append(str(response))
            lines.append("")

    return '\n'.join(lines)

# Walk through all extracted data
for root, dirs, files in os.walk(output_dir):
    if 'readable' in root:
        continue
    for fname in files:
        if not fname.endswith('.json') or fname.startswith('_'):
            continue
        filepath = os.path.join(root, fname)
        rel_path = os.path.relpath(filepath, output_dir)

        convos = extract_conversations_from_json(filepath)
        if convos:
            for i, convo in enumerate(convos):
                md_content = conversation_to_markdown(convo, i + 1, rel_path)
                all_conversations.append({
                    'source': rel_path,
                    'index': i,
                    'title': convo.get('title', convo.get('name', f'Conversation {i+1}')),
                    'created': convo.get('createdAt', convo.get('created', convo.get('timestamp', 0))),
                    'markdown': md_content,
                    'raw': convo
                })

# Sort by creation time (newest first)
all_conversations.sort(key=lambda c: c.get('created', 0) if isinstance(c.get('created', 0), (int, float)) else 0, reverse=True)

# Write individual conversation files
for i, convo in enumerate(all_conversations):
    safe_title = "".join(c if c.isalnum() or c in ' -_' else '_' for c in str(convo['title']))[:80]
    md_file = os.path.join(md_output, f"{i+1:03d}_{safe_title}.md")
    with open(md_file, 'w') as f:
        f.write(convo['markdown'])

# Write index file
index_file = os.path.join(md_output, "INDEX.md")
with open(index_file, 'w') as f:
    f.write("# Cursor Chat Export Index\n\n")
    f.write(f"**Exported:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    f.write(f"**Total conversations:** {len(all_conversations)}\n\n")
    f.write("| # | Title | Source | Created |\n")
    f.write("|---|-------|--------|--------|\n")
    for i, convo in enumerate(all_conversations):
        title = str(convo['title'])[:60]
        source = convo['source']
        created = parse_timestamp(convo['created']) if convo['created'] else 'Unknown'
        f.write(f"| {i+1} | {title} | `{source}` | {created} |\n")

# Write raw dump for completeness
raw_file = os.path.join(output_dir, "all_conversations_raw.json")
with open(raw_file, 'w') as f:
    raw_data = [{'source': c['source'], 'title': c['title'], 'created': c['created'], 'raw': c['raw']}
                for c in all_conversations]
    json.dump(raw_data, f, indent=2, default=str)

print(f"\nTotal conversations found: {len(all_conversations)}")
if errors:
    print(f"Errors encountered: {len(errors)}")
    for e in errors:
        print(f"  - {e}")
PYEOF
}

# ─── Generate Summary Report ────────────────────────────────────────────────

generate_summary() {
    log_step "Generating summary report..."

    local report="${OUTPUT_DIR}/REPORT.md"

    cat > "$report" << EOF
# Cursor Chat Extraction Report

**Date:** $(date '+%Y-%m-%d %H:%M:%S')
**Host:** $(hostname)
**User:** $(whoami)

## Sources Scanned

### Host-Side Cursor Data
EOF

    local host_dirs
    host_dirs=$(find_cursor_host_dirs)
    for d in $host_dirs; do
        echo "- \`$d\`" >> "$report"
    done

    cat >> "$report" << EOF

### Docker Containers
EOF

    if command -v docker &>/dev/null && docker info &>/dev/null; then
        docker ps -a --format '| {{.Names}} | {{.ID}} | {{.Status}} | {{.Image}} |' 2>/dev/null | {
            echo "| Name | ID | Status | Image |" >> "$report"
            echo "|------|-------|--------|-------|" >> "$report"
            cat >> "$report"
        }
    else
        echo "*Docker not accessible*" >> "$report"
    fi

    cat >> "$report" << EOF

## Extracted Files

\`\`\`
$(find "$OUTPUT_DIR" -type f | sort)
\`\`\`

## How to Read

- **\`readable/\`** - Markdown files, one per conversation (easiest to read)
- **\`readable/INDEX.md\`** - Table of all conversations
- **\`host/\`** - Raw extracted data from host Cursor installation
- **\`containers/\`** - Raw extracted data from Docker containers
- **\`all_conversations_raw.json\`** - Complete raw dump
EOF

    log_ok "Report saved to: $report"
}

# ─── Main ────────────────────────────────────────────────────────────────────

main() {
    echo ""
    echo "========================================"
    echo "  Cursor Chat History Extractor"
    echo "========================================"
    echo ""

    check_dependencies

    mkdir -p "$OUTPUT_DIR"
    log_info "Output directory: $OUTPUT_DIR"
    echo ""

    # Step 1: Host-side Cursor data
    log_step "Step 1/4: Scanning host Cursor data directories..."
    local cursor_dirs
    cursor_dirs=$(find_cursor_host_dirs)

    if [[ -n "$cursor_dirs" ]]; then
        for cursor_dir in $cursor_dirs; do
            process_host_cursor_data "$cursor_dir"
        done
    else
        log_warn "No host Cursor data directories found."
        log_info "Expected locations:"
        log_info "  Windows: /mnt/c/Users/<you>/AppData/Roaming/Cursor"
        log_info "  Linux: ~/.cursor or ~/.config/Cursor"
    fi
    echo ""

    # Step 2: Docker containers
    log_step "Step 2/4: Extracting from Docker containers..."
    process_docker_containers
    echo ""

    # Step 3: Convert to readable format
    log_step "Step 3/4: Converting to readable Markdown..."
    export OUTPUT_DIR
    convert_chats_to_markdown
    echo ""

    # Step 4: Summary
    log_step "Step 4/4: Generating summary..."
    generate_summary
    echo ""

    echo "========================================"
    echo "  Extraction Complete!"
    echo "========================================"
    echo ""
    log_ok "All data saved to: $OUTPUT_DIR"
    log_info "Start with: $OUTPUT_DIR/readable/INDEX.md"
    log_info "Full report: $OUTPUT_DIR/REPORT.md"
    echo ""
}

main "$@"
