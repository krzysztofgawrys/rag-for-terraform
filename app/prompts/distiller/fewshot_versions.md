<!-- input -->
Module: modules/webserver (27 usages)
Versions observed:
- v2.4.1: 15 usages (prod: 10, staging: 3, dev: 2)
- v2.4.0: 8 usages (prod: 5, staging: 2, dev: 1)
- v2.3.0: 3 usages (dev only)
- v2.2.0: 1 usage (legacy)

<!-- output -->
Version pinning: all deployments use exact semver refs (27/27), no floating refs (master/main). Prod tends to lag 1 minor behind latest (v2.4.0 still in 5 prod deployments while v2.4.1 available). Dev uses older versions for testing (v2.3.0). One legacy pinned to v2.2.0 - upgrade candidate.
ASSESSMENT: STRONG
