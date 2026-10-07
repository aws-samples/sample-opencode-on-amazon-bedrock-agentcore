#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# setup-oauth-app.sh -- Register the GitHub OAuth App with AgentCore Identity.
#
# Stores the OAuth App client id/secret in Secrets Manager
# (opencode/github-oauth-app) and creates or updates the
# "github-provider" OAuth2 credential provider. Safe to re-run.
#
#   ./scripts/setup-oauth-app.sh                      # prompts for client id/secret
#   ./scripts/setup-oauth-app.sh --client-id ID --client-secret SECRET
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - AWS_REGION set (or pass --region)
#   - `cdk deploy` completed (AgentCore Identity must exist in the region)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SECRET_PREFIX="opencode"
AWS_PROFILE="${AWS_PROFILE:-}"
AWS_REGION="${AWS_REGION:-}"
PROVIDER="github"
CLIENT_ID=""
CLIENT_SECRET=""

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --add)         shift ;;   # accepted for backwards compatibility (no-op)
    --provider)
      PROVIDER="$2"
      if [[ "$PROVIDER" != "github" ]]; then
        echo "Unknown provider: $PROVIDER"; exit 1
      fi
      shift 2 ;;
    --client-id)   CLIENT_ID="$2"; shift 2 ;;
    --client-secret) CLIENT_SECRET="$2"; shift 2 ;;
    --profile)     AWS_PROFILE="$2"; shift 2 ;;
    --region)      AWS_REGION="$2"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
Usage: setup-oauth-app.sh [OPTIONS]

Register (or update) the GitHub OAuth App with AgentCore Identity.

Options:
  --provider       github (default and only supported value)
  --client-id      OAuth App client ID (prompted if omitted)
  --client-secret  OAuth App client secret (prompted if omitted)
  --profile        AWS CLI profile (or set AWS_PROFILE)
  --region         AWS region (or set AWS_REGION; required)
  -h, --help       Show this help
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Require a region
# ---------------------------------------------------------------------------
if [[ -z "$AWS_REGION" ]]; then
  echo "error: AWS_REGION is not set. Export it or pass --region <region>." >&2
  echo "  Confirmed deployable regions: us-east-1, eu-central-1" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Check AWS credentials -- prompt for profile if not configured
# ---------------------------------------------------------------------------
check_aws_credentials() {
  local test_args=(--region "$AWS_REGION")
  [[ -n "$AWS_PROFILE" ]] && test_args+=(--profile "$AWS_PROFILE")

  if aws sts get-caller-identity "${test_args[@]}" &>/dev/null; then
    local acct
    acct=$(aws sts get-caller-identity "${test_args[@]}" --output text --query 'Account' 2>/dev/null)
    echo "AWS credentials OK (account: ${acct})"
    [[ -n "$AWS_PROFILE" ]] && echo "Using profile: $AWS_PROFILE"
    echo ""
    return 0
  fi
  return 1
}

