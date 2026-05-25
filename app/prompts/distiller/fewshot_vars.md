<!-- input -->
Module: modules/webserver (27 usages)
Usage summaries:
- modules/webserver@v2.4.1 in prod/eu-west-1 vars: app_name='my-webapp', env='prod', vpc_id=module.vpc.id, instance_size='large', backup_enabled=true, monitoring_level='detailed'
- modules/webserver@v2.4.1 in staging/eu-west-1 vars: app_name='my-webapp', env='staging', vpc_id=module.vpc.id, instance_size='medium', monitoring_level='basic'
- modules/webserver@v2.4.0 in prod/us-east-1 vars: app_name='my-webapp', env='prod', vpc_id=module.vpc.id, instance_size='large', backup_enabled=true, monitoring_level='detailed', custom_security_groups=[sg-xxx]
- modules/webserver@v2.3.0 in dev/eu-west-1 vars: app_name='test-app', env='dev', vpc_id=module.vpc.id, instance_size='small'
- ... (23 more following same patterns)

<!-- output -->
Required variables (always passed, 27/27): app_name, env, vpc_id, instance_size. app_name is typically the project/service name ('my-webapp' in 24/27, 'test-app' only in dev). vpc_id always references module.vpc.id output, never hardcoded. instance_size values: 'large' for prod (20/27), 'medium' for staging (4/27), 'small' for dev (3/27). Usually passed (>80%): backup_enabled (true in prod, omitted in dev - defaults false), monitoring_level ('detailed' in prod, 'basic' elsewhere). Rarely passed (<20%): custom_security_groups (3 edge cases with specific network requirements), override_ami_id (1 case). Never pass deprecated_legacy_mode - removed in v2.0+.
ASSESSMENT: STRONG
