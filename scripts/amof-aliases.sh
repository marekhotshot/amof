#!/bin/bash
# AMOF Shell Aliases
# Source this file or add to your .bashrc/.zshrc:
#   source /path/to/amof/scripts/amof-aliases.sh

# Detect AMOF root (where this script is located) for bash or zsh sourcing
if [[ -n "$BASH_SOURCE" ]]; then
    _amof_src="${BASH_SOURCE[0]}"
elif [[ -n "$ZSH_VERSION" ]]; then
    _amof_src="${(%):-%N}"
else
    _amof_src="$0"
fi
AMOF_ROOT="$(cd "$(dirname "$_amof_src")/.." && pwd)"
export AMOF_ROOT
unset _amof_src

# AMOF command wrapper
amof() {
    local _amof_cwd _search_dir _run_root
    _amof_cwd="$PWD"
    _search_dir="$PWD"
    _run_root=""

    # Prefer the nearest ancestor containing scripts/amof.py so commands work
    # from linked worktrees and nested paths inside them.
    while [[ "$_search_dir" != "/" ]]; do
        if [[ -f "$_search_dir/scripts/amof.py" ]]; then
            _run_root="$_search_dir"
            break
        fi
        _search_dir="$(dirname "$_search_dir")"
    done

    if [[ -z "$_run_root" ]]; then
        _run_root="$AMOF_ROOT"
    fi

    (
        export AMOF_CWD="$_amof_cwd"
        export AMOF_ROOT="$_run_root"
        cd "$_run_root" && python3 "$_run_root/scripts/amof.py" "$@"
    )
}

# Export for subshells
export -f amof 2>/dev/null || true

# Load shell completions
if [[ -n "$ZSH_VERSION" ]]; then
    # Zsh
    source "$AMOF_ROOT/scripts/amof-completion.zsh" 2>/dev/null
elif [[ -n "$BASH_VERSION" ]]; then
    # Bash
    source "$AMOF_ROOT/scripts/amof-completion.bash" 2>/dev/null
fi

echo "AMOF aliases loaded. Try: amof <TAB> for completions"

