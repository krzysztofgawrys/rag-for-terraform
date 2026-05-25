<!-- input -->
Module: modules/webserver (27 usages)
Source locators:
- infra-prod@abc:eu-west-1/webserver/main.tf
- infra-prod@abc:us-east-1/webserver/main.tf
- infra-staging@def:eu-west-1/webserver/main.tf
- infra-dev@ghi:eu-west-1/webserver/main.tf
- ... (23 more)

<!-- output -->
File layout follows '{region}/{module_name}/main.tf' pattern (24/27). Each environment has its own repo (infra-prod, infra-staging, infra-dev). Module definitions are isolated per directory, not mixed in shared files.
ASSESSMENT: STRONG
