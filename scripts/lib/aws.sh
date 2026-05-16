#!/usr/bin/env bash
# AMOF AWS Library Functions
# Source this file in other scripts: source "$(dirname "$0")/../lib/aws.sh"

# Default AWS settings
: "${AWS_REGION:=eu-central-1}"
: "${AWS_PROFILE:=demo-profile}"
: "${ECR_REPO_PREFIX:=my-ecr-prefix}"

# Get AWS account ID
get_aws_account() {
    aws sts get-caller-identity --query Account --output text --profile "$AWS_PROFILE"
}

# Get ECR base URL
get_ecr_base() {
    local account
    account=$(get_aws_account)
    echo "${account}.dkr.ecr.${AWS_REGION}.amazonaws.com"
}

# Login to ECR
ecr_login() {
    local ecr_base
    ecr_base=$(get_ecr_base)
    log_info "Logging into ECR: $ecr_base"
    aws ecr get-login-password --region "$AWS_REGION" --profile "$AWS_PROFILE" \
        | docker login --username AWS --password-stdin "$ecr_base"
}

# Ensure ECR repository exists
ensure_ecr_repo() {
    local repo_name="$1"
    if ! aws ecr describe-repositories \
        --repository-names "$repo_name" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" >/dev/null 2>&1; then
        log_info "Creating ECR repository: $repo_name"
        aws ecr create-repository \
            --repository-name "$repo_name" \
            --region "$AWS_REGION" \
            --profile "$AWS_PROFILE" >/dev/null
    fi
}

# Check if image exists in ECR
ecr_image_exists() {
    local repo="$1"
    local tag="$2"
    aws ecr describe-images \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        --repository-name "$repo" \
        --image-ids imageTag="$tag" >/dev/null 2>&1
}

# Get CloudFormation nested stack by logical ID pattern
get_nested_stack() {
    local parent_stack="$1"
    local pattern="$2"
    aws cloudformation list-stack-resources \
        --stack-name "$parent_stack" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        --output json \
        | jq -r --arg re "$pattern" '.StackResourceSummaries[]
            | select(.ResourceType=="AWS::CloudFormation::Stack")
            | select(.LogicalResourceId|test($re; "i"))
            | .PhysicalResourceId' | head -n1
}

# Get CloudFormation stack events
get_stack_events() {
    local stack="$1"
    local limit="${2:-50}"
    aws cloudformation describe-stack-events \
        --stack-name "$stack" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        --query "reverse(StackEvents)[].{Time:Timestamp,LogicalId:LogicalResourceId,Status:ResourceStatus,Reason:ResourceStatusReason}" \
        --output table | head -n "$limit"
}

# Get Lambda log group events
get_lambda_logs() {
    local log_group="$1"
    local lookback_sec="${2:-3600}"
    local start_ms=$(( $(date +%s) * 1000 - lookback_sec * 1000 ))
    local end_ms=$(( $(date +%s) * 1000 ))
    
    aws logs filter-log-events \
        --log-group-name "$log_group" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        --start-time "$start_ms" \
        --end-time "$end_ms" \
        --limit 500 \
        --query 'events[].message' \
        --output text
}

