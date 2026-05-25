<!-- input -->
Module: modules/webserver (27 usages)
Usage summaries:
- modules/webserver in prod: tags={env='prod', project='my-project', team='platform', managed_by='terraform'}
- modules/webserver in staging: tags={env='staging', project='my-project', team='platform', managed_by='terraform'}
- ... (25 more similar)

<!-- output -->
Tags always include env, project, team, managed_by (27/27). project is consistently 'my-project' for webserver. team is 'platform' across all deployments. No cost-center or owner tags observed.
ASSESSMENT: STRONG
