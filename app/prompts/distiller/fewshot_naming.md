<!-- input -->
Module: modules/webserver (27 usages)
Usage summaries:
- modules/webserver@v2.4.1 in prod/eu-west-1 as 'webserver-prod-eu'
- modules/webserver@v2.4.1 in staging/eu-west-1 as 'webserver-staging-eu'
- modules/webserver@v2.4.0 in prod/us-east-1 as 'webserver-prod-us'
- modules/webserver@v2.3.0 in dev/eu-west-1 as 'webserver-dev-eu'
- modules/webserver@v2.2.0 in prod/eu-west-1 as 'webserver-legacy-eu' (infra-legacy)
- ... (22 more following same pattern)

<!-- output -->
Instance naming follows '{module_name}-{env}-{region_short}' pattern (24/27 deployments). Examples: 'webserver-prod-eu', 'webserver-staging-us', 'webserver-dev-eu'. Region short codes: eu=eu-west-1, us=us-east-1, ap=ap-southeast-1. 3 legacy deployments in infra-legacy use '{module_name}-legacy-{region_short}' - do NOT replicate.
ASSESSMENT: STRONG
