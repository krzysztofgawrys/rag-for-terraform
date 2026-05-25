#!/bin/sh
# Worker entrypoint for ECS Fargate.
# Writes the SSH deploy key from env var to a file and configures git to use it.

set -e

if [ -n "$SSH_DEPLOY_KEY_B64" ] && [ "$SSH_DEPLOY_KEY_B64" != "not-configured" ]; then
    mkdir -p /home/app/.ssh
    echo "$SSH_DEPLOY_KEY_B64" | base64 -d > /home/app/.ssh/deploy_key
    chmod 600 /home/app/.ssh/deploy_key
    export GIT_SSH_COMMAND="ssh -i /home/app/.ssh/deploy_key -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null"
    echo "SSH deploy key configured"
fi

exec "$@"
