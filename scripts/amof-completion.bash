#!/bin/bash
# AMOF bash completion script
# Source this file in your .bashrc: source /path/to/amof-completion.bash

_amof_completions() {
    local cur prev opts commands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    
    # Main commands
    commands="sync status add-repo context install workspace push discard ecosystem actor helm images audit check"
    
    # First argument - main command
    if [[ ${COMP_CWORD} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "${commands} --help --version" -- ${cur}) )
        return 0
    fi
    
    # Command-specific completions
    case "${COMP_WORDS[1]}" in
        sync|status)
            opts="--repo --help"
            COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            ;;
        install)
            opts="--push --dry-run --help"
            COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            ;;
        discard)
            opts="--force --dry-run --help"
            COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            ;;
        context)
            opts="--type --format --incremental --help"
            COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            ;;
        ecosystem)
            if [[ ${COMP_CWORD} -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "create list --help" -- ${cur}) )
            else
                opts="--from --help"
                COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            fi
            ;;
        actor)
            if [[ ${COMP_CWORD} -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "add list update --help" -- ${cur}) )
            else
                opts="--name --type --status --help"
                COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            fi
            ;;
        helm)
            if [[ ${COMP_CWORD} -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "sync diff template --help" -- ${cur}) )
            else
                opts="--source --branch --help"
                COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            fi
            ;;
        images)
            if [[ ${COMP_CWORD} -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "discover diff migrate verify --help" -- ${cur}) )
            else
                opts="--ticket --dry-run --help"
                COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            fi
            ;;
        audit)
            if [[ ${COMP_CWORD} -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "list show record --help" -- ${cur}) )
            else
                opts="--ecosystem --limit --customer --ticket --type --message --help"
                COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            fi
            ;;
        add-repo)
            opts="--branch --path --include --exclude --replace --sync --help"
            COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            ;;
        *)
            opts="--help"
            COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            ;;
    esac
    
    return 0
}

# Register completion
complete -F _amof_completions amof
complete -F _amof_completions python3\ scripts/amof.py