if ! check_aws_credentials; then
  echo "No valid AWS credentials found."
  echo ""

  profiles=()
  if [[ -f ~/.aws/config ]]; then
    while IFS= read -r line; do profiles+=("$line"); done \
      < <(grep -oE '\[profile [^]]+\]' ~/.aws/config 2>/dev/null | sed 's/\[profile //;s/\]//' || true)
  fi
  if [[ -f ~/.aws/credentials ]]; then
    while IFS= read -r line; do profiles+=("$line"); done \
      < <(grep -oE '\[[^]]+\]' ~/.aws/credentials 2>/dev/null | sed 's/\[//;s/\]//' || true)
  fi
  # Deduplicate
  if [[ ${#profiles[@]} -gt 0 ]]; then
    deduped=()
    while IFS= read -r line; do deduped+=("$line"); done < <(printf '%s\n' "${profiles[@]}" | sort -u)
    profiles=("${deduped[@]}")
  fi

  if [[ ${#profiles[@]} -eq 0 ]]; then
    echo "No AWS profiles found. Run 'aws configure' or set AWS_PROFILE."
    exit 1
  fi

  echo "Available AWS profiles:"
  for i in "${!profiles[@]}"; do echo "  $((i + 1))) ${profiles[$i]}"; done
  echo ""
  read -rp "Select profile [1-${#profiles[@]}]: " profile_choice

  if [[ "$profile_choice" -ge 1 && "$profile_choice" -le ${#profiles[@]} ]] 2>/dev/null; then
    AWS_PROFILE="${profiles[$((profile_choice - 1))]}"
    export AWS_PROFILE
    echo ""
    if ! check_aws_credentials; then
      echo "Selected profile '$AWS_PROFILE' does not have valid credentials."
      echo "You may need to run: aws sso login --profile $AWS_PROFILE"
      exit 1
    fi
  else
    echo "Invalid choice"; exit 1
  fi
fi

# ---------------------------------------------------------------------------
# AWS CLI args (reused everywhere)
# ---------------------------------------------------------------------------
AWS_ARGS=(--region "$AWS_REGION" --no-cli-pager)
[[ -n "$AWS_PROFILE" ]] && AWS_ARGS+=(--profile "$AWS_PROFILE")

# ---------------------------------------------------------------------------
# Resolve provider -> secret name and registration name
# ---------------------------------------------------------------------------
resolve_names() {
  # Sets: SECRET_NAME, DISPLAY_HOST, PROVIDER_REG_NAME
  case "$PROVIDER" in
    github)
      SECRET_NAME="${SECRET_PREFIX}/github-oauth-app"
      DISPLAY_HOST="github.com"
      PROVIDER_REG_NAME="github-provider"
      ;;
    *) echo "Unknown provider: $PROVIDER"; exit 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Show provider-specific setup instructions
# ---------------------------------------------------------------------------
show_instructions() {
  echo ""
  echo "=== GitHub OAuth App Setup ==="
  echo ""
  echo "1. Go to: https://github.com/settings/developers"
  echo "   (Profile picture -> Settings -> Developer settings -> OAuth Apps)"
  echo "2. Click 'New OAuth App' (or 'Register a new application')"
  echo "3. Fill in:"
  echo "   - Application name: OpenCode on AgentCore"
  echo "   - Homepage URL: https://github.com (or your org URL)"
  echo "   - Authorization callback URL: use any placeholder for now"
  echo "     (the script will show the correct URL after registration)"
  echo "4. Leave 'Enable Device Flow' unchecked"
  echo "   (not needed -- we use the authorization code flow)"
  echo "5. Click 'Register application'"
  echo "6. Copy the Client ID from the app page"
  echo "7. Click 'Generate a new client secret' -- copy it immediately (shown only once)"
  echo ""
  echo "Docs: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app"
  echo ""
}

# ---------------------------------------------------------------------------
# Add (create or update) the provider
# ---------------------------------------------------------------------------
do_add() {
  resolve_names
  show_instructions

  if [[ -z "$CLIENT_ID" ]]; then
    read -rp "OAuth App Client ID: " CLIENT_ID
  fi
  if [[ -z "$CLIENT_SECRET" ]]; then
    read -rsp "OAuth App Client Secret: " CLIENT_SECRET
    echo ""
  fi
  [[ -z "$CLIENT_ID" || -z "$CLIENT_SECRET" ]] && { echo "Error: client_id and client_secret are required"; exit 1; }

  local secret_value
  secret_value="{\"client_id\":\"${CLIENT_ID}\",\"client_secret\":\"${CLIENT_SECRET}\",\"provider\":\"${PROVIDER}\",\"host\":\"${DISPLAY_HOST}\"}"

  echo ""
  echo "Storing OAuth App credentials:"
  echo "  Provider:    $PROVIDER ($DISPLAY_HOST)"
  echo "  Secret name: $SECRET_NAME"
  echo "  Region:      $AWS_REGION"
  echo ""

  if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" "${AWS_ARGS[@]}" &>/dev/null; then
    echo "Secret exists -- updating..."
    echo "$secret_value" | aws secretsmanager put-secret-value \
      --secret-id "$SECRET_NAME" \
      --secret-string file:///dev/stdin \
      "${AWS_ARGS[@]}"
  else
    echo "Creating secret..."
    echo "$secret_value" | aws secretsmanager create-secret \
      --name "$SECRET_NAME" \
      --description "OAuth App credentials for AgentCore Identity ($DISPLAY_HOST)" \
      --secret-string file:///dev/stdin \
      "${AWS_ARGS[@]}"
  fi
  echo ""
  echo "Done. Secret stored at: $SECRET_NAME"
  echo ""

  # Register credential provider
  echo "Registering credential provider with AgentCore Identity..."

  local vendor_config="{\"githubOauth2ProviderConfig\":{\"clientId\":\"${CLIENT_ID}\",\"clientSecret\":\"${CLIENT_SECRET}\"}}"
  local provider_vendor="GithubOauth2"
  local result=""

  if result=$(echo "$vendor_config" | aws bedrock-agentcore-control create-oauth2-credential-provider \
      --name "$PROVIDER_REG_NAME" \
      --credential-provider-vendor "$provider_vendor" \
      --oauth2-provider-config-input file:///dev/stdin \
      "${AWS_ARGS[@]}" 2>/dev/null); then
    echo "Credential provider '$PROVIDER_REG_NAME' registered."
  elif result=$(echo "$vendor_config" | aws bedrock-agentcore-control update-oauth2-credential-provider \
      --name "$PROVIDER_REG_NAME" \
      --credential-provider-vendor "$provider_vendor" \
      --oauth2-provider-config-input file:///dev/stdin \
      "${AWS_ARGS[@]}" 2>/dev/null); then
    echo "Credential provider '$PROVIDER_REG_NAME' updated."
  else
    echo ""
    echo "Warning: Could not register credential provider automatically."
    echo "This may happen if AgentCore Identity is not yet deployed."
    echo "Re-run this script after \`cdk deploy\` completes."
  fi

  # Extract the callback URL from the create/update response.
  # The CreateOauth2CredentialProvider API returns a `callbackUrl` field
  # directly.  Fall back to constructing from the ARN for older SDK versions.
  local callback_url
  callback_url=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('callbackUrl',''))" 2>/dev/null || true)

  if [[ -z "$callback_url" ]]; then
    # Fallback: extract UUID from the ARN (legacy behavior).
    local provider_arn callback_uuid
    provider_arn=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('credentialProviderArn',''))" 2>/dev/null || true)
    if [[ -n "$provider_arn" ]]; then
      callback_uuid="${provider_arn##*/}"
      callback_url="https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/identities/oauth2/callback/${callback_uuid}"
    fi
  fi

  if [[ -n "$callback_url" ]]; then
    echo ""
    echo "=== IMPORTANT: Update your OAuth App callback URL ==="
    echo ""
    echo "Set the Authorization callback URL in your OAuth App to:"
    echo "  $callback_url"
    echo ""
    echo "AgentCore Identity appends a provider-specific UUID to the callback path."
    echo "The OAuth App callback URL must match exactly, or GitHub will reject the redirect."
  fi

  echo ""
  echo "Setup complete -- the credential provider is active."
}

do_add
