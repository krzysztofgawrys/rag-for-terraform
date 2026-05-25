<!-- input -->
Module: modules/webserver (27 usages)
Usage summaries:
- modules/webserver@v2.4.1 in prod/eu-west-1 co-deployed with: modules/elasticsearch (cluster_size=3), modules/sftp, modules/cloudfront
- modules/webserver@v2.4.1 in staging/eu-west-1 co-deployed with: modules/elasticsearch (cluster_size=1)
- modules/webserver@v2.4.0 in prod/us-east-1 co-deployed with: modules/elasticsearch (cluster_size=3), modules/sftp, modules/cloudfront, shared/waf-rules
- modules/webserver@v2.3.0 in dev/eu-west-1 (standalone)
- ... (23 more)

<!-- output -->
Typical stack (24/27 deployments): always co-deployed with modules/elasticsearch (24/27, cluster_size scales with env: prod=3, staging=1, dev=1). Frequently with modules/sftp (20/27) for inbound data feeds. Frequently with modules/cloudfront (18/27) for public-facing endpoints. shared/waf-rules appears in prod deployments with cloudfront (15/27). Module wired to outputs of shared/vpc (vpc_id, subnet_ids) and shared/iam (role_arns). Standalone deployment (no co-modules) only in dev (3/27).
ASSESSMENT: STRONG
