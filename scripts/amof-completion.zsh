#compdef amof
# AMOF zsh completion script
# Source this file in your .zshrc: source /path/to/amof-completion.zsh

_amof() {
    local -a commands
    local -a options
    
    commands=(
        'sync:Synchronize repositories'
        'status:Show repository status'
        'add-repo:Add repository to manifest'
        'context:Generate AI context'
        'install:Bootstrap workspace'
        'workspace:Generate workspace file'
        'push:Push all branches'
        'discard:Delete workspace'
        'ecosystem:Manage ecosystems'
        'actor:Manage actors'
        'helm:Helm operations'
        'images:Image operations'
        'audit:Audit trail'
        'check:Check prerequisites'
    )
    
    case $state in
        (cmd)
            _describe -t commands 'amof commands' commands
            ;;
        (*)
            case $words[2] in
                sync|status)
                    _arguments \
                        '--repo[Repo name]:repo:' \
                        '--help[Show help]'
                    ;;
                install)
                    _arguments \
                        '1:ticket_id:' \
                        '--push[Push to origin]' \
                        '--dry-run[Preview actions]' \
                        '--help[Show help]'
                    ;;
                discard)
                    _arguments \
                        '--force[Skip confirmation]' \
                        '--dry-run[Preview actions]' \
                        '--help[Show help]'
                    ;;
                context)
                    _arguments \
                        '1:service:' \
                        '--type[Context types]:type:(all api config structure impact chunks)' \
                        '--format[Output format]:format:(json markdown)' \
                        '--incremental[Only changed files]' \
                        '--help[Show help]'
                    ;;
                ecosystem)
                    local -a subcmds
                    subcmds=('create:Create ecosystem' 'list:List ecosystems')
                    _describe -t subcmds 'ecosystem subcommands' subcmds
                    ;;
                actor)
                    local -a subcmds
                    subcmds=('add:Add actor' 'list:List actors' 'update:Update actor')
                    _describe -t subcmds 'actor subcommands' subcmds
                    ;;
                helm)
                    local -a subcmds
                    subcmds=('sync:Sync chart' 'diff:Compare charts' 'template:Render chart')
                    _describe -t subcmds 'helm subcommands' subcmds
                    ;;
                images)
                    local -a subcmds
                    subcmds=('discover:Find images' 'diff:Compare images' 'migrate:Migrate images' 'verify:Verify images')
                    _describe -t subcmds 'images subcommands' subcmds
                    ;;
                audit)
                    local -a subcmds
                    subcmds=('list:List entries' 'show:Show entry' 'record:Record entry')
                    _describe -t subcmds 'audit subcommands' subcmds
                    ;;
                check)
                    _arguments '--help[Show help]'
                    ;;
                add-repo)
                    _arguments \
                        '1:name:' \
                        '2:url:' \
                        '--branch[Branch]:branch:' \
                        '--path[Local path]:path:_files -/' \
                        '--include[Include glob]:glob:' \
                        '--exclude[Exclude glob]:glob:' \
                        '--replace[Replace existing]' \
                        '--sync[Sync after adding]' \
                        '--help[Show help]'
                    ;;
                *)
                    _arguments '--help[Show help]' '--version[Show version]'
                    ;;
            esac
            ;;
    esac
}

_amof "$@"

