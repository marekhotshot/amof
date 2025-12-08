# CONTEXT BUILDER SPECIFICATION

## Input
- amof.yaml
- target repo name
- include/exclude patterns

## Output

### context/<service>/index.json
- list of files  
- size  
- type  
- relevance  

### context/<service>/summary.md
- architecture summary  
- modules  
- key functions  
- TODOs  
- known issues  

### full/
Optional folder with full copies of included files.

## Rules
- Respect token limits from amof.yaml
- Ignore binaries
- Always include README.md at root
